from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class DailyUsage(Base):
    """Per-day usage counter backing the daily rate limits.

    One row per (key, day): key encodes the scope and principal, e.g.
    "sightings:user:42" or "explorer:ip:1.2.3.4"; day is an ISO date string.
    DB-backed so limits survive restarts and hold across multiple instances
    (unlike the old in-memory dicts in the routers). Stale rows from past days
    are simply never read again — tiny table, no sweep needed at this scale.
    """

    __tablename__ = "daily_usage"

    key: Mapped[str] = mapped_column(String, primary_key=True)
    day: Mapped[str] = mapped_column(String, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
