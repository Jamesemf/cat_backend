import logging
import math
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session
from sqlalchemy import distinct, func

from app.db.session import get_db
from app.models.cat import Cat
from app.models.claim import CatClaim
from app.models.explorer import ExplorerPost
from app.models.exploration import ExploredTile
from app.models.notification import Notification
from app.models.sighting import Sighting
from app.models.user import User
from collections import Counter

from app.routers.claims import owner_card_for_cat
from app.schemas.cat import (
    CatCreate,
    CatMergeRequest,
    CatNearby,
    CatOut,
    CatWithSightings,
    CountItem,
    GlobalStats,
    LeaderboardRow,
    Leaderboards,
    MyPhotoOut,
    TerritoryOut,
    TopCat,
)
from app.schemas.claim import INDOOR_OUTDOOR_VALUES
from app.services.auth_service import get_current_user, require_admin
from app.services.claim_verification import MAX_CLAIM_ATTEMPTS_PER_DAY, MAX_PHOTOS
from app.services.moderation import register_content_strike
from app.services.storage import get_storage
from app.services.vision import VisionError, analyze_cat_photo
from app.utils.matching import haversine_km
from app.utils.rarity import compute_rarity_score
from app.utils.territory import build_territory_geojson
from app.utils.upload import read_upload_capped, sanitize_image

router = APIRouter(prefix="/cats", tags=["cats"])

log = logging.getLogger(__name__)

MAX_PHOTO_BYTES = 5 * 1024 * 1024


@router.get("", response_model=list[CatOut])
def list_cats(limit: int = 100, db: Session = Depends(get_db)):
    limit = max(1, min(limit, 200))
    return db.query(Cat).order_by(Cat.last_seen.desc()).limit(limit).all()


@router.get("/nearby", response_model=list[CatNearby])
def list_cats_nearby(
    limit: int = 100,
    photos: int = 8,
    lat: float | None = None,
    lng: float | None = None,
    radius_km: float = 3.0,
    db: Session = Depends(get_db),
):
    """Cats for the onboarding picker, each with up to `photos` recent photos so a
    user can recognise their own cat by sight rather than by an assigned nickname.

    When lat/lng are provided, only cats last seen within `radius_km` are returned,
    nearest first — so the picker shows cats the user could plausibly own rather
    than whatever was most recently spotted anywhere in the world. Without a
    location we fall back to most-recently-seen ordering.
    """
    if lat is not None and lng is not None:
        # Bounding-box prefilter in SQL (SQLite has no PostGIS), then a precise
        # Haversine check — mirrors utils.matching.find_match_candidates.
        lat_delta = radius_km / 111.0
        lng_delta = radius_km / max(111.0 * math.cos(math.radians(lat)), 0.001)
        prefiltered = (
            db.query(Cat)
            .filter(
                Cat.last_lat.isnot(None),
                Cat.last_lng.isnot(None),
                Cat.last_lat.between(lat - lat_delta, lat + lat_delta),
                Cat.last_lng.between(lng - lng_delta, lng + lng_delta),
            )
            .all()
        )
        within = [
            (c, haversine_km(lat, lng, c.last_lat, c.last_lng)) for c in prefiltered
        ]
        within = [(c, d) for c, d in within if d <= radius_km]
        within.sort(key=lambda cd: cd[1])
        cats = [c for c, _ in within[:limit]]
    else:
        cats = db.query(Cat).order_by(Cat.last_seen.desc()).limit(limit).all()
    ids = [c.id for c in cats]
    by_cat: dict[int, list[str]] = {}
    if ids:
        rows = (
            db.query(Sighting.cat_id, Sighting.photo_path)
            .filter(Sighting.cat_id.in_(ids), Sighting.photo_path.isnot(None))
            .order_by(Sighting.spotted_at.desc())
            .all()
        )
        for cid, path in rows:
            lst = by_cat.setdefault(cid, [])
            if len(lst) < photos:
                lst.append(path)
    out: list[CatNearby] = []
    for c in cats:
        item = CatNearby.model_validate(c)
        item.photos = by_cat.get(c.id) or ([c.last_photo_path] if c.last_photo_path else [])
        out.append(item)
    return out


