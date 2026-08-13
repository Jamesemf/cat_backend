"""Scheduled deletion of personal data there is no longer a reason to hold.

Claim photos are the sharp case, and the reason this module exists. A claim asks
for three photos of the claimant *with* the cat, taken — per the app's own
guidance — inside their home. A rejected claim therefore leaves us holding
photographs of an identifiable person at their home address, kept in support of
a request we refused. Holding those until the user happens to delete their
account is not a retention position anyone would defend, and the published
privacy policy now states a period, so this is what makes that sentence true.

What gets swept, and what deliberately doesn't:

* ``rejected`` / ``revoked`` — swept once the appeal window has passed. The
  claim row itself stays (it is the record of the decision, and the re-claim
  cooldown reads its timestamp); only the photographs go.
* ``pending`` — kept. A moderator still has to look at them.
* ``verified`` — kept. They are the evidence for a grant that is still in
  force. They go when the claim is revoked (via this sweep, after the window)
  or when the account is deleted (services/content_deletion.py).

Deletion goes through ``safe_unlink``, which refuses to remove an object any
surviving row still points at — a ``register`` claim's first photo becomes the
cat's cover image on approval, and revoking that claim later must not blank the
cat's photo.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.claim import CatClaim, ClaimPhoto
from app.models.rate_limit import DailyUsage
from app.services.content_deletion import safe_unlink

log = logging.getLogger(__name__)

# How long a closed claim's photos survive its decision. Long enough that a
# rejected claimant can appeal and a moderator can reverse a mistake with the
# evidence still in front of them; short enough that "we deleted it" is true
# within a quarter. Stated in the privacy policy — keep the two in step.
CLAIM_PHOTO_RETENTION_DAYS = 90

# Statuses that mean the claim is closed and the photos have done their job.
CLOSED_STATUSES = ("rejected", "revoked")

# How long a daily rate-limit counter is kept. Half of these are keyed by IP
# address (services/rate_limit.py), which is personal data, and yesterday's
# count is never read again — the limit only ever looks at today. Keeping a
# fortnight leaves enough history to investigate a burst of abuse.
RATE_LIMIT_RETENTION_DAYS = 14


def _naive_utc_cutoff(days: int) -> datetime:
    """Cutoff as a naive UTC datetime.

    The DateTime columns are naive (they store UTC without a tzinfo), so an
    aware datetime would not compare correctly against them on every dialect.
    """
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)


def sweep_claim_photos(
    db: Session, retain_days: int = CLAIM_PHOTO_RETENTION_DAYS
) -> int:
    """Delete photos belonging to claims closed more than ``retain_days`` ago.

    Returns the number of ClaimPhoto rows removed. Idempotent: a second run
    finds nothing, because the rows are gone.
    """
    cutoff = _naive_utc_cutoff(retain_days)

    claim_ids = [
        row[0]
        for row in db.query(CatClaim.id)
        .filter(
            CatClaim.status.in_(CLOSED_STATUSES),
            CatClaim.decided_at.isnot(None),
            CatClaim.decided_at < cutoff,
        )
        .all()
    ]
    if not claim_ids:
        return 0

    paths = [
        row[0]
        for row in db.query(ClaimPhoto.photo_path)
        .filter(ClaimPhoto.claim_id.in_(claim_ids))
        .all()
    ]
    if not paths:
        return 0

    removed = (
        db.query(ClaimPhoto)
        .filter(ClaimPhoto.claim_id.in_(claim_ids))
        .delete(synchronize_session=False)
    )
    db.commit()

    # After the commit, so a surviving reference check sees the real state.
    safe_unlink(db, paths)

    log.info(
        "Retention sweep: removed %d claim photo(s) from %d closed claim(s)",
        removed, len(claim_ids),
    )
    return removed


def sweep_rate_limit_counters(
    db: Session, retain_days: int = RATE_LIMIT_RETENTION_DAYS
) -> int:
    """Drop daily-usage rows older than ``retain_days``.

    ``day`` is an ISO date string, so a lexical comparison is a chronological
    one. Returns the number of rows removed.
    """
    cutoff = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=retain_days)
    ).date().isoformat()

    removed = (
        db.query(DailyUsage)
        .filter(DailyUsage.day < cutoff)
        .delete(synchronize_session=False)
    )
    db.commit()

    if removed:
        log.info("Retention sweep: removed %d stale rate-limit counter(s)", removed)
    return removed
