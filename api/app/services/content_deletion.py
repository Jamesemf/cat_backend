"""Shared primitives for deleting user content.

Used by both single-post deletion and full account deletion. SQLite has no FK
cascades configured, so children are removed explicitly, and photo files are
only unlinked after the transaction commits (and only when nothing else still
references them).
"""

import logging

from sqlalchemy.orm import Session

from app.models.cat import Cat
from app.models.claim import CatClaim, ClaimPhoto
from app.models.explorer import ExplorerPost, PostComment, PostMeow, PostReport
from app.models.follow import CatFollow
from app.models.notification import Notification
from app.models.sighting import Sighting
from app.services.storage import UPLOADS_PREFIX, get_storage
from app.utils.rarity import compute_rarity_score

log = logging.getLogger(__name__)


def delete_post_dependents(db: Session, post_ids: list[int]) -> None:
    """Remove meows, comments, reports, and notifications hanging off posts."""
    if not post_ids:
        return
    db.query(PostMeow).filter(PostMeow.post_id.in_(post_ids)).delete(synchronize_session=False)
    db.query(PostComment).filter(PostComment.post_id.in_(post_ids)).delete(synchronize_session=False)
    db.query(PostReport).filter(PostReport.post_id.in_(post_ids)).delete(synchronize_session=False)
    db.query(Notification).filter(Notification.post_id.in_(post_ids)).delete(synchronize_session=False)


def delete_cat(db: Session, cat: Cat) -> list[str]:
    """Remove a cat and everything that references it.

    Returns claim-photo file paths to unlink after commit. Tagged Explorer
    posts survive with cat_id nulled.
    """
    file_paths: list[str] = []
    db.query(Notification).filter(Notification.cat_id == cat.id).delete(synchronize_session=False)
    db.query(CatFollow).filter(CatFollow.cat_id == cat.id).delete(synchronize_session=False)
    db.query(ExplorerPost).filter(ExplorerPost.cat_id == cat.id).update(
        {ExplorerPost.cat_id: None}, synchronize_session=False
    )
    claim_ids = [
        row[0] for row in db.query(CatClaim.id).filter(CatClaim.cat_id == cat.id).all()
    ]
    if claim_ids:
        file_paths.extend(
            row[0]
            for row in db.query(ClaimPhoto.photo_path)
            .filter(ClaimPhoto.claim_id.in_(claim_ids))
            .all()
        )
        db.query(ClaimPhoto).filter(ClaimPhoto.claim_id.in_(claim_ids)).delete(synchronize_session=False)
        db.query(CatClaim).filter(CatClaim.id.in_(claim_ids)).delete(synchronize_session=False)
    db.delete(cat)
    return file_paths


def recompute_cat_after_sighting_removal(
    db: Session, cat: Cat, removed_photo_path: str
) -> list[str]:
    """Repair a cat's counters after one of its sightings was deleted.

    Call after the sighting row has been deleted and flushed. If the cat has no
    sightings left it is kept (zeroed) when a verified claim exists, otherwise
    deleted entirely. Returns file paths to unlink after commit.
    """
    remaining = (
        db.query(Sighting)
        .filter(Sighting.cat_id == cat.id)
        .order_by(Sighting.spotted_at.desc())
        .all()
    )

    if remaining:
        latest = remaining[0]
        cat.sighting_count = len(remaining)
        cat.last_seen = latest.spotted_at
        cat.last_lat = latest.latitude
        cat.last_lng = latest.longitude
        cat.last_photo_path = latest.photo_path
        cat.rarity_score = compute_rarity_score(len(remaining), cat.last_seen)
        return []

    has_verified_claim = (
        db.query(CatClaim)
        .filter(CatClaim.cat_id == cat.id, CatClaim.status == "verified")
        .first()
        is not None
    )
    if has_verified_claim:
        # Owner-profile cat: keep it, but without sighting-derived data. The
        # last photo may be an owner upload that must survive — only clear it
        # when it pointed at the photo we just removed.
        cat.sighting_count = 0
        cat.rarity_score = 0.0
        cat.last_lat = None
        cat.last_lng = None
        if cat.last_photo_path == removed_photo_path:
            cat.last_photo_path = None
        return []

    return delete_cat(db, cat)


def safe_unlink(db: Session, paths: list[str]) -> None:
    """Delete stored objects that no surviving row references. Call after commit.

    Works against whatever storage backend is active (local disk or S3) — the
    photo_path values are backend-independent keys.
    """
    storage = get_storage()
    for raw in set(paths):
        if not raw:
            continue
        if not raw.startswith(f"{UPLOADS_PREFIX}/"):
            log.warning("Refusing to delete non-upload key: %s", raw)
            continue
        still_referenced = (
            db.query(Sighting.id).filter(Sighting.photo_path == raw).first()
            or db.query(ExplorerPost.id).filter(ExplorerPost.photo_path == raw).first()
            or db.query(Cat.id).filter(Cat.last_photo_path == raw).first()
            or db.query(ClaimPhoto.id).filter(ClaimPhoto.photo_path == raw).first()
        )
        if still_referenced:
            continue
        storage.delete(raw)
