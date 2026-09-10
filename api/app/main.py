import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

import app.models  # noqa: F401 — ensures models are registered with Base before create_all
from app.config import settings
from app.db.session import Base, SessionLocal, engine
from app.models.cat import Cat
from app.routers import (
    auth,
    cat_merges,
    cats,
    claims,
    exploration,
    explorer,
    media,
    moderation,
    notifications,
    sightings,
    trait_changes,
    users,
)
from app.middleware import add_security_headers
from app.services.reconcile import reconcile
from app.services.retention import sweep_claim_photos, sweep_rate_limit_counters
from app.services.demo_seed import seed_apple_review_demo
from app.utils.rarity import compute_rarity_score

log = logging.getLogger(__name__)


def _require_secure_config() -> None:
    """Fail fast if a real (non-SQLite) deployment runs with an insecure secret.

    A production database implies production traffic, so the JWT signing key must
    not be the built-in default or a trivially short value — otherwise anyone can
    forge tokens for any account. Local/dev (SQLite) and the test suite are
    exempt so they keep working with the default.
    """
    if settings.database_url and not settings.database_url.startswith("sqlite"):
        if settings.secret_key in ("", "change-me") or len(settings.secret_key) < 32:
            raise RuntimeError(
                "SECRET_KEY must be set to a strong (>=32 char) value when running "
                "against a non-SQLite database. Refusing to start with an insecure key."
            )


_require_secure_config()

Base.metadata.create_all(bind=engine)

