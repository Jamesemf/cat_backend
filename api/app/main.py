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
from app.routers import auth, cats, claims, exploration, explorer, media, notifications, sightings
from app.services.reconcile import reconcile
from app.utils.rarity import compute_rarity_score

log = logging.getLogger(__name__)

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
    else:
        # Postgres (prod/Neon): create_all won't add a column to the existing
        # users table. ADD COLUMN IF NOT EXISTS is idempotent on Postgres.
        _conn.execute(_text(
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_verified BOOLEAN NOT NULL DEFAULT FALSE"
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


app = FastAPI(title="Cats API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten to specific origins in production
    allow_credentials=True,
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


@app.get("/health")
def health():
    return {"status": "ok"}