@router.post("", response_model=CatOut, status_code=201)
def create_cat(
    body: CatCreate,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    now = datetime.now(timezone.utc)
    cat = Cat(name=body.name, breed=body.breed, first_seen=now, last_seen=now)
    db.add(cat)
    db.commit()
    db.refresh(cat)
    return cat


@router.post("/register", response_model=CatOut, status_code=201)
async def register_cat(
    photos: list[UploadFile] = File(...),
    name: str = Form(...),
    likes_petting: bool = Form(...),
    accepts_treats: bool = Form(...),
    age_years: int = Form(...),
    indoor_outdoor: str = Form(...),
    fun_fact: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Register a brand-new cat the user owns.

    Unlike a claim (which matches photos against an existing cat), this creates
    the cat from the owner's own photo and marks them as its verified owner. The
    cover photo is run through vision to confirm it's a cat and to seed the cat's
    visual features, so future sightings by other people can be matched back to
    this profile. The cat has no sightings yet, so no rarity and no map pin.
    """
    name = name.strip()
    if not name or len(name) > 40:
        raise HTTPException(status_code=400, detail="Give your cat a name (40 characters or fewer).")
    if not (1 <= len(photos) <= MAX_PHOTOS):
        raise HTTPException(status_code=400, detail=f"Add between 1 and {MAX_PHOTOS} photos.")
    if indoor_outdoor not in INDOOR_OUTDOOR_VALUES:
        raise HTTPException(status_code=400, detail="indoor_outdoor must be indoor, outdoor or both.")
    if not (0 <= age_years <= 30):
        raise HTTPException(status_code=400, detail="age_years must be between 0 and 30.")
    if fun_fact and len(fun_fact) > 280:
        raise HTTPException(status_code=400, detail="fun_fact must be 280 characters or fewer.")

    # Share the claim abuse budget so registration can't be used to spam cats.
    day_cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    attempts_today = (
        db.query(CatClaim)
        .filter(CatClaim.user_id == current_user.id, CatClaim.created_at >= day_cutoff)
        .count()
    )
    if attempts_today >= MAX_CLAIM_ATTEMPTS_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit of {MAX_CLAIM_ATTEMPTS_PER_DAY} claim/registration attempts reached.",
        )

    cover = photos[0]
    contents = await read_upload_capped(
        cover,
        MAX_PHOTO_BYTES,
        f"Each photo must be under {MAX_PHOTO_BYTES // 1024 // 1024}MB.",
    )
    # Strip metadata (EXIF/GPS) and pin a safe extension from the decoded format.
    clean, ext = sanitize_image(contents)

    try:
        features = await analyze_cat_photo(clean)
    except VisionError as exc:
        log.warning("Register vision failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Cat recognition is temporarily unavailable. Please try again.",
        )

    # Harmful content earns a strike (two warnings, banned on the third).
    if not features.is_appropriate:
        detail = register_content_strike(db, current_user, features.inappropriate_reason)
        raise HTTPException(status_code=400, detail=detail)

    if not features.is_cat:
        raise HTTPException(
            status_code=400,
            detail="That photo doesn't look like a cat. Try a clear photo of your cat.",
        )
    if features.cat_count > 1:
        raise HTTPException(status_code=400, detail="Please use a photo of just your cat.")

    photo_path = get_storage().put(clean, ext=ext)

    now = datetime.now(timezone.utc)
    cat = Cat(
        name=name,
        breed=features.breed,
        last_photo_path=photo_path,
        sighting_count=0,
        rarity_score=0.0,
        first_seen=now,
        last_seen=now,
        is_cat=features.is_cat,
        primary_color=features.primary_color,
        secondary_color=features.secondary_color,
        pattern=features.pattern,
        fur_length=features.fur_length,
        eye_color=features.eye_color,
        body_size=features.body_size,
        features_json=features.to_json(),
    )
    db.add(cat)
    db.flush()
    db.add(
        CatClaim(
            cat_id=cat.id,
            user_id=current_user.id,
            status="verified",
            real_name=name,
            likes_petting=likes_petting,
            accepts_treats=accepts_treats,
            age_years=age_years,
            fun_fact=fun_fact.strip() if fun_fact else None,
            indoor_outdoor=indoor_outdoor,
            created_at=now,
            decided_at=now,
        )
    )
    # Same inbox record the claim flow leaves — registering makes you the
    # verified owner directly.
    db.add(
        Notification(
            user_id=current_user.id,
            type="claim_verified",
            title=f"You're now {cat.name}'s verified owner",
            body="You'll be notified whenever they're spotted.",
            cat_id=cat.id,
        )
    )
    db.commit()
    db.refresh(cat)
    return cat


@router.post("/recompute-rarity", status_code=200)
def recompute_all_rarity(
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """Recalculate rarity_score for every cat based on current sighting counts."""
    cats = db.query(Cat).all()
    for cat in cats:
        cat.rarity_score = compute_rarity_score(cat.sighting_count, cat.last_seen)
    db.commit()
    return {"updated": len(cats)}


@router.post("/{source_id}/merge", response_model=CatOut)
def merge_cats(
    source_id: int,
    body: CatMergeRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_admin),
):
    """Merge a duplicate cat (the one in the path) into a target, then delete it.

    When a missed Re-ID match creates a second cat for the same animal, this
    folds the duplicate back in: every sighting, Explorer post, claim and
    notification moves to the target, the target's aggregates are recomputed,
    and the source cat is removed. Requires a signed-in user; this is a
    maintenance action that should be restricted to admins before production.
    """
    target_id = body.target_id
    if source_id == target_id:
        raise HTTPException(status_code=400, detail="A cat can't be merged into itself.")

    source = db.query(Cat).filter(Cat.id == source_id).first()
    target = db.query(Cat).filter(Cat.id == target_id).first()
    if not source or not target:
        raise HTTPException(status_code=404, detail="Cat not found.")

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
    target.rarity_score = compute_rarity_score(target.sighting_count, target.last_seen)

    db.delete(source)
    db.commit()
    db.refresh(target)
    return target


@router.get("/stats", response_model=GlobalStats)
def get_global_stats(db: Session = Depends(get_db)):
    total_cats = db.query(Cat).count()
    total_sightings = db.query(Sighting).count()

    cats = db.query(Cat).all()

    def top_n(values: list[str | None], n: int = 5) -> list[CountItem]:
        counts = Counter(v for v in values if v)
        return [CountItem(label=label, count=count) for label, count in counts.most_common(n)]

    breeds      = top_n([c.breed         for c in cats])
    colors      = top_n([c.primary_color for c in cats])
    patterns    = top_n([c.pattern       for c in cats])
    fur_lengths = top_n([c.fur_length    for c in cats])

    most_spotted_cat = (
        db.query(Cat).order_by(Cat.sighting_count.desc()).first()
    )
    most_spotted = (
        TopCat(
            id=most_spotted_cat.id,
            name=most_spotted_cat.name,
            sighting_count=most_spotted_cat.sighting_count,
            last_photo_path=most_spotted_cat.last_photo_path,
        )
        if most_spotted_cat else None
    )

    return GlobalStats(
        total_cats=total_cats,
        total_sightings=total_sightings,
        top_breeds=breeds,
        top_colors=colors,
        top_patterns=patterns,
        top_fur_lengths=fur_lengths,
        most_spotted=most_spotted,
    )


@router.get("/stats/leaderboard", response_model=Leaderboards)
def get_leaderboard(db: Session = Depends(get_db)):
    """Public top-20 rankings of spotters by total sightings and tiles explored."""

    def rank_by(count_col, join_model, extra_filter=None) -> list[LeaderboardRow]:
        q = (
            db.query(
                User.id,
                User.display_name,
                User.avatar_emoji,
                func.count(count_col).label("value"),
            )
            .join(join_model, join_model.user_id == User.id)
        )
        if extra_filter is not None:
            q = q.filter(extra_filter)
        rows = (
            q.group_by(User.id)
            .order_by(func.count(count_col).desc())
            .limit(20)
            .all()
        )
        return [
            LeaderboardRow(
                user_id=r.id,
                display_name=r.display_name,
                avatar_emoji=r.avatar_emoji,
                value=r.value,
            )
            for r in rows
        ]

    return Leaderboards(
        total_sightings=rank_by(Sighting.id, Sighting),
        # Home-seed tiles are free, so they don't count toward the ranking.
        tiles_explored=rank_by(ExploredTile.id, ExploredTile, ExploredTile.is_home.is_(False)),
    )


@router.get("/mine", response_model=list[CatOut])
def list_my_cats(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(distinct(Sighting.cat_id))
        .filter(Sighting.user_id == current_user.id, Sighting.cat_id.isnot(None))
        .all()
    )
    cat_ids = [row[0] for row in rows]
    if not cat_ids:
        return []
    return (
        db.query(Cat)
        .filter(Cat.id.in_(cat_ids))
        .order_by(Cat.last_seen.desc())
        .all()
    )


@router.get("/{cat_id}/my-photos", response_model=list[MyPhotoOut])
def list_my_photos_of_cat(
    cat_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The current user's own photos of a cat (newest first) — the candidates for
    which one they highlight on that cat's Cat-a-log card."""
    return (
        db.query(Sighting)
        .filter(Sighting.user_id == current_user.id, Sighting.cat_id == cat_id)
        .order_by(Sighting.spotted_at.desc())
        .all()
    )


@router.get("/{cat_id}/territory", response_model=TerritoryOut)
def get_territory(cat_id: int, db: Session = Depends(get_db)):
    """Convex hull of all sightings for a cat, as a GeoJSON Polygon Feature."""
    cat = db.query(Cat).filter(Cat.id == cat_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Cat not found")
    sightings = db.query(Sighting).filter(Sighting.cat_id == cat_id).all()
    return TerritoryOut(
        cat_id=cat_id,
        sighting_count=len(sightings),
        geojson=build_territory_geojson(cat_id, sightings),
    )


@router.get("/{cat_id}", response_model=CatWithSightings)
def get_cat(
    cat_id: int,
    db: Session = Depends(get_db),
):
    cat = db.query(Cat).filter(Cat.id == cat_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Cat not found")
    out = CatWithSightings.model_validate(cat)
    out.owner = owner_card_for_cat(db, cat_id)
    return out
