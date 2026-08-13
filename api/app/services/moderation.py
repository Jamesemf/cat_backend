"""Moderation: content-strike accounting, and the community report threshold.

Two independent paths lead here.

*Strikes* are recorded when vision flags a submitted photo as harmful
(CatFeatures.is_appropriate == False — see the INAPPROPRIATE_REASONS enum in
services/vision.py). Benign failures (no cat, blurry, multiple cats) are NOT
strikes. The ban is enforced app-wide by auth_service.get_current_user, which
rejects any account with banned_at set.

*Reports* are filed by users against a post. Once enough distinct users report
the same post it is hidden automatically — a reversible holding action that
takes the content out of circulation until a human looks at it. Hiding never
bans or strikes the author: reports are unverified by definition, and treating
them as proof would hand any group of three users a way to punish someone.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import and_, exists, func
from sqlalchemy.orm import Session

from app.models.explorer import ExplorerPost, PostReport
from app.models.sighting import Sighting
from app.models.user import User

log = logging.getLogger(__name__)

# Offense 1 and 2 warn; offense 3 bans.
BAN_STRIKE_COUNT = 3

# Where a user contests a strike. Strikes are applied by the vision model with
# no human in the loop, and the third one locks the account out of the whole
# app, so every message that reports a strike has to carry a route to a person —
# that route is the only human review in this pipeline.
APPEAL_EMAIL = "support@catapp.uk"

# Distinct open reports that hide a post pending review. Low enough to react
# quickly on a small user base, high enough that one person can't hide a post
# they dislike (and the unique constraint on post_reports stops them trying).
AUTO_HIDE_REPORT_COUNT = 3


def sighting_has_hidden_post():
    """Correlated EXISTS: this sighting was mirrored into a post that is hidden.

    Hiding the post alone would only clear the Explorer feed — the same photo
    also reaches users through the map/neighbourhood feed and the per-cat photo
    carousels, which read sightings directly. Apply this filter there so one
    moderation decision covers every surface the photo appears on.
    """
    return exists().where(
        and_(
            ExplorerPost.sighting_id == Sighting.id,
            ExplorerPost.hidden_at.isnot(None),
        )
    )


def open_report_count(db: Session, post_id: int) -> int:
    """How many unresolved reports stand against a post."""
    return (
        db.query(func.count(PostReport.id))
        .filter(PostReport.post_id == post_id, PostReport.reviewed_at.is_(None))
        .scalar()
        or 0
    )


def apply_report_threshold(db: Session, post: ExplorerPost) -> bool:
    """Hide a post once it crosses the report threshold. Commits if it hides.

    Returns True when this call hid the post. A post a moderator already hid
    (or that is already hidden automatically) is left alone.
    """
    if post.hidden_at is not None:
        return False
    if open_report_count(db, post.id) < AUTO_HIDE_REPORT_COUNT:
        return False

    post.hidden_at = datetime.now(timezone.utc)
    post.hidden_reason = "auto_reports"
    db.commit()
    log.warning(
        "Post %s auto-hidden after %d reports (author=%s)",
        post.id, AUTO_HIDE_REPORT_COUNT, post.user_id,
    )
    return True


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
            "Our automated check flagged this photo as content that isn't allowed. "
            "Your account has been banned for repeated violations. No person "
            f"reviewed this before the ban — if it's wrong, email {APPEAL_EMAIL} "
            "and we'll look at it ourselves."
        )
    remaining = BAN_STRIKE_COUNT - strikes
    return (
        "Our automated check flagged this photo as content that isn't allowed. "
        f"Warning {strikes} of {BAN_STRIKE_COUNT - 1} — "
        f"{remaining} more violation{'s' if remaining != 1 else ''} and your "
        f"account will be banned. If this is wrong, email {APPEAL_EMAIL}."
    )