# Ad-hoc column migrations for pre-existing SQLite dev databases. create_all
# won't add columns to tables that already exist, so on SQLite we patch them in
# on startup. These use PRAGMA (SQLite-only) and are redundant on a fresh
# Postgres database, where create_all builds every column from the models — so
# the whole block is skipped on non-SQLite engines. Schema versioning on
# Postgres is Alembic's job (next step), not this.
with engine.connect() as _conn:
    from sqlalchemy import text as _text
    if engine.dialect.name == "sqlite":
        _s_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(sightings)")).fetchall()]
        if "user_id" not in _s_cols:
            _conn.execute(_text("ALTER TABLE sightings ADD COLUMN user_id INTEGER REFERENCES users(id)"))
            _conn.commit()
        if "frame_id" not in _s_cols:
            _conn.execute(_text("ALTER TABLE sightings ADD COLUMN frame_id TEXT"))
            _conn.commit()
        if "photo_adjust" not in _s_cols:
            _conn.execute(_text("ALTER TABLE sightings ADD COLUMN photo_adjust TEXT"))
            _conn.commit()
        if "caption" not in _s_cols:
            _conn.execute(_text("ALTER TABLE sightings ADD COLUMN caption TEXT"))
            _conn.commit()
        _u_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(users)")).fetchall()]
        if "avatar_emoji" not in _u_cols:
            _conn.execute(_text("ALTER TABLE users ADD COLUMN avatar_emoji TEXT"))
            _conn.commit()
        if "display_name_updated_at" not in _u_cols:
            _conn.execute(_text("ALTER TABLE users ADD COLUMN display_name_updated_at DATETIME"))
            _conn.commit()
        if "email_verified" not in _u_cols:
            _conn.execute(_text("ALTER TABLE users ADD COLUMN email_verified BOOLEAN NOT NULL DEFAULT 0"))
            _conn.commit()
        if "catalog_layout" not in _u_cols:
            _conn.execute(_text("ALTER TABLE users ADD COLUMN catalog_layout TEXT"))
            _conn.commit()
        if "notify_nearby_sightings" not in _u_cols:
            _conn.execute(_text("ALTER TABLE users ADD COLUMN notify_nearby_sightings BOOLEAN NOT NULL DEFAULT 1"))
            _conn.commit()
        if "notify_new_cat_in_area" not in _u_cols:
            _conn.execute(_text("ALTER TABLE users ADD COLUMN notify_new_cat_in_area BOOLEAN NOT NULL DEFAULT 1"))
            _conn.commit()
        if "is_admin" not in _u_cols:
            _conn.execute(_text("ALTER TABLE users ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT 0"))
            _conn.commit()
        if "content_strikes" not in _u_cols:
            _conn.execute(_text("ALTER TABLE users ADD COLUMN content_strikes INTEGER NOT NULL DEFAULT 0"))
            _conn.commit()
        if "banned_at" not in _u_cols:
            _conn.execute(_text("ALTER TABLE users ADD COLUMN banned_at DATETIME"))
            _conn.commit()
        if "onboarded_at" not in _u_cols:
            _conn.execute(_text("ALTER TABLE users ADD COLUMN onboarded_at DATETIME"))
            _conn.commit()
        _ev_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(email_verifications)")).fetchall()]
        if _ev_cols and "attempts" not in _ev_cols:
            _conn.execute(_text("ALTER TABLE email_verifications ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"))
            _conn.commit()
        _pr_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(password_resets)")).fetchall()]
        if _pr_cols and "attempts" not in _pr_cols:
            _conn.execute(_text("ALTER TABLE password_resets ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"))
            _conn.commit()
        _c_info = _conn.execute(_text("PRAGMA table_info(cat_claims)")).fetchall()
        _c_cols = [r[1] for r in _c_info]
        if _c_cols and "real_name" not in _c_cols:
            _conn.execute(_text("ALTER TABLE cat_claims ADD COLUMN real_name TEXT"))
            _conn.commit()
        if _c_cols and "reviewed_by_id" not in _c_cols:
            _conn.execute(_text("ALTER TABLE cat_claims ADD COLUMN reviewed_by_id INTEGER REFERENCES users(id)"))
            _conn.commit()
        if _c_cols and "source" not in _c_cols:
            _conn.execute(_text("ALTER TABLE cat_claims ADD COLUMN source TEXT NOT NULL DEFAULT 'claim'"))
            _conn.commit()
        # cat_id became nullable (a pending registration has no cat until it is
        # approved). SQLite has no ALTER COLUMN, so this is the documented
        # 12-step rebuild, guarded so it runs at most once. PRAGMA table_info
        # rows are (cid, name, type, notnull, dflt_value, pk).
        # The rebuilt table drops avg_confidence on the way through: claims are
        # no longer scored, so there is no number to carry over.
        _cat_id_notnull = next((r[3] for r in _c_info if r[1] == "cat_id"), 0)
        if _c_info and _cat_id_notnull:
            _conn.execute(_text("PRAGMA foreign_keys=OFF"))
            _conn.execute(_text("""
                CREATE TABLE cat_claims_new (
                    id INTEGER NOT NULL PRIMARY KEY,
                    cat_id INTEGER REFERENCES cats(id),
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    status VARCHAR,
                    source VARCHAR NOT NULL DEFAULT 'claim',
                    rejection_reason TEXT,
                    reviewed_by_id INTEGER REFERENCES users(id),
                    real_name VARCHAR,
                    likes_petting BOOLEAN,
                    accepts_treats BOOLEAN,
                    age_years INTEGER,
                    fun_fact TEXT,
                    indoor_outdoor VARCHAR,
                    created_at DATETIME,
                    decided_at DATETIME
                )
            """))
            _conn.execute(_text("""
                INSERT INTO cat_claims_new (
                    id, cat_id, user_id, status, source, rejection_reason,
                    reviewed_by_id, real_name, likes_petting, accepts_treats, age_years,
                    fun_fact, indoor_outdoor, created_at, decided_at
                )
                SELECT id, cat_id, user_id, status, source, rejection_reason,
                       reviewed_by_id, real_name, likes_petting, accepts_treats, age_years,
                       fun_fact, indoor_outdoor, created_at, decided_at
                FROM cat_claims
            """))
            _conn.execute(_text("DROP TABLE cat_claims"))
            _conn.execute(_text("ALTER TABLE cat_claims_new RENAME TO cat_claims"))
            _conn.execute(_text("CREATE INDEX IF NOT EXISTS ix_cat_claims_cat_id ON cat_claims (cat_id)"))
            _conn.execute(_text("CREATE INDEX IF NOT EXISTS ix_cat_claims_user_id ON cat_claims (user_id)"))
            _conn.execute(_text("CREATE INDEX IF NOT EXISTS ix_cat_claims_status ON cat_claims (status)"))
            _conn.commit()
            _conn.execute(_text("PRAGMA foreign_keys=ON"))
        # Databases that already went through the rebuild above (or that never
        # needed it) still carry the scoring columns. DROP COLUMN needs SQLite
        # >= 3.35; older dev machines just leave them behind, inert.
        import sqlite3 as _sqlite3_claims
        if _sqlite3_claims.sqlite_version_info >= (3, 35, 0):
            _c_now = [r[1] for r in _conn.execute(_text("PRAGMA table_info(cat_claims)")).fetchall()]
            if "avg_confidence" in _c_now:
                _conn.execute(_text("ALTER TABLE cat_claims DROP COLUMN avg_confidence"))
                _conn.commit()
            _cp_now = [r[1] for r in _conn.execute(_text("PRAGMA table_info(claim_photos)")).fetchall()]
            if "confidence" in _cp_now:
                _conn.execute(_text("ALTER TABLE claim_photos DROP COLUMN confidence"))
                _conn.commit()
        _n_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(notifications)")).fetchall()]
        if _n_cols and "post_id" not in _n_cols:
            _conn.execute(_text("ALTER TABLE notifications ADD COLUMN post_id INTEGER REFERENCES explorer_posts(id)"))
            _conn.commit()
        _e_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(explorer_posts)")).fetchall()]
        if _e_cols and "cat_id" not in _e_cols:
            _conn.execute(_text("ALTER TABLE explorer_posts ADD COLUMN cat_id INTEGER REFERENCES cats(id)"))
            _conn.commit()
        if _e_cols and "hidden_at" not in _e_cols:
            _conn.execute(_text("ALTER TABLE explorer_posts ADD COLUMN hidden_at DATETIME"))
            _conn.commit()
        if _e_cols and "hidden_reason" not in _e_cols:
            _conn.execute(_text("ALTER TABLE explorer_posts ADD COLUMN hidden_reason TEXT"))
            _conn.commit()
        _rep_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(post_reports)")).fetchall()]
        if _rep_cols and "reviewed_at" not in _rep_cols:
            _conn.execute(_text("ALTER TABLE post_reports ADD COLUMN reviewed_at DATETIME"))
            _conn.commit()
        if _rep_cols and "reviewed_by_id" not in _rep_cols:
            _conn.execute(_text("ALTER TABLE post_reports ADD COLUMN reviewed_by_id INTEGER REFERENCES users(id)"))
            _conn.commit()
        _et_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(explored_tiles)")).fetchall()]
        if _et_cols and "is_home" not in _et_cols:
            _conn.execute(_text("ALTER TABLE explored_tiles ADD COLUMN is_home BOOLEAN NOT NULL DEFAULT 0"))
            _conn.commit()
        # Landmarks/checkpoints were removed from the game; DROP COLUMN needs SQLite >= 3.35
        # (older dev machines just leave the columns behind, inert).
        import sqlite3 as _sqlite3
        if _sqlite3.sqlite_version_info >= (3, 35, 0):
            for _col in ("checkpoint_id", "checkpoint_name"):
                if _col in _et_cols:
                    _conn.execute(_text(f"ALTER TABLE explored_tiles DROP COLUMN {_col}"))
                    _conn.commit()
    else:
        # Postgres (prod/Neon): create_all won't add a column to the existing
        # users table. ADD COLUMN IF NOT EXISTS is idempotent on Postgres.
        _conn.execute(_text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS catalog_layout TEXT"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS content_strikes INTEGER NOT NULL DEFAULT 0"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS banned_at TIMESTAMP"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS onboarded_at TIMESTAMP"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE sightings ADD COLUMN IF NOT EXISTS frame_id TEXT"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE sightings ADD COLUMN IF NOT EXISTS photo_adjust TEXT"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE sightings ADD COLUMN IF NOT EXISTS caption TEXT"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE explored_tiles ADD COLUMN IF NOT EXISTS is_home BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        _conn.commit()
        # Landmarks/checkpoints were removed from the game.
        _conn.execute(_text(
            "ALTER TABLE explored_tiles DROP COLUMN IF EXISTS checkpoint_id"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE explored_tiles DROP COLUMN IF EXISTS checkpoint_name"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_nearby_sightings BOOLEAN NOT NULL DEFAULT TRUE"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS notify_new_cat_in_area BOOLEAN NOT NULL DEFAULT TRUE"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS is_admin BOOLEAN NOT NULL DEFAULT FALSE"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE email_verifications ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE password_resets ADD COLUMN IF NOT EXISTS attempts INTEGER NOT NULL DEFAULT 0"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE explorer_posts ADD COLUMN IF NOT EXISTS hidden_at TIMESTAMP"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE explorer_posts ADD COLUMN IF NOT EXISTS hidden_reason TEXT"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE post_reports ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP"
        ))
        _conn.commit()
        _conn.execute(_text(
            "ALTER TABLE post_reports ADD COLUMN IF NOT EXISTS reviewed_by_id INTEGER REFERENCES users(id)"
        ))
        _conn.commit()
        # cat_claims had no Postgres statements at all until claims moved to
        # human review, so the owner-card columns are patched in here too —
        # IF NOT EXISTS makes that free where they already exist.
        for _stmt in (
            "ALTER TABLE cat_claims ADD COLUMN IF NOT EXISTS real_name VARCHAR",
            "ALTER TABLE cat_claims ADD COLUMN IF NOT EXISTS likes_petting BOOLEAN",
            "ALTER TABLE cat_claims ADD COLUMN IF NOT EXISTS accepts_treats BOOLEAN",
            "ALTER TABLE cat_claims ADD COLUMN IF NOT EXISTS age_years INTEGER",
            "ALTER TABLE cat_claims ADD COLUMN IF NOT EXISTS fun_fact TEXT",
            "ALTER TABLE cat_claims ADD COLUMN IF NOT EXISTS indoor_outdoor VARCHAR",
            "ALTER TABLE cat_claims ADD COLUMN IF NOT EXISTS reviewed_by_id INTEGER REFERENCES users(id)",
            "ALTER TABLE cat_claims ADD COLUMN IF NOT EXISTS source VARCHAR NOT NULL DEFAULT 'claim'",
            # A pending registration has no cat until it is approved.
            "ALTER TABLE cat_claims ALTER COLUMN cat_id DROP NOT NULL",
            # Claims aren't scored any more, so the stored scores go too.
            "ALTER TABLE cat_claims DROP COLUMN IF EXISTS avg_confidence",
            "ALTER TABLE claim_photos DROP COLUMN IF EXISTS confidence",
        ):
            _conn.execute(_text(_stmt))
            _conn.commit()
    # Cat follows were removed (claiming a cat is the only per-cat notification
    # subscription), so sweep the orphaned table off existing databases.
    # Standard SQL, both dialects, idempotent.
    _conn.execute(_text("DROP TABLE IF EXISTS cat_follows"))
    _conn.commit()
    # The tile_key index is declared on the model (fresh create_all builds it),
    # but create_all won't add it to a pre-existing table. IF NOT EXISTS is
    # supported by both SQLite and Postgres, so patch it in for both.
    _conn.execute(_text(
        "CREATE INDEX IF NOT EXISTS ix_explored_tiles_tile_key ON explored_tiles (tile_key)"
    ))
    _conn.commit()
    # Same story for the one-verified-owner-per-cat constraint: it's declared on
    # the model, but create_all skips pre-existing tables, so a database created
    # before it was added has no such index. Approving a claim re-checks in
    # Python as well — this is the backstop against two moderators approving
    # rival claims at the same instant. Partial-index syntax is valid on both
    # dialects. If existing data already violates it the CREATE fails; log and
    # carry on rather than making the service unbootable over it.
    try:
        _conn.execute(_text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ix_one_verified_claim_per_cat "
            "ON cat_claims (cat_id) WHERE status = 'verified'"
        ))
        _conn.commit()
    except Exception:
        _conn.rollback()
        log.exception(
            "Could not create ix_one_verified_claim_per_cat — duplicate verified "
            "claims likely exist. Approval still re-checks in Python."
        )
    # Grandfather accounts that predate email verification so enforcing it
    # doesn't lock them out: a pre-feature user has no pending verification code
    # (the table didn't exist when they signed up), so mark them verified. New
    # signups always get a code row at registration, so they're left untouched
    # and must still verify. Standard SQL, all dialects, idempotent.
    _conn.execute(_text("""
        UPDATE users SET email_verified = TRUE
        WHERE email_verified = FALSE
          AND email NOT IN (SELECT email FROM email_verifications)
    """))
    _conn.commit()
    # Grandfather every account that predates onboarded_at: they finished
    # onboarding long before the column existed, so leaving it null would loop
    # them back through the intro on their next sign-in. New registrations start
    # null and are stamped by POST /auth/onboarded. Idempotent — a second run
    # finds nothing to update. (The Apple review account is deliberately reset to
    # null afterwards by the demo seed below.)
    _conn.execute(_text("""
        UPDATE users SET onboarded_at = created_at WHERE onboarded_at IS NULL
    """))
    _conn.commit()
    # Backfill: every sighting appears in the Explorer feed exactly once.
    # Standard SQL, runs on every dialect. Idempotent — re-running inserts
    # nothing new, and inserts nothing at all on a fresh (empty) database.
    _conn.execute(_text("""
        INSERT INTO explorer_posts (user_id, sighting_id, photo_path, caption, latitude, longitude, created_at)
        SELECT s.user_id, s.id, s.photo_path, s.vibes, s.latitude, s.longitude, s.spotted_at
        FROM sightings s
        WHERE NOT EXISTS (SELECT 1 FROM explorer_posts p WHERE p.sighting_id = s.id)
    """))
    _conn.commit()

