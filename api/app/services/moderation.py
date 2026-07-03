"""Content-strike accounting: two warnings, banned on the third offense.

A strike is recorded when vision flags a submitted photo as harmful
(CatFeatures.is_appropriate == False — see the INAPPROPRIATE_REASONS enum in
services/vision.py). Benign failures (no cat, blurry, multiple cats) are NOT
strikes. The ban is enforced app-wide by auth_service.get_current_user, which
rejects any account with banned_at set.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models.user import User

log = logging.getLogger(__name__)

# Offense 1 and 2 warn; offense 3 bans.
BAN_STRIKE_COUNT = 3


def register_content_strike(db: Session, user: User, reason: str | None) -> str:
    """Record one strike, banning the account on the third. Commits.

    Returns the user-facing message describing the consequence — callers put it
    in the rejection response so the user sees the warning immediately.
    """
    user.content_strikes += 1
    strikes = user.content_strikes
    if strikes >= BAN_STRIKE_COUNT and user.banned_at is None:
        user.banned_at = datetime.now(timezone.utc)
    db.commit()

    log.warning(
        "Content strike %d for user %s (reason=%s)%s",
        strikes, user.id, reason, " — BANNED" if user.banned_at else "",
    )

    if user.banned_at is not None:
        return (
            "This photo contains content that isn't allowed. Your account has "
            "been banned for repeated violations."
        )
    remaining = BAN_STRIKE_COUNT - strikes
    return (
        "This photo contains content that isn't allowed. "
        f"Warning {strikes} of {BAN_STRIKE_COUNT - 1} — "
        f"{remaining} more violation{'s' if remaining != 1 else ''} and your "
        "account will be banned."
    )
