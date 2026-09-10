"""Folding one cat's record into another.

Re-ID sometimes misses a match, so the same animal photographed twice becomes
two cats, each holding half a history. This puts them back together: every
sighting, post, claim, notification and trait correction moves to the survivor,
the survivor's denormalized counters are rebuilt from the combined set, and the
duplicate is deleted.

Shared by the admin endpoint (POST /cats/{id}/merge) and the moderation queue so
the two can never drift apart. Nothing here commits — the caller owns the
transaction, because deciding a merge request also stamps the request row and
both must land together or not at all.
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models.cat import Cat
from app.models.cat_merge import CatMergeRequest
from app.models.claim import CatClaim
from app.models.explorer import ExplorerPost
from app.models.notification import Notification
from app.models.sighting import Sighting
from app.models.trait_change import TraitChangeRequest
from app.models.user import User
from app.utils.rarity import compute_rarity_score

log = logging.getLogger(__name__)


def merge_cat_into(
    db: Session,
    source: Cat,
    target: Cat,
    *,
    skip_request_id: int | None = None,
) -> None:
    """Fold `source` into `target`, then delete `source`. Does not commit.

    Raises the same HTTPExceptions the admin endpoint always did, so both
    callers refuse the same merges for the same reasons.

    `skip_request_id` is the merge request being decided, if there is one: every
    *other* pending request naming the source is superseded, but that one is
    left alone for the caller to stamp as merged.
    """
    if source.id == target.id:
        raise HTTPException(status_code=400, detail="A cat can't be merged into itself.")

    source_id, target_id = source.id, target.id

    # Imported here rather than at module scope: demo_seed reaches
    # content_deletion, which reaches this module, so a top-level import would
    # close the loop.
    from app.services.demo_seed import demo_feature_key

    # The demo neighbourhood is torn down and rebuilt from DEMO_MARKER on every
    # startup, so merging across its boundary is destructive in both directions.
    # Keeping the demo cat would hide real sightings from every ordinary user
    # and then delete them with it at the next restart; keeping the real one
    # leaves the seed's own rows orphaned. Neither is worth allowing for a
    # neighbourhood that regenerates anyway.
    if demo_feature_key(source.features_json) or demo_feature_key(target.features_json):
        raise HTTPException(
            status_code=409,
            detail="One of these is demo content, which can't be merged.",
        )

    # Two verified owners can't collapse onto one cat (one verified claim per cat).
    src_verified = (
        db.query(CatClaim)
        .filter(CatClaim.cat_id == source_id, CatClaim.status == "verified")
        .count()
    )
    tgt_verified = (
        db.query(CatClaim)
        .filter(CatClaim.cat_id == target_id, CatClaim.status == "verified")
        .count()
    )
    if src_verified and tgt_verified:
        raise HTTPException(
            status_code=409,
            detail="Both cats have a verified owner; resolve ownership before merging.",
        )

    # Pending claims can't be carried across a merge. Reassigning one would put
    # photos of the source cat in front of a moderator judging them against the
    # target's record, and if either side is already verified, approving the
    # moved claim afterwards would collide with it.
    pending = (
        db.query(CatClaim)
        .filter(
            CatClaim.cat_id.in_((source_id, target_id)),
            CatClaim.status == "pending",
        )
        .count()
    )
    if pending:
        raise HTTPException(
            status_code=409,
            detail=(
                f"{pending} claim(s) on these cats are awaiting review; "
                "decide them in /moderation/claims before merging."
            ),
        )

    # Move sightings, posts and notifications wholesale. Sighting-originated posts
    # carry cat_id NULL and follow their sighting automatically, so only directly
    # tagged posts need reassigning here.
    db.query(Sighting).filter(Sighting.cat_id == source_id).update(
        {Sighting.cat_id: target_id}, synchronize_session=False
    )
    db.query(ExplorerPost).filter(ExplorerPost.cat_id == source_id).update(
        {ExplorerPost.cat_id: target_id}, synchronize_session=False
    )
    db.query(Notification).filter(Notification.cat_id == source_id).update(
        {Notification.cat_id: target_id}, synchronize_session=False
    )

    # At most one side is verified (checked above), so reassigning claims is safe.
    db.query(CatClaim).filter(CatClaim.cat_id == source_id).update(
        {CatClaim.cat_id: target_id}, synchronize_session=False
    )

    # Suggestions about the duplicate are suggestions about the survivor: same
    # animal, same traits. cat_id is NOT NULL, so leaving these behind doesn't
    # merely orphan them — Postgres refuses the delete below outright.
    db.query(TraitChangeRequest).filter(TraitChangeRequest.cat_id == source_id).update(
        {TraitChangeRequest.cat_id: target_id}, synchronize_session=False
    )

    detach_cat_from_merge_requests(db, source_id, skip_request_id=skip_request_id)
    rewrite_catalog_layouts(db, source_id, target_id)

    db.flush()

    # Recompute the target's denormalized aggregates from its combined sightings.
    target.sighting_count = db.query(Sighting).filter(Sighting.cat_id == target_id).count()
    latest = (
        db.query(Sighting)
        .filter(Sighting.cat_id == target_id)
        .order_by(Sighting.spotted_at.desc())
        .first()
    )
    if latest:
        target.last_seen = latest.spotted_at
        target.last_lat = latest.latitude
        target.last_lng = latest.longitude
        target.last_photo_path = latest.photo_path
        # The words follow the photo. Sighting reassignment already refreshes
        # these together (routers/sightings.py); without it here the profile
        # shows the duplicate's newest photo above the survivor's older vibes.
        if latest.vibes:
            target.vibes = latest.vibes

    # The combined record starts when the earlier of the two did. Left alone,
    # a survivor that was created later than the cat it absorbed claims a first
    # sighting that postdates half its own history.
    target.first_seen = min(_naive_utc(source.first_seen), _naive_utc(target.first_seen))

    target.rarity_score = compute_rarity_score(target.sighting_count, target.last_seen)

    db.delete(source)
    log.info("Merged cat %s into cat %s", source_id, target_id)


def detach_cat_from_merge_requests(
    db: Session, cat_id: int, *, skip_request_id: int | None = None
) -> None:
    """Let go of a cat that is about to stop existing. Does not commit.

    Pending requests naming it are `superseded` — nobody decided them, and there
    is nothing left to decide. Every reference to the id is then nulled, on
    decided rows too, so no column points at a deleted cat. The name snapshots
    taken at filing time are what keeps those rows readable afterwards.

    Used by both the merge (for the duplicate) and outright cat deletion.
    """
    pending = db.query(CatMergeRequest).filter(
        CatMergeRequest.status == "pending",
        (CatMergeRequest.cat_a_id == cat_id) | (CatMergeRequest.cat_b_id == cat_id),
    )
    if skip_request_id is not None:
        pending = pending.filter(CatMergeRequest.id != skip_request_id)
    pending.update(
        {
            CatMergeRequest.status: "superseded",
            CatMergeRequest.decided_at: datetime.now(timezone.utc),
        },
        synchronize_session=False,
    )

    for column in (
        CatMergeRequest.cat_a_id,
        CatMergeRequest.cat_b_id,
        CatMergeRequest.merged_into_cat_id,
    ):
        db.query(CatMergeRequest).filter(column == cat_id).update(
            {column: None}, synchronize_session=False
        )


def rewrite_catalog_layouts(db: Session, source_id: int, target_id: int) -> None:
    """Point people's Cat-a-log arrangements at the surviving cat. Does not commit.

    A cat id appears in `catalog_layout` twice over: as an int in `order`, and as
    a stringified key in `covers` / `frames` / `adjusts`. Left alone, a merged-away
    cat lingers in the arrangement of everyone who had ranked or framed it.

    The cover keys move honestly — a cover names one of that user's own
    sightings, and those sightings moved to the target with everything else.
    Where the user already had a setting for the target, theirs wins: it was a
    choice about the cat they are keeping.
    """
    # A LIKE on the id is a cheap prefilter, not the test — "12" matches a layout
    # mentioning cat 123. The real check is on the parsed structure below.
    candidates = (
        db.query(User)
        .filter(
            User.catalog_layout.isnot(None),
            User.catalog_layout.like(f"%{source_id}%"),
        )
        .all()
    )

    source_key, target_key = str(source_id), str(target_id)
    for user in candidates:
        try:
            data = json.loads(user.catalog_layout)
        except (TypeError, ValueError):
            continue
        if not isinstance(data, dict):
            continue

        touched = False

        raw_order = data.get("order")
        if isinstance(raw_order, list):
            ranked = [_as_int(entry) for entry in raw_order]
            if source_id in ranked:
                touched = True
                # The survivor takes the duplicate's place in the ranking,
                # unless it was already ranked somewhere of its own.
                already_ranked = target_id in ranked
                rebuilt: list[int] = []
                for value in ranked:
                    if value is None:
                        continue
                    if value == source_id:
                        if not already_ranked:
                            rebuilt.append(target_id)
                        continue
                    rebuilt.append(value)
                data["order"] = rebuilt

        for field in ("covers", "frames", "adjusts"):
            mapping = data.get(field)
            if not isinstance(mapping, dict) or source_key not in mapping:
                continue
            moved = mapping.pop(source_key)
            touched = True
            if target_key not in mapping:
                mapping[target_key] = moved

        if touched:
            user.catalog_layout = json.dumps(data)


def _naive_utc(value: datetime) -> datetime:
    """Drop the offset from an aware UTC timestamp.

    Our datetime columns store naive UTC, but a Cat built in the same session it
    is merged in still carries the aware value its default produced. Comparing
    the two raises, so flatten both before taking a minimum.
    """
    return value.replace(tzinfo=None) if value.tzinfo is not None else value


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
