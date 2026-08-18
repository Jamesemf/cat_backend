import json
import logging
import math
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.cat import Cat
from app.models.claim import CatClaim
from app.models.explorer import ExplorerPost, PostComment, PostMeow
from app.models.notification import Notification
from app.models.sighting import Sighting
from app.schemas.sighting import (
    FeedItem,
    MatchCandidate,
    MatchCheckRequest,
    MatchCheckResponse,
    PolaroidUpdate,
    SightingAnalysis,
    SightingAssign,
    SightingCommit,
    SightingOut,
)
from app.services.vision import VisionError, analyze_cat_photo, generate_cat_nickname
from sqlalchemy.orm import joinedload

from app.models.user import User
from app.services.auth_service import get_current_user, get_optional_user
from app.services.content_deletion import recompute_cat_after_sighting_removal, safe_unlink
from app.services.demo_seed import is_demo_user, relocate_demo_content, visible_cats_query, visible_sightings_query
from app.services.moderation import register_content_strike, sighting_has_hidden_post
from app.services.push import push_to_user
from app.services.rate_limit import enforce_daily_limit
from app.services.sighting_notifications import notify_sighting_audiences
from app.services.storage import UPLOADS_PREFIX, get_storage
from app.utils.matching import find_match_candidates, haversine_km
from app.utils.rarity import compute_rarity_score
from app.utils.upload import read_upload_capped, sanitize_image

log = logging.getLogger(__name__)

router = APIRouter(prefix="/sightings", tags=["sightings"])

# Abuse protection. The daily cap is enforced per authenticated user (per IP
# for anonymous callers) and backed by the daily_usage table, so it survives
# restarts and holds across instances. Applied at /analyze — the endpoint that
# spends a Claude vision call — and counts attempts, not just committed
# sightings, so failed/uncommitted analyses still draw down the allowance.
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_SIGHTINGS_PER_DAY = 10

# How many recent photos of each match candidate go out with /match-check. The
# client shows them as a swipeable pager so two look-alike cats can be told
# apart. A constant, not a query param: this endpoint is a POST with a JSON body
# and exactly one caller, so a knob would only be one more thing to clamp.
MATCH_CANDIDATE_PHOTOS = 8