with SessionLocal() as _seed_db:
    try:
        seed_apple_review_demo(_seed_db)
    except Exception:
        _seed_db.rollback()
        log.exception("Apple review demo seed failed")


async def _rarity_recompute_loop() -> None:
    """Recalculate rarity scores for all cats at startup, then every 24 hours.

    Rarity decays with time since a cat was last seen, so scores go stale
    between sightings; recomputing on launch means a restarted server reflects
    that drift immediately instead of waiting up to a day.
    """
    while True:
        db = SessionLocal()
        try:
            all_cats = db.query(Cat).all()
            for cat in all_cats:
                cat.rarity_score = compute_rarity_score(cat.sighting_count, cat.last_seen)
            db.commit()
            log.info("Rarity recompute: updated %d cats", len(all_cats))
        except Exception:
            log.exception("Rarity recompute failed")
        finally:
            db.close()
        await asyncio.sleep(86_400)


async def _storage_reconcile_loop() -> None:
    """Periodically reconcile the DB against the storage bucket.

    Sweeps orphaned objects (uploaded but never committed) older than the grace
    window and logs dangling references whose object has gone missing. Runs once
    at startup, then on the configured interval.
    """
    interval = max(1, settings.storage_reconcile_interval_hours) * 3600
    while True:
        db = SessionLocal()
        try:
            report = reconcile(db, grace_hours=settings.storage_orphan_grace_hours)
            log.info(
                "Storage reconcile: %d referenced, %d stored, %d swept, %d dangling",
                report.referenced,
                report.stored,
                len(report.deleted_keys),
                len(report.dangling_keys),
            )
        except Exception:
            log.exception("Storage reconcile failed")
        finally:
            db.close()
        await asyncio.sleep(interval)


