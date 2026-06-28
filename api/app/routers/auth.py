from datetime import datetime, timedelta, timezone
from random import randint

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.user import User
from app.schemas.auth import (
    AppleLoginRequest,
    ForgotPasswordRequest,
    GoogleLoginRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UpdateMeRequest,
    UserOut,
    UserStats,
    VerifyCodeRequest,
)
from app.models.sighting import Sighting
from app.models.exploration import ExploredTile
from app.models.password_reset import PasswordReset
from app.models.email_verification import EmailVerification
from app.services.auth_service import (
    create_access_token,
    decode_token,
    fetch_google_user_info,
    get_current_user,
    hash_password,
    verify_apple_identity_token,
    verify_password,
)
from app.services.email import send_password_reset_code, send_verification_code
from app.utils.profanity import contains_profanity

router = APIRouter(tags=["auth"])


def issue_verification_code(db: Session, email: str) -> None:
    """Generate, store, and email a fresh email-verification code.

    Replaces any outstanding code for this email, stores the new one hashed
    (single-use, 15-minute expiry), then sends it. Falls back to logging when
    email isn't configured so the flow stays testable in local dev.
    """
    code = f"{randint(0, 999999):06d}"
    db.query(EmailVerification).filter(EmailVerification.email == email).delete()
    db.add(
        EmailVerification(
            email=email,
            code_hash=hash_password(code),
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
        )
    )
    db.commit()
    if not send_verification_code(email, code):
        print(f"[EMAIL VERIFICATION] Code for {email}: {code}")  # noqa: T201


def clean_display_name(name: str | None) -> str | None:
    """Trim a display name and reject anything containing profanity.

    Returns the trimmed name (or ``None`` if blank). Raises 400 when the name
    contains a forbidden word.
    """
    if name is None:
        return None
    trimmed = name.strip()
    if not trimmed:
        return None
    if contains_profanity(trimmed):
        raise HTTPException(
            status_code=400,
            detail="That name isn't allowed. Please choose another.",
        )
    return trimmed


def safe_display_name(name: str | None) -> str | None:
    """Trim a display name, dropping it to ``None`` if it contains profanity.

    For OAuth sign-ins where the name comes from the provider: we don't want a
    profane name to block the login, just to never be stored.
    """
    if name is None:
        return None
    trimmed = name.strip()
    if not trimmed or contains_profanity(trimmed):
        return None
    return trimmed


