from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, DateTime, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class CatFollow(Base):
    """A user following a cat. Followers get notified about new sightings."""

    __tablename__ = "cat_follows"
    __table_args__ = (UniqueConstraint("cat_id", "user_id", name="uq_follow_cat_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    cat_id: Mapped[int] = mapped_column(Integer, ForeignKey("cats.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
