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
from app.routers import auth, cats, claims, exploration, explorer, media, notifications, sightings, users
from app.services.reconcile import reconcile
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
        _ev_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(email_verifications)")).fetchall()]
        if _ev_cols and "attempts" not in _ev_cols:
            _conn.execute(_text("ALTER TABLE email_verifications ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"))
            _conn.commit()
        _pr_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(password_resets)")).fetchall()]
        if _pr_cols and "attempts" not in _pr_cols:
            _conn.execute(_text("ALTER TABLE password_resets ADD COLUMN attempts INTEGER NOT NULL DEFAULT 0"))
            _conn.commit()
        _c_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(cat_claims)")).fetchall()]
        if _c_cols and "real_name" not in _c_cols:
            _conn.execute(_text("ALTER TABLE cat_claims ADD COLUMN real_name TEXT"))
            _conn.commit()
        _n_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(notifications)")).fetchall()]
        if _n_cols and "post_id" not in _n_cols:
            _conn.execute(_text("ALTER TABLE notifications ADD COLUMN post_id INTEGER REFERENCES explorer_posts(id)"))
            _conn.commit()
        _e_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(explorer_posts)")).fetchall()]
        if _e_cols and "cat_id" not in _e_cols:
            _conn.execute(_text("ALTER TABLE explorer_posts ADD COLUMN cat_id INTEGER REFERENCES cats(id)"))
            _conn.commit()
        _et_cols = [r[1] for r in _conn.execute(_text("PRAGMA table_info(explored_tiles)")).fetchall()]
        if _et_cols and "is_home" not in _et_cols:
            _conn.execute(_text("ALTER TABLE explored_tiles ADD COLUMN is_home BOOLEAN NOT NULL DEFAULT 0"))
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [asyncio.create_task(_rarity_recompute_loop())]
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
app.include_router(notifications.router)
app.include_router(cats.router)
app.include_router(explorer.router)
app.include_router(exploration.router)
app.include_router(users.router)


@app.get("/health")
def health():
    return {"status": "ok"}