async def _retention_sweep_loop() -> None:
    """Delete personal data past its stated retention period. Daily.

    Deliberately not gated on storage_reconcile_enabled: reconciliation is
    housekeeping an operator may switch off, whereas this enforces the retention
    period the privacy policy publishes. Runs once at startup, then daily.
    """
    while True:
        db = SessionLocal()
        try:
            sweep_claim_photos(db, retain_days=settings.claim_photo_retention_days)
            sweep_rate_limit_counters(db)
        except Exception:
            log.exception("Retention sweep failed")
        finally:
            db.close()
        await asyncio.sleep(86_400)


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(_rarity_recompute_loop()),
        asyncio.create_task(_retention_sweep_loop()),
    ]
    if settings.storage_reconcile_enabled:
        tasks.append(asyncio.create_task(_storage_reconcile_loop()))
    yield
    for task in tasks:
        task.cancel()
    for task in tasks:
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="Meow Map API", lifespan=lifespan)

_cors_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    # Auth is Bearer-token, not cookie-based, so credentials are never needed.
    # Keeping this False is what makes a wildcard origin safe (the browser blocks
    # "*" + credentials anyway) and avoids exposing the API to credentialed CSRF.
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Added after CORS so it ends up outermost (Starlette's add_middleware prepends,
# so the last one added wraps the rest) — that way the headers land on CORS
# preflight responses too, not just on the ones the routers produce.
add_security_headers(app)

# Brand assets (e.g. the email logo) served from a stable public URL so
# transactional emails can reference https://<api>/static/logo.png. Path is
# resolved off this module so it works regardless of the process CWD.
app.mount(
    "/static",
    StaticFiles(directory=Path(__file__).parent / "static"),
    name="static",
)

app.include_router(media.router)  # serves /uploads/* (local file or S3 redirect)

app.include_router(auth.router, prefix="/auth")
app.include_router(sightings.router)
app.include_router(claims.router)
app.include_router(trait_changes.router)
app.include_router(cat_merges.router)
app.include_router(notifications.router)
app.include_router(cats.router)
app.include_router(explorer.router)
app.include_router(moderation.router)
app.include_router(exploration.router)
app.include_router(users.router)


@app.get("/health")
def health():
    return {"status": "ok"}
