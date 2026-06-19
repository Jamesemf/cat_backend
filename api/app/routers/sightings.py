import logging
import math
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.cat import Cat
from app.models.claim import CatClaim
from app.models.explorer import ExplorerPost
from app.models.follow import CatFollow
from app.models.notification import Notification
from app.models.sighting import Sighting
from app.schemas.sighting import (
    FeedItem,
    MatchCandidate,
    MatchCheckRequest,
    MatchCheckResponse,
    SightingAnalysis,
    SightingAssign,
    SightingCommit,
    SightingOut,
)
from app.services.vision import VisionError, analyze_cat_photo, generate_cat_nickname
from sqlalchemy.orm import joinedload

from app.models.user import User
from app.services.auth_service import get_optional_user
from app.services.push import push_to_user
from app.services.storage import UPLOADS_PREFIX, get_storage
from app.utils.matching import find_match_candidates, haversine_km
from app.utils.rarity import compute_rarity_score

log = logging.getLogger(__name__)

router = APIRouter(prefix="/sightings", tags=["sightings"])

# Abuse protection. In-memory only — resets on server restart and does not
# survive multi-worker deployments. Swap for Redis when going to production.
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_SIGHTINGS_PER_IP_PER_DAY = 50
_rate_limit_buckets: dict[str, tuple[str, int]] = {}


def _enforce_rate_limit(client_ip: str) -> None:
    today = date.today().isoformat()
    bucket_date, count = _rate_limit_buckets.get(client_ip, (today, 0))
    if bucket_date != today:
        count = 0
    if count >= MAX_SIGHTINGS_PER_IP_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit of {MAX_SIGHTINGS_PER_IP_PER_DAY} sightings reached for this client.",
        )
    _rate_limit_buckets[client_ip] = (today, count + 1)


@router.post("/analyze", response_model=SightingAnalysis)
async def analyze_photo(
    request: Request,
    photo: UploadFile = File(...),
):
    """Validate a photo before the user fills metadata.

    Saves the photo to disk and runs Claude vision. Does NOT touch the DB —
    the client must call POST /sightings to actually commit. Multi-cat,
    oversize, rate-limit, and vision-service errors are reported here so
    junk never enters the catalog.

    Orphan photos: if the client never commits, the saved file is left on
    disk. A future cleanup job can sweep uploads with no matching Sighting.
    """
    client_ip = request.client.host if request.client else "unknown"
    _enforce_rate_limit(client_ip)

    contents = await photo.read()
    if len(contents) > MAX_PHOTO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Photo exceeds {MAX_PHOTO_BYTES // 1024 // 1024}MB limit.",
        )

    storage = get_storage()
    ext = Path(photo.filename).suffix if photo.filename else ".jpg"
    photo_path = storage.put(contents, ext=ext)

    try:
        features = await analyze_cat_photo(contents)
    except VisionError as exc:
        storage.delete(photo_path)
        log.warning("Vision recognition failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Cat recognition is temporarily unavailable. Please try again.",
        )

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
def match_check(body: MatchCheckRequest, db: Session = Depends(get_db)):
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
    matches = find_match_candidates(db, body.latitude, body.longitude, features)

    candidates = [
        MatchCandidate(
            cat_id=cat.id,
            name=cat.name,
            breed=cat.breed,
            last_photo_path=cat.last_photo_path,
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
        cat = db.query(Cat).filter(Cat.id == body.cat_id).first()
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
        spotter_name=body.spotter_name,
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
    if body.cat_id is not None:
        submitter_id = current_user.id if current_user else None
        notified_ids: set[int] = set()

        claim = (
            db.query(CatClaim)
            .filter(CatClaim.cat_id == cat.id, CatClaim.status == "verified")
            .first()
        )
        if claim and claim.user_id != submitter_id:
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
            notified_ids.add(claim.user_id)

        # Followers get the news too — excluding the spotter and the owner,
        # who already has the richer notification above.
        follower_ids = [
            row[0]
            for row in db.query(CatFollow.user_id).filter(CatFollow.cat_id == cat.id).all()
            if row[0] != submitter_id and row[0] not in notified_ids
        ]
        if follower_ids:
            title = f"{cat.name or 'A cat you follow'} was spotted!"
            notif_body = (
                f"{cat.name or 'A cat you follow'} was just seen again. Tap to take a look."
            )
            for uid in follower_ids:
                db.add(
                    Notification(
                        user_id=uid,
                        type="followed_sighting",
                        title=title,
                        body=notif_body,
                        cat_id=cat.id,
                        sighting_id=sighting.id,
                    )
                )
            db.commit()
            for uid in follower_ids:
                background_tasks.add_task(
                    push_to_user,
                    uid,
                    title,
                    notif_body,
                    {"cat_id": cat.id, "sighting_id": sighting.id},
                )

    return sighting


@router.get("/feed", response_model=list[FeedItem])
def get_feed(
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float = 10.0,
    limit: int = 30,
    offset: int = 0,
    db: Session = Depends(get_db),
):
    """Recent sightings enriched with their cat's name and rarity score.

    When lat/lng are provided, only sightings within radius_km are returned.
    Falls back to the global feed when location is unavailable.
    """
    query = db.query(Sighting).options(joinedload(Sighting.cat), joinedload(Sighting.user))

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

    # Gather every photo for each cat in one query (most recent first) so a feed
    # card can show a swipeable carousel without an N+1 fetch per card.
    cat_ids = {s.cat_id for s in sightings if s.cat_id is not None}
    photos_by_cat: dict[int, list[str]] = {}
    if cat_ids:
        rows = (
            db.query(Sighting.cat_id, Sighting.photo_path)
            .filter(Sighting.cat_id.in_(cat_ids))
            .order_by(Sighting.spotted_at.desc())
            .all()
        )
        for cat_id, photo_path in rows:
            photos_by_cat.setdefault(cat_id, []).append(photo_path)

    return [
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
        )
        for s in sightings
    ]


@router.get("", response_model=list[SightingOut])
def list_sightings(limit: int = 50, offset: int = 0, db: Session = Depends(get_db)):
    return (
        db.query(Sighting)
        .order_by(Sighting.spotted_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )


@router.patch("/{sighting_id}", response_model=SightingOut)
def assign_cat(sighting_id: int, body: SightingAssign, db: Session = Depends(get_db)):
    sighting = db.query(Sighting).filter(Sighting.id == sighting_id).first()
    if not sighting:
        raise HTTPException(status_code=404, detail="Sighting not found")

    cat = db.query(Cat).filter(Cat.id == body.cat_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Cat not found")

    sighting.cat_id = cat.id
    cat.sighting_count = db.query(Sighting).filter(Sighting.cat_id == cat.id).count() + 1
    cat.last_seen = sighting.spotted_at
    cat.last_lat = sighting.latitude
    cat.last_lng = sighting.longitude
    cat.rarity_score = compute_rarity_score(cat.sighting_count, cat.last_seen)
    cat.last_photo_path = sighting.photo_path
    if sighting.vibes:
        cat.vibes = sighting.vibes

    db.commit()
    db.refresh(sighting)
    return sighting