@router.post("/register", response_model=TokenResponse)
def register(body: RegisterRequest, db: Session = Depends(get_db)):
    if db.query(User).filter(User.email == body.email.lower()).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    user = User(
        email=body.email.lower(),
        hashed_password=hash_password(body.password),
        display_name=clean_display_name(body.display_name),
        email_verified=False,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    # Email a verification code. The account is usable immediately (a token is
    # returned); the app can prompt for the code and call /verify-email.
    issue_verification_code(db, user.email)
    return TokenResponse(access_token=create_access_token({"sub": str(user.id)}))


@router.post("/verify-email")
def verify_email(body: VerifyCodeRequest, db: Session = Depends(get_db)):
    """Confirm a registration's email with the 6-digit code that was emailed."""
    email = body.email.lower()
    entry = (
        db.query(EmailVerification)
        .filter(EmailVerification.email == email)
        .order_by(EmailVerification.id.desc())
        .first()
    )
    if not entry:
        raise HTTPException(status_code=400, detail="No verification code found. Please request a new one.")

    # Stored datetimes come back naive on some engines; normalise to UTC.
    expires_at = entry.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        db.delete(entry)
        db.commit()
        raise HTTPException(status_code=400, detail="Code expired. Please request a new one.")

    if not verify_password(body.code, entry.code_hash):
        raise HTTPException(status_code=400, detail="Incorrect code.")

    db.delete(entry)
    user = db.query(User).filter(User.email == email).first()
    if user:
        user.email_verified = True
    db.commit()
    return {"message": "Email verified", "email_verified": True}


@router.post("/resend-verification")
def resend_verification(body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    """Re-send the email-verification code. No-op (still 200) if the email is
    unknown or already verified, to avoid leaking which addresses exist."""
    email = body.email.lower()
    user = db.query(User).filter(User.email == email).first()
    if user and not user.email_verified:
        issue_verification_code(db, email)
    return {"message": "If that email needs verification, a new code has been sent."}


@router.post("/login", response_model=TokenResponse)
def login(body: LoginRequest, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email.lower()).first()
    if not user or not user.hashed_password or not verify_password(body.password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    # Credentials are valid but the email was never confirmed — re-send a fresh
    # code and tell the app to route to the verification screen.
    if not user.email_verified:
        issue_verification_code(db, user.email)
        raise HTTPException(status_code=403, detail="email_not_verified")
    return TokenResponse(access_token=create_access_token({"sub": str(user.id)}))


@router.post("/apple", response_model=TokenResponse)
def login_apple(body: AppleLoginRequest, db: Session = Depends(get_db)):
    try:
        payload = verify_apple_identity_token(body.identity_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    apple_sub = payload.get("sub")
    email = payload.get("email")

    user = db.query(User).filter(User.apple_sub == apple_sub).first()
    if not user and email:
        user = db.query(User).filter(User.email == email.lower()).first()

    is_new = False
    if not user:
        if not email:
            raise HTTPException(
                status_code=400,
                detail="Email unavailable from Apple. Please sign in again.",
            )
        # Apple has already verified the email it returns.
        user = User(
            email=email.lower(),
            apple_sub=apple_sub,
            display_name=safe_display_name(body.display_name),
            email_verified=True,
        )
        db.add(user)
        is_new = True
    elif not user.apple_sub:
        user.apple_sub = apple_sub

    db.commit()
    db.refresh(user)
    return TokenResponse(access_token=create_access_token({"sub": str(user.id)}), is_new_user=is_new)


@router.post("/google", response_model=TokenResponse)
def login_google(body: GoogleLoginRequest, db: Session = Depends(get_db)):
    try:
        info = fetch_google_user_info(body.access_token)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc))

    google_sub = info.get("sub")
    email = info.get("email")
    display_name = info.get("name")

    if not google_sub or not email:
        raise HTTPException(status_code=400, detail="Could not retrieve profile from Google")

    user = db.query(User).filter(User.google_sub == google_sub).first()
    if not user:
        user = db.query(User).filter(User.email == email.lower()).first()

    is_new = False
    if not user:
        # Google has already verified the email it returns.
        user = User(
            email=email.lower(),
            google_sub=google_sub,
            display_name=safe_display_name(display_name),
            email_verified=True,
        )
        db.add(user)
        is_new = True
    elif not user.google_sub:
        user.google_sub = google_sub

    db.commit()
    db.refresh(user)
    return TokenResponse(access_token=create_access_token({"sub": str(user.id)}), is_new_user=is_new)


@router.post("/forgot-password")
def forgot_password(body: ForgotPasswordRequest, db: Session = Depends(get_db)):
    email = body.email.lower()
    user = db.query(User).filter(User.email == email).first()
    if user:
        code = f"{randint(0, 999999):06d}"
        # Replace any outstanding code for this email, then store the new one
        # hashed (single-use, 15-minute expiry).
        db.query(PasswordReset).filter(PasswordReset.email == email).delete()
        db.add(
            PasswordReset(
                email=email,
                code_hash=hash_password(code),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=15),
            )
        )
        db.commit()
        if not send_password_reset_code(email, code):
            # Email not configured (or send failed): log so the flow stays
            # testable in local dev.
            print(f"[PASSWORD RESET] Code for {email}: {code}")  # noqa: T201
    # Always return 200 to prevent email enumeration
    return {"message": "If that email is registered, a reset code has been sent."}


@router.post("/verify-code")
def verify_code(body: VerifyCodeRequest, db: Session = Depends(get_db)):
    email = body.email.lower()
    entry = (
        db.query(PasswordReset)
        .filter(PasswordReset.email == email)
        .order_by(PasswordReset.id.desc())
        .first()
    )
    if not entry:
        raise HTTPException(status_code=400, detail="No reset code found. Please request a new one.")

    # Stored datetimes come back naive on some engines; normalise to UTC.
    expires_at = entry.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) > expires_at:
        db.delete(entry)
        db.commit()
        raise HTTPException(status_code=400, detail="Code expired. Please request a new one.")

    if not verify_password(body.code, entry.code_hash):
        raise HTTPException(status_code=400, detail="Incorrect code.")

    db.delete(entry)
    db.commit()
    reset_token = create_access_token(
        {"sub": email, "purpose": "password_reset"},
        expires_delta=timedelta(minutes=5),
    )
    return {"reset_token": reset_token}


