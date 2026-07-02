import logging
from datetime import datetime, timedelta, timezone

import bcrypt
import httpx
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy.orm import Session

from app.config import settings
from app.db.session import get_db
from app.models.user import User

log = logging.getLogger(__name__)

_security = HTTPBearer()

APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


def create_access_token(data: dict, expires_delta: timedelta | None = None) -> str:
    payload = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    payload["exp"] = expire
    return jwt.encode(payload, settings.secret_key, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.secret_key, algorithms=["HS256"])
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


_optional_security = HTTPBearer(auto_error=False)


def get_optional_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_optional_security),
    db: Session = Depends(get_db),
) -> User | None:
    if not credentials:
        return None
    try:
        payload = decode_token(credentials.credentials)
    except HTTPException:
        return None
    if payload.get("purpose"):
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    user = db.get(User, int(user_id))
    return user if user and user.is_active else None


def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(_security),
    db: Session = Depends(get_db),
) -> User:
    payload = decode_token(credentials.credentials)
    if payload.get("purpose"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")
    user = db.get(User, int(user_id))
    if not user or not user.is_active:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="User not found")
    # Hard email-verification gate: an unverified account can authenticate but
    # cannot use any protected endpoint until it confirms its email. The app
    # routes this 403 to the verification screen. /auth/verify-email and
    # /auth/resend-verification take the email in the body (no token), so they
    # stay reachable while unverified.
    if not user.email_verified:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="email_not_verified")
    return user


def require_admin(current_user: User = Depends(get_current_user)) -> User:
    """Gate catalog-maintenance endpoints to admin accounts only."""
    if not getattr(current_user, "is_admin", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return current_user


APPLE_ISSUER = "https://appleid.apple.com"
GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"


def _check_audience(claims: dict, expected: str, provider: str) -> None:
    """Enforce that a verified token was minted for *this* app.

    ``expected`` is our configured OAuth client/bundle id(s) — a comma-separated
    list, since a native app has a different Google client id per platform
    (iOS/Android/Web) and any of them is legitimate. When it's unset (local dev,
    no client id available) the check is skipped with a warning rather than
    silently trusting any audience. Google's tokeninfo names the minting client
    in ``aud``/``azp``; Apple's identity token uses ``aud``.
    """
    allowed = {a.strip() for a in expected.split(",") if a.strip()}
    if not allowed:
        log.warning(
            "%s audience not verified — set the client id to enable this check", provider
        )
        return
    presented = {claims.get("aud"), claims.get("azp")}
    presented.discard(None)
    if allowed.isdisjoint(presented):
        raise ValueError(f"{provider} token was not issued for this app")


def verify_apple_identity_token(identity_token: str) -> dict:
    """Verify an Apple identity token against Apple's JWKS.

    Pins the algorithm to RS256 (never trusting the token's own header to name
    it), verifies the issuer, and — when apple_client_id is configured — the
    audience, so a token minted for a different Apple relying party is rejected.
    """
    try:
        header = jwt.get_unverified_header(identity_token)
        kid = header.get("kid")

        resp = httpx.get(APPLE_JWKS_URL, timeout=10.0)
        resp.raise_for_status()
        jwks = resp.json()

        key = next((k for k in jwks["keys"] if k["kid"] == kid), None)
        if not key:
            raise ValueError("No matching Apple public key found")

        claims = jwt.decode(
            identity_token,
            key,
            algorithms=["RS256"],  # pin — ignore the attacker-controllable header alg
            issuer=APPLE_ISSUER,
            options={"verify_aud": False, "verify_iss": True},
        )
    except JWTError as exc:
        raise ValueError(f"Invalid Apple token: {exc}") from exc

    _check_audience(claims, settings.apple_client_id, "Apple")
    return claims


def fetch_google_user_info(access_token: str) -> dict:
    """Resolve a Google OAuth2 access token to a verified profile.

    Uses Google's tokeninfo endpoint (not userinfo) so the response includes the
    ``aud``/``azp`` of the client that minted the token; we reject tokens that
    weren't issued for this app. This closes the confused-deputy hole where any
    valid Google access token (harvested by a third-party app) could be replayed
    to log in as the victim.
    """
    resp = httpx.get(
        GOOGLE_TOKENINFO_URL,
        params={"access_token": access_token},
        timeout=10.0,
    )
    if resp.status_code != 200:
        raise ValueError("Invalid Google access token")
    info = resp.json()
    _check_audience(info, settings.google_client_id, "Google")
    return info