@router.post("/analyze", response_model=SightingAnalysis)
async def analyze_photo(
    request: Request,
    photo: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    """Validate a photo before the user fills metadata.

    Saves the photo to disk and runs Claude vision. Aside from the rate-limit
    counter, does NOT touch the DB — the client must call POST /sightings to
    actually commit. Multi-cat, oversize, rate-limit, and vision-service
    errors are reported here so junk never enters the catalog.

    Orphan photos: if the client never commits, the saved file is left on
    disk. A future cleanup job can sweep uploads with no matching Sighting.
    """
    if current_user:
        limit_key = f"sightings:user:{current_user.id}"
    else:
        client_ip = request.client.host if request.client else "unknown"
        limit_key = f"sightings:ip:{client_ip}"
    enforce_daily_limit(
        db,
        limit_key,
        MAX_SIGHTINGS_PER_DAY,
        f"Daily limit of {MAX_SIGHTINGS_PER_DAY} photos reached. Come back tomorrow!",
    )

    contents = await read_upload_capped(
        photo,
        MAX_PHOTO_BYTES,
        f"Photo exceeds {MAX_PHOTO_BYTES // 1024 // 1024}MB limit.",
    )
    # Re-encode through Pillow: strips EXIF/GPS metadata and pins a safe
    # extension from the decoded format, so a JPEG/HTML polyglot can never be
    # served from our origin as text/html.
    clean, ext = sanitize_image(contents)

    storage = get_storage()
    photo_path = storage.put(clean, ext=ext)

    try:
        features = await analyze_cat_photo(clean)
    except VisionError as exc:
        storage.delete(photo_path)
        log.warning("Vision recognition failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Cat recognition is temporarily unavailable. Please try again.",
        )

    # Harmful content: never store the photo, and strike the account (two
    # warnings, banned on the third). Anonymous callers just get the rejection
    # — there's no account to strike.
    if not features.is_appropriate:
        storage.delete(photo_path)
        if current_user:
            detail = register_content_strike(db, current_user, features.inappropriate_reason)
        else:
            detail = "This photo contains content that isn't allowed."
        raise HTTPException(status_code=400, detail=detail)

    if features.cat_count > 1:
        storage.delete(photo_path)
        raise HTTPException(
            status_code=400,
            detail=f"Detected {features.cat_count} cats. Please photograph one cat at a time.",
        )

    return SightingAnalysis(
        photo_path=photo_path,
        is_cat=features.is_cat,
        cat_count=features.cat_count,
        not_cat_reason=features.not_cat_reason,
        primary_color=features.primary_color,
        secondary_color=features.secondary_color,
        pattern=features.pattern,
        fur_length=features.fur_length,
        eye_color=features.eye_color,
        body_size=features.body_size,
        breed=features.breed,
        features_json=features.to_json(),
    )


@router.post("/match-check", response_model=MatchCheckResponse)
def match_check(
    body: MatchCheckRequest,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    """Return Re-ID candidates for a potential sighting before committing.

    The client should call this after /analyze once the user is ready to submit,
    passing the GPS coordinates and extracted features. If candidates is
    non-empty, show the user a confirmation prompt and let them pick the matching
    cat (or decline). A cat is never linked automatically — even a near-perfect
    match is only assigned when the user confirms it. If candidates is empty,
    create a new cat.
    """
    features = {
        "primary_color": body.primary_color,
        "secondary_color": body.secondary_color,
        "pattern": body.pattern,
        "fur_length": body.fur_length,
        "eye_color": body.eye_color,
        "body_size": body.body_size,
        "breed": body.breed,
    }
    matches = find_match_candidates(
        db,
        body.latitude,
        body.longitude,
        features,
        # Scoped to the caller: the Apple review account has to be able to
        # recognise the seeded cats in its neighbourhood and add its own photo to
        # one, which is the whole point of them being there. Nobody else's
        # candidates change — visible_cats_query only widens for demo/admin.
        query=visible_cats_query(db.query(Cat), current_user),
    )

    # Recent photos per candidate, batched — one query for all of them rather
    # than one per cat. Copies the /cats/nearby carousel query (cats.list_cats_
    # nearby), including its hidden-post filter: one moderation decision has to
    # cover every surface the photo shows up on.
    #
    # Scoping is already handled by `ids` coming from a visible_cats_query
    # result above, so a demo cat can never reach the IN clause. The sighting
    # scope on top is belt-and-braces for admin merge, which reparents sightings
    # between cats and could otherwise leave demo rows on a visible cat.
    ids = [cat.id for cat, _ in matches]
    by_cat: dict[int, list[str]] = {}
    if ids:
        rows = (
            visible_sightings_query(
                db.query(Sighting.cat_id, Sighting.photo_path), current_user
            )
            .filter(
                Sighting.cat_id.in_(ids),
                Sighting.photo_path.isnot(None),
                ~sighting_has_hidden_post(),
            )
            # id breaks spotted_at ties so the order is stable across databases.
            .order_by(Sighting.spotted_at.desc(), Sighting.id.desc())
            .all()
        )
        for cid, path in rows:
            lst = by_cat.setdefault(cid, [])
            # The seed writes a cat's two sightings with one photo_path, and a
            # merge can leave real duplicates — the same picture twice under two
            # dots reads as a broken pager.
            if len(lst) < MATCH_CANDIDATE_PHOTOS and path not in lst:
                lst.append(path)

    candidates = [
        MatchCandidate(
            cat_id=cat.id,
            name=cat.name,
            breed=cat.breed,
            last_photo_path=cat.last_photo_path,
            # No last_photo_path fallback for an empty bucket, unlike
            # /cats/nearby: that endpoint lists cats registered by claim, which
            # legitimately have no sightings, whereas a candidate must have a
            # last_lat and only a sighting commit sets one. So empty here means
            # every photo was moderated away, and falling back would put the
            # hidden one straight back on screen.
            photos=by_cat.get(cat.id, []),
            last_seen=cat.last_seen,
            sighting_count=cat.sighting_count,
            confidence=score,
        )
        for cat, score in matches
    ]

    return MatchCheckResponse(candidates=candidates)


@router.post("", response_model=SightingOut, status_code=201)
def create_sighting(
    body: SightingCommit,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    """Commit a previously analyzed photo to the DB.

    Expects body.photo_path to point at a file already saved by /analyze.
    If body.cat_id is provided the sighting is linked to that existing cat and
    the cat's counters are updated. Otherwise a new cat record is created.
    """
    if not body.photo_path.startswith(f"{UPLOADS_PREFIX}/") or not get_storage().exists(
        body.photo_path
    ):
        raise HTTPException(status_code=400, detail="Invalid photo_path.")

    if body.cat_id is not None:
        # Same scoping as match-check above, so a candidate offered there can
        # actually be committed to.
        cat = visible_cats_query(db.query(Cat), current_user).filter(Cat.id == body.cat_id).first()
        if not cat:
            raise HTTPException(status_code=404, detail=f"Cat {body.cat_id} not found.")
        cat.sighting_count += 1
        cat.last_seen = datetime.now(timezone.utc)
        cat.last_lat = body.latitude
        cat.last_lng = body.longitude
        cat.last_photo_path = body.photo_path
        if body.vibes:
            cat.vibes = body.vibes
        cat.rarity_score = compute_rarity_score(cat.sighting_count, cat.last_seen)
    else:
        cat = Cat(
            name=generate_cat_nickname(
                breed=body.breed,
                primary_color=body.primary_color,
                secondary_color=body.secondary_color,
                pattern=body.pattern,
                body_size=body.body_size,
                vibes=body.vibes,
            ),
            breed=body.breed,
            last_lat=body.latitude,
            last_lng=body.longitude,
            last_photo_path=body.photo_path,
            vibes=body.vibes,
            sighting_count=1,
            is_cat=body.is_cat,
            primary_color=body.primary_color,
            secondary_color=body.secondary_color,
            pattern=body.pattern,
            fur_length=body.fur_length,
            eye_color=body.eye_color,
            body_size=body.body_size,
            features_json=body.features_json,
        )
        db.add(cat)
        db.flush()
        cat.rarity_score = compute_rarity_score(1, cat.last_seen)

    sighting = Sighting(
        cat_id=cat.id,
        user_id=current_user.id if current_user else None,
        photo_path=body.photo_path,
        latitude=body.latitude,
        longitude=body.longitude,
        # Derive the public spotter label from the authenticated account's
        # display name only — never the client-supplied value (spoofable) and
        # never the email (which would leak the address on the public feed).
        spotter_name=(current_user.display_name if current_user else None),
        breed_description=body.breed,
        vibes=body.vibes,
        is_cat=body.is_cat,
        primary_color=body.primary_color,
        secondary_color=body.secondary_color,
        pattern=body.pattern,
        fur_length=body.fur_length,
        eye_color=body.eye_color,
        body_size=body.body_size,
        features_json=body.features_json,
        frame_id=body.frame_id,
        photo_adjust=json.dumps(body.photo_adjust.model_dump()) if body.photo_adjust else None,
        caption=body.caption,
    )
    db.add(sighting)
    db.commit()
    db.refresh(sighting)

    # Mirror the sighting into the Explorer feed. Sighting photos already passed
    # the is_cat gate in /sightings/analyze, so no extra moderation pass here.
    db.add(
        ExplorerPost(
            user_id=sighting.user_id,
            sighting_id=sighting.id,
            photo_path=sighting.photo_path,
            caption=sighting.vibes,
            latitude=sighting.latitude,
            longitude=sighting.longitude,
        )
    )
    db.commit()

    # Notify the verified owner (if any) that their cat was spotted. The
    # notification row is written in its own transaction so it is never lost;
    # the push goes via a background task so the response isn't delayed.
    submitter_id = current_user.id if current_user else None
    owner_id = None
    if body.cat_id is not None:
        claim = (
            db.query(CatClaim)
            .filter(CatClaim.cat_id == cat.id, CatClaim.status == "verified")
            .first()
        )
        if claim and claim.user_id != submitter_id:
            owner_id = claim.user_id
            title = f"{cat.name or 'Your cat'} was spotted!"
            notif_body = (
                f"Someone just logged a sighting of {cat.name or 'your cat'}. Tap to see where."
            )
            db.add(
                Notification(
                    user_id=claim.user_id,
                    type="sighting",
                    title=title,
                    body=notif_body,
                    cat_id=cat.id,
                    sighting_id=sighting.id,
                )
            )
            db.commit()
            background_tasks.add_task(
                push_to_user,
                claim.user_id,
                title,
                notif_body,
                {"cat_id": cat.id, "sighting_id": sighting.id},
            )

    # Fan out to explorers of nearby tiles. The submitter and the owner
    # (already notified above) are excluded.
    background_tasks.add_task(
        notify_sighting_audiences,
        sighting.id,
        cat.id,
        cat.name,
        sighting.latitude,
        sighting.longitude,
        body.cat_id is None,
        {uid for uid in (submitter_id, owner_id) if uid is not None},
    )

    return sighting


def _visible_sightings(query, current_user: User | None):
    """Drop sightings whose mirrored post was moderated away.

    Admins still see them (so they can review in context) and so does the author,
    who is told their spot is hidden rather than left wondering where it went.
    """
    if current_user is None:
        return visible_sightings_query(query, None).filter(~sighting_has_hidden_post())
    if current_user.is_admin:
        return query
    return visible_sightings_query(query, current_user).filter(
        or_(~sighting_has_hidden_post(), Sighting.user_id == current_user.id)
    )


def _serialize_feed_items(
    db: Session, sightings: list[Sighting], current_user: User | None
) -> list[FeedItem]:
    """Enrich sightings with their cat, spotter and mirrored-post interaction state.

    Counts are batched across the whole list (no N+1), so this serves both the
    feed and single-sighting lookups from the same code path.
    """
    # Gather every photo for each cat in one query (most recent first) so a feed
    # card can show a swipeable carousel without an N+1 fetch per card.
    cat_ids = {s.cat_id for s in sightings if s.cat_id is not None}
    photos_by_cat: dict[int, list[str]] = {}
    if cat_ids:
        rows = (
            visible_sightings_query(db.query(Sighting.cat_id, Sighting.photo_path), current_user)
            .filter(Sighting.cat_id.in_(cat_ids), ~sighting_has_hidden_post())
            .order_by(Sighting.spotted_at.desc())
            .all()
        )
        for cat_id, photo_path in rows:
            photos_by_cat.setdefault(cat_id, []).append(photo_path)

    # Link each sighting to the Explorer post it was mirrored into, then batch
    # the meow/comment counts for those posts (no N+1) so every card can show —
    # and drive — likes, comments and reports.
    sighting_ids = [s.id for s in sightings]
    post_by_sighting: dict[int, ExplorerPost] = {}
    meow_counts: dict[int, int] = {}
    comment_counts: dict[int, int] = {}
    my_meows: set[int] = set()
    if sighting_ids:
        posts = (
            db.query(ExplorerPost)
            .filter(ExplorerPost.sighting_id.in_(sighting_ids))
            .all()
        )
        post_by_sighting = {p.sighting_id: p for p in posts}
        post_ids = [p.id for p in posts]
        if post_ids:
            meow_counts = dict(
                db.query(PostMeow.post_id, func.count(PostMeow.id))
                .filter(PostMeow.post_id.in_(post_ids))
                .group_by(PostMeow.post_id)
                .all()
            )
            comment_counts = dict(
                db.query(PostComment.post_id, func.count(PostComment.id))
                .filter(PostComment.post_id.in_(post_ids))
                .group_by(PostComment.post_id)
                .all()
            )
            if current_user:
                my_meows = {
                    row[0]
                    for row in db.query(PostMeow.post_id)
                    .filter(PostMeow.post_id.in_(post_ids), PostMeow.user_id == current_user.id)
                    .all()
                }

    items: list[FeedItem] = []
    for s in sightings:
        post = post_by_sighting.get(s.id)
        items.append(
            FeedItem(
                id=s.id,
                photo_path=s.photo_path,
                photos=photos_by_cat.get(s.cat_id, [s.photo_path]) if s.cat_id is not None else [s.photo_path],
                latitude=s.latitude,
                longitude=s.longitude,
                spotted_at=s.spotted_at,
                spotter_name=(s.user.display_name if s.user else None),
                breed_description=s.breed_description,
                vibes=s.vibes,
                primary_color=s.primary_color,
                secondary_color=s.secondary_color,
                pattern=s.pattern,
                fur_length=s.fur_length,
                eye_color=s.eye_color,
                body_size=s.body_size,
                cat_id=s.cat_id,
                cat_name=s.cat.name if s.cat else None,
                cat_rarity_score=s.cat.rarity_score if s.cat else None,
                cat_sighting_count=s.cat.sighting_count if s.cat else None,
                spotter_emoji=s.user.avatar_emoji if s.user else None,
                spotter_id=s.user.id if s.user else None,
                post_id=post.id if post else None,
                meow_count=meow_counts.get(post.id, 0) if post else 0,
                comment_count=comment_counts.get(post.id, 0) if post else 0,
                meowed_by_me=post.id in my_meows if post else False,
                is_mine=bool(current_user and post and post.user_id == current_user.id),
                hidden=bool(post and post.hidden_at is not None),
                frame_id=s.frame_id,
                photo_adjust=s.photo_adjust,
                caption=s.caption,
            )
        )
    return items


@router.get("/feed", response_model=list[FeedItem])
def get_feed(
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float = 10.0,
    limit: int = 30,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    """Recent sightings enriched with their cat's name and rarity score.

    When lat/lng are provided, only sightings within radius_km are returned.
    Falls back to the global feed when location is unavailable. Each item also
    carries the interaction state of the Explorer post it was mirrored into, so
    the Neighbourhood feed can like/comment/report each spot.
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    if is_demo_user(current_user):
        relocate_demo_content(db, lat, lng)
    query = db.query(Sighting).options(joinedload(Sighting.cat), joinedload(Sighting.user))

    # Moderated-away spots drop out of the feed.
    query = _visible_sightings(query, current_user)

    if lat is not None and lng is not None:
        lat_delta = radius_km / 111.0
        lng_delta = radius_km / max(111.0 * math.cos(math.radians(lat)), 0.001)
        query = query.filter(
            Sighting.latitude.between(lat - lat_delta, lat + lat_delta),
            Sighting.longitude.between(lng - lng_delta, lng + lng_delta),
        )

    sightings = (
        query
        .order_by(Sighting.spotted_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    if lat is not None and lng is not None:
        sightings = [
            s for s in sightings
            if haversine_km(lat, lng, s.latitude, s.longitude) <= radius_km
        ]

    return _serialize_feed_items(db, sightings, current_user)


@router.get("/{sighting_id}", response_model=FeedItem)
def get_sighting(
    sighting_id: int,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    """One sighting by id, in the same shape the feed serves.

    Deliberately unfiltered by location: this backs the sighting screen reached
    from a cat's profile, where the spot can sit far outside the viewer's
    neighbourhood. Routing that through the feed is what used to leave the tap
    going nowhere.

    Declared after /feed — a bare int path param would otherwise capture it.
    """
    # Same moderation rule as the feed, so a withheld photo isn't reachable by
    # guessing an id. A hidden spot 404s rather than 403ing — no point telling a
    # stranger it exists.
    query = db.query(Sighting).options(joinedload(Sighting.cat), joinedload(Sighting.user))
    sighting = _visible_sightings(query, current_user).filter(Sighting.id == sighting_id).first()
    if sighting is None:
        raise HTTPException(status_code=404, detail="Sighting not found")

    return _serialize_feed_items(db, [sighting], current_user)[0]


@router.patch("/{sighting_id}/polaroid", response_model=SightingOut)
def update_polaroid(
    sighting_id: int,
    body: PolaroidUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Update the spotter's polaroid keepsake (frame, framing, caption).

    Owner-only: the customization shows to everyone in the Nearby feed, so only
    the user who logged the sighting may change it. Only the fields present in
    the request body are applied (a sent null clears that field).
    """
    sighting = db.query(Sighting).filter(Sighting.id == sighting_id).first()
    if not sighting:
        raise HTTPException(status_code=404, detail="Sighting not found.")
    if sighting.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your sighting.")

    fields = body.model_dump(exclude_unset=True)
    if "frame_id" in fields:
        sighting.frame_id = fields["frame_id"]
    if "caption" in fields:
        sighting.caption = fields["caption"]
    if "photo_adjust" in fields:
        sighting.photo_adjust = (
            json.dumps(body.photo_adjust.model_dump()) if body.photo_adjust else None
        )

    db.commit()
    db.refresh(sighting)
    return sighting


@router.get("", response_model=list[SightingOut])
def list_sightings(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    return (
        db.query(Sighting)
        .order_by(Sighting.spotted_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.patch("/{sighting_id}", response_model=SightingOut)
def assign_cat(
    sighting_id: int,
    body: SightingAssign,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    sighting = db.query(Sighting).filter(Sighting.id == sighting_id).first()
    if not sighting:
        raise HTTPException(status_code=404, detail="Sighting not found")

    # Only the user who logged the sighting may reassign which cat it belongs to.
    if sighting.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="Not your sighting")

    cat = visible_cats_query(db.query(Cat), current_user).filter(Cat.id == body.cat_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Cat not found")

    # Remember the cat we're moving away from so its aggregates can be repaired.
    old_cat_id = sighting.cat_id
    removed_photo_path = sighting.photo_path

    sighting.cat_id = cat.id
    db.flush()  # so the row above is counted exactly once below
    cat.sighting_count = db.query(Sighting).filter(Sighting.cat_id == cat.id).count()
    cat.last_seen = sighting.spotted_at
    cat.last_lat = sighting.latitude
    cat.last_lng = sighting.longitude
    cat.rarity_score = compute_rarity_score(cat.sighting_count, cat.last_seen)
    cat.last_photo_path = sighting.photo_path
    if sighting.vibes:
        cat.vibes = sighting.vibes

    # Repair the previous cat: recompute its counters, or retire it if it has no
    # sightings and no verified claim left. Without this the old cat keeps an
    # inflated sighting_count, a stale last_photo_path, and a stale rarity.
    files_to_unlink: list[str] = []
    if old_cat_id is not None and old_cat_id != cat.id:
        old_cat = db.query(Cat).filter(Cat.id == old_cat_id).first()
        if old_cat:
            files_to_unlink = recompute_cat_after_sighting_removal(
                db, old_cat, removed_photo_path
            )

    db.commit()
    safe_unlink(db, files_to_unlink)
    db.refresh(sighting)
    return sighting