@router.post("/reset-password")
def reset_password(body: ResetPasswordRequest, db: Session = Depends(get_db)):
    try:
        payload = decode_token(body.reset_token)
    except HTTPException:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")

    if payload.get("purpose") != "password_reset":
        raise HTTPException(status_code=400, detail="Invalid reset token")

    email = payload.get("sub")
    user = db.query(User).filter(User.email == email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.hashed_password = hash_password(body.new_password)
    db.commit()
    return {"message": "Password reset successfully"}


@router.get("/me", response_model=UserOut)
def get_me(current_user: User = Depends(get_current_user)):
    return current_user


@router.put("/me", response_model=UserOut)
def update_me(
    body: UpdateMeRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if body.display_name is not None:
        new_name = clean_display_name(body.display_name)
        if current_user.display_name_updated_at is not None:
            since = datetime.now(timezone.utc) - current_user.display_name_updated_at.replace(tzinfo=timezone.utc)
            if since < timedelta(days=30):
                days_left = 30 - since.days
                raise HTTPException(
                    status_code=429,
                    detail=f"You can only change your name once a month. Try again in {days_left} day{'s' if days_left != 1 else ''}.",
                )
        current_user.display_name = new_name
        current_user.display_name_updated_at = datetime.now(timezone.utc)
    if body.avatar_emoji is not None:
        current_user.avatar_emoji = body.avatar_emoji
    db.commit()
    db.refresh(current_user)
    return current_user


@router.delete("/me", status_code=204)
def delete_me(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Permanently delete the account.

    Personal content (direct Explorer uploads, comments, meows, claims,
    notifications, push tokens) is hard-deleted. Sightings stay in the
    community catalog anonymously — removing them would gut cats other
    users follow — and their mirror posts survive the startup backfill
    with the user reference nulled.
    """
    from app.models.claim import CatClaim, ClaimPhoto
    from app.models.cat import Cat
    from app.models.explorer import ExplorerPost, PostComment, PostMeow, PostReport
    from app.models.notification import Notification, PushToken
    from app.services.content_deletion import delete_cat, delete_post_dependents, safe_unlink

    uid = current_user.id
    files_to_unlink: list[str] = []

    # 1. Direct-upload posts: hard delete with dependents and files.
    direct_posts = (
        db.query(ExplorerPost)
        .filter(ExplorerPost.user_id == uid, ExplorerPost.sighting_id.is_(None))
        .all()
    )
    delete_post_dependents(db, [p.id for p in direct_posts])
    for p in direct_posts:
        db.query(Cat).filter(Cat.last_photo_path == p.photo_path).update(
            {Cat.last_photo_path: None}, synchronize_session=False
        )
        files_to_unlink.append(p.photo_path)
        db.delete(p)
    db.flush()

    # 2. Sighting-mirror posts survive anonymously (the backfill re-creates
    #    them otherwise), as do the sightings themselves.
    db.query(ExplorerPost).filter(
        ExplorerPost.user_id == uid, ExplorerPost.sighting_id.isnot(None)
    ).update({ExplorerPost.user_id: None}, synchronize_session=False)
    db.query(Sighting).filter(Sighting.user_id == uid).update(
        {Sighting.user_id: None, Sighting.spotter_name: None}, synchronize_session=False
    )

    # 3. Interactions, reports, and follows.
    from app.models.follow import CatFollow

    db.query(PostComment).filter(PostComment.user_id == uid).delete(synchronize_session=False)
    db.query(PostMeow).filter(PostMeow.user_id == uid).delete(synchronize_session=False)
    db.query(PostReport).filter(PostReport.reporter_id == uid).delete(synchronize_session=False)
    db.query(CatFollow).filter(CatFollow.user_id == uid).delete(synchronize_session=False)
    db.query(ExploredTile).filter(ExploredTile.user_id == uid).delete(synchronize_session=False)

    # 4. Claims (all statuses) and their evidence photos. Verified claims
    #    vanish, so those cats become claimable again.
    claims = db.query(CatClaim).filter(CatClaim.user_id == uid).all()
    claimed_cat_ids = {c.cat_id for c in claims}
    claim_ids = [c.id for c in claims]
    if claim_ids:
        files_to_unlink.extend(
            row[0]
            for row in db.query(ClaimPhoto.photo_path)
            .filter(ClaimPhoto.claim_id.in_(claim_ids))
            .all()
        )
        db.query(ClaimPhoto).filter(ClaimPhoto.claim_id.in_(claim_ids)).delete(synchronize_session=False)
        db.query(CatClaim).filter(CatClaim.id.in_(claim_ids)).delete(synchronize_session=False)
    db.flush()

    # Orphaned "my new cat" profiles: zero sightings and no remaining claims.
    for cat_id in claimed_cat_ids:
        cat = db.query(Cat).filter(Cat.id == cat_id).first()
        if not cat:
            continue
        has_sightings = db.query(Sighting.id).filter(Sighting.cat_id == cat_id).first() is not None
        has_claims = db.query(CatClaim.id).filter(CatClaim.cat_id == cat_id).first() is not None
        if not has_sightings and not has_claims:
            files_to_unlink.extend(delete_cat(db, cat))

    # 5. Inbox and devices, then the user row itself.
    db.query(Notification).filter(Notification.user_id == uid).delete(synchronize_session=False)
    db.query(PushToken).filter(PushToken.user_id == uid).delete(synchronize_session=False)
    db.delete(current_user)

    db.commit()
    safe_unlink(db, files_to_unlink)


@router.get("/me/stats", response_model=UserStats)
def get_my_stats(current_user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    my_sightings = db.query(Sighting).filter(Sighting.user_id == current_user.id).count()
    unique_cats = (
        db.query(Sighting.cat_id)
        .filter(Sighting.user_id == current_user.id, Sighting.cat_id.isnot(None))
        .distinct()
        .count()
    )
    tiles_explored = (
        db.query(ExploredTile).filter(ExploredTile.user_id == current_user.id).count()
    )
    checkpoints_lit = (
        db.query(ExploredTile)
        .filter(ExploredTile.user_id == current_user.id, ExploredTile.checkpoint_id.isnot(None))
        .count()
    )
    return UserStats(
        my_sightings=my_sightings,
        unique_cats_spotted=unique_cats,
        tiles_explored=tiles_explored,
        checkpoints_lit=checkpoints_lit,
        joined_at=current_user.created_at,
    )
