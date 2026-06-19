import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.db.session import get_db
from app.models.cat import Cat
from app.models.claim import CatClaim, ClaimPhoto
from app.models.notification import Notification
from app.models.user import User
from app.schemas.claim import (
    INDOOR_OUTDOOR_VALUES,
    ClaimOut,
    ClaimResult,
    ClaimStatusResponse,
    MyClaimItem,
    OwnerCard,
    OwnerCardUpdate,
)
from app.services.auth_service import get_current_user, get_optional_user
from app.services.claim_verification import (
    CLAIM_COOLDOWN_HOURS,
    MAX_CLAIM_ATTEMPTS_PER_DAY,
    MAX_PHOTOS,
    MIN_PHOTOS,
    analyze_claim_photos,
    decide_claim,
)
from app.services.storage import UPLOADS_PREFIX, get_storage
from app.services.vision import VisionError

log = logging.getLogger(__name__)

router = APIRouter(tags=["claims"])

CLAIM_PREFIX = f"{UPLOADS_PREFIX}/claims"

MAX_PHOTO_BYTES = 5 * 1024 * 1024


def _verified_claim(db: Session, cat_id: int) -> CatClaim | None:
    return (
        db.query(CatClaim)
        .filter(CatClaim.cat_id == cat_id, CatClaim.status == "verified")
        .first()
    )


def owner_card_for_cat(db: Session, cat_id: int) -> OwnerCard | None:
    """Public owner card for a cat, or None if unclaimed. Reused by GET /cats/{id}."""
    claim = (
        db.query(CatClaim)
        .options(joinedload(CatClaim.user))
        .filter(CatClaim.cat_id == cat_id, CatClaim.status == "verified")
        .first()
    )
    if not claim:
        return None
    return OwnerCard(
        display_name=claim.user.display_name if claim.user else None,
        avatar_emoji=claim.user.avatar_emoji if claim.user else None,
        real_name=claim.real_name,
        likes_petting=claim.likes_petting,
        accepts_treats=claim.accepts_treats,
        age_years=claim.age_years,
        fun_fact=claim.fun_fact,
        indoor_outdoor=claim.indoor_outdoor,
        claimed_at=claim.decided_at or claim.created_at,
    )


