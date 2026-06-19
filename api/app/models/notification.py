from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Integer, String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Notification(Base):
    """In-app notification. Also mirrored as an Expo push when the user has tokens."""

    __tablename__ = "notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # sighting | claim_verified | claim_rejected | meow | comment
    type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    cat_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("cats.id"), nullable=True)
    sighting_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("sightings.id"), nullable=True)
    post_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("explorer_posts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    cat = relationship("Cat")
    sighting = relationship("Sighting")


class PushToken(Base):
    """An Expo push token for one of a user's devices."""

    __tablename__ = "push_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    token: Mapped[str] = mapped_column(String, unique=True, nullable=False, index=True)
    # ios | android
    platform: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_used_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
