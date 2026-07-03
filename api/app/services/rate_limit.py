"""DB-backed daily rate limiting.

Replaces the routers' old in-memory per-IP dicts, which reset on every deploy
and kept an independent count per instance. Counters live in the daily_usage
table, so limits are enforced consistently across restarts and instances.

Keys are scoped per feature and principal ("sightings:user:42",
"explorer:ip:1.2.3.4") — prefer the authenticated user id where available:
mobile carriers put many users behind one CGNAT IP, and an abuser can rotate
IPs far more easily than accounts.
"""

from __future__ import annotations

from datetime import date

from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.rate_limit import DailyUsage


def enforce_daily_limit(db: Session, key: str, limit: int, detail: str) -> None:
    """Count one use against `key` for today; raise 429 once `limit` is reached.

    Increment-then-check, committed immediately so the spend is recorded even
    if the request later fails. Two concurrent requests may briefly race the
    insert; the IntegrityError fallback re-reads and increments, so the count
    stays accurate enough for an abuse cap (this is not a billing ledger).
    """
    today = date.today().isoformat()
    row = db.get(DailyUsage, (key, today))
    if row is None:
        row = DailyUsage(key=key, day=today, count=1)
        db.add(row)
        try:
            db.commit()
        except IntegrityError:
            # Lost the insert race — another request created today's row first.
            db.rollback()
            row = db.get(DailyUsage, (key, today))
            if row is None:  # pragma: no cover — row can't vanish within a day
                raise HTTPException(status_code=429, detail=detail)
            row.count += 1
            db.commit()
    else:
        row.count += 1
        db.commit()

    if row.count > limit:
        raise HTTPException(status_code=429, detail=detail)