@router.post("/cats/{cat_id}/claim", response_model=ClaimResult, status_code=201)
async def submit_claim(
    cat_id: int,
    photos: list[UploadFile] = File(...),
    likes_petting: bool = Form(...),
    accepts_treats: bool = Form(...),
    age_years: int = Form(...),
    indoor_outdoor: str = Form(...),
    real_name: str | None = Form(None),
    fun_fact: str | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Claim a cat by submitting 2-3 photos plus the owner card.

    Photos are scored against the cat's stored vision features; the claim is
    verified or rejected synchronously (a few seconds of vision calls).
    """
    cat = db.query(Cat).filter(Cat.id == cat_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Cat not found")

    if not (MIN_PHOTOS <= len(photos) <= MAX_PHOTOS):
        raise HTTPException(
            status_code=400,
            detail=f"Submit between {MIN_PHOTOS} and {MAX_PHOTOS} photos.",
        )
    if indoor_outdoor not in INDOOR_OUTDOOR_VALUES:
        raise HTTPException(status_code=400, detail="indoor_outdoor must be indoor, outdoor or both.")
    if not (0 <= age_years <= 30):
        raise HTTPException(status_code=400, detail="age_years must be between 0 and 30.")
    if fun_fact and len(fun_fact) > 280:
        raise HTTPException(status_code=400, detail="fun_fact must be 280 characters or fewer.")
    real_name = real_name.strip() if real_name else None
    if real_name and len(real_name) > 40:
        raise HTTPException(status_code=400, detail="real_name must be 40 characters or fewer.")

    existing = _verified_claim(db, cat_id)
    if existing:
        detail = (
            "You are already this cat's verified owner."
            if existing.user_id == current_user.id
            else "This cat already has a verified owner."
        )
        raise HTTPException(status_code=409, detail=detail)

    now = datetime.now(timezone.utc)
    cooldown_cutoff = now - timedelta(hours=CLAIM_COOLDOWN_HOURS)
    recent_rejection = (
        db.query(CatClaim)
        .filter(
            CatClaim.cat_id == cat_id,
            CatClaim.user_id == current_user.id,
            CatClaim.status == "rejected",
            CatClaim.decided_at >= cooldown_cutoff,
        )
        .order_by(CatClaim.decided_at.desc())
        .first()
    )
    if recent_rejection:
        cooldown_until = recent_rejection.decided_at + timedelta(hours=CLAIM_COOLDOWN_HOURS)
        raise HTTPException(
            status_code=409,
            detail=f"A recent claim on this cat was rejected. Try again after {cooldown_until.isoformat()}.",
        )

    day_cutoff = now - timedelta(days=1)
    attempts_today = (
        db.query(CatClaim)
        .filter(CatClaim.user_id == current_user.id, CatClaim.created_at >= day_cutoff)
        .count()
    )
    if attempts_today >= MAX_CLAIM_ATTEMPTS_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit of {MAX_CLAIM_ATTEMPTS_PER_DAY} claim attempts reached.",
        )

    storage = get_storage()
    photos_bytes: list[bytes] = []
    saved_paths: list[str] = []
    for photo in photos:
        contents = await photo.read()
        if len(contents) > MAX_PHOTO_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Each photo must be under {MAX_PHOTO_BYTES // 1024 // 1024}MB.",
            )
        photos_bytes.append(contents)
        ext = Path(photo.filename).suffix if photo.filename else ".jpg"
        saved_paths.append(storage.put(contents, ext=ext, prefix=CLAIM_PREFIX))

    try:
        photo_features = await analyze_claim_photos(photos_bytes)
    except VisionError as exc:
        for p in saved_paths:
            storage.delete(p)
        log.warning("Claim vision failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Cat recognition is temporarily unavailable. Please try again.",
        )

    decision = decide_claim(photo_features, cat)

    claim = CatClaim(
        cat_id=cat_id,
        user_id=current_user.id,
        status="verified" if decision.verified else "rejected",
        avg_confidence=decision.avg_confidence,
        rejection_reason=decision.reason,
        real_name=real_name,
        likes_petting=likes_petting,
        accepts_treats=accepts_treats,
        age_years=age_years,
        fun_fact=fun_fact,
        indoor_outdoor=indoor_outdoor,
        created_at=now,
        decided_at=datetime.now(timezone.utc),
    )
    db.add(claim)
    try:
        db.flush()
    except IntegrityError:
        # Lost a race with a simultaneous verified claim on the same cat.
        db.rollback()
        raise HTTPException(status_code=409, detail="This cat already has a verified owner.")

    # Per-photo scores exist only for photos the decision reached before any
    # early rejection; align by index and leave the rest None.
    for i, (path, features) in enumerate(zip(saved_paths, photo_features)):
        db.add(
            ClaimPhoto(
                claim_id=claim.id,
                photo_path=path,
                confidence=decision.per_photo[i] if i < len(decision.per_photo) else None,
                features_json=features.to_json(),
            )
        )

    if decision.verified:
        # The owner knows the cat's actual name: it replaces the generated nickname.
        if real_name:
            cat.name = real_name
        db.add(
            Notification(
                user_id=current_user.id,
                type="claim_verified",
                title=f"You're now {cat.name or 'your cat'}'s verified owner",
                body="Your photos matched. You'll be notified whenever they're spotted.",
                cat_id=cat.id,
            )
        )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="This cat already has a verified owner.")

    return ClaimResult(
        status=claim.status,
        avg_confidence=decision.avg_confidence,
        per_photo_confidences=decision.per_photo,
        rejection_reason=decision.reason,
    )


@router.get("/cats/{cat_id}/claim", response_model=ClaimStatusResponse)
def get_claim_status(
    cat_id: int,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    cat = db.query(Cat).filter(Cat.id == cat_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="Cat not found")

    owner = owner_card_for_cat(db, cat_id)

    my_claim = None
    cooldown_until = None
    if current_user:
        latest = (
            db.query(CatClaim)
            .filter(CatClaim.cat_id == cat_id, CatClaim.user_id == current_user.id)
            .order_by(CatClaim.created_at.desc())
            .first()
        )
        if latest:
            my_claim = ClaimOut.model_validate(latest)
            if latest.status == "rejected" and latest.decided_at:
                decided = latest.decided_at
                if decided.tzinfo is None:
                    decided = decided.replace(tzinfo=timezone.utc)
                until = decided + timedelta(hours=CLAIM_COOLDOWN_HOURS)
                if until > datetime.now(timezone.utc):
                    cooldown_until = until

    can_claim = bool(
        current_user
        and owner is None
        and cooldown_until is None
        and (my_claim is None or my_claim.status in ("rejected", "revoked"))
    )

    return ClaimStatusResponse(
        owner=owner,
        my_claim=my_claim,
        can_claim=can_claim,
        cooldown_until=cooldown_until,
    )


@router.put("/cats/{cat_id}/claim", response_model=ClaimOut)
def update_owner_card(
    cat_id: int,
    body: OwnerCardUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    claim = _verified_claim(db, cat_id)
    if not claim or claim.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You are not this cat's verified owner.")

    if body.indoor_outdoor is not None and body.indoor_outdoor not in INDOOR_OUTDOOR_VALUES:
        raise HTTPException(status_code=400, detail="indoor_outdoor must be indoor, outdoor or both.")

    for field in ("real_name", "likes_petting", "accepts_treats", "age_years", "fun_fact", "indoor_outdoor"):
        value = getattr(body, field)
        if value is not None:
            setattr(claim, field, value)

    # Renaming through the owner card also renames the cat itself.
    if body.real_name:
        cat = db.query(Cat).filter(Cat.id == cat_id).first()
        if cat:
            cat.name = body.real_name

    db.commit()
    db.refresh(claim)
    return claim


@router.delete("/cats/{cat_id}/claim", status_code=204)
def revoke_claim(
    cat_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    claim = _verified_claim(db, cat_id)
    if not claim or claim.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You are not this cat's verified owner.")
    claim.status = "revoked"
    claim.decided_at = datetime.now(timezone.utc)
    db.commit()


@router.get("/claims/mine", response_model=list[MyClaimItem])
def my_claims(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    claims = (
        db.query(CatClaim)
        .options(joinedload(CatClaim.cat))
        .filter(CatClaim.user_id == current_user.id, CatClaim.status != "revoked")
        .order_by(CatClaim.created_at.desc())
        .all()
    )
    return [
        MyClaimItem(
            **ClaimOut.model_validate(c).model_dump(),
            cat_name=c.cat.name if c.cat else None,
            cat_photo_path=c.cat.last_photo_path if c.cat else None,
            cat_rarity_score=c.cat.rarity_score if c.cat else None,
        )
        for c in claims
    ]
