from datetime import datetime, timezone

from sqlalchemy import Boolean, Float, Integer, String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class Cat(Base):
    __tablename__ = "cats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    breed: Mapped[str | None] = mapped_column(String, nullable=True)
    rarity_score: Mapped[float] = mapped_column(Float, default=0.0)
    sighting_count: Mapped[int] = mapped_column(Integer, default=0)
    first_seen: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    last_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_photo_path: Mapped[str | None] = mapped_column(String, nullable=True)
    vibes: Mapped[str | None] = mapped_column(String, nullable=True)

    # Representative visual features for Re-ID. Written from the first sighting and
    # refreshed when a sighting is reassigned to this cat.
    is_cat: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    primary_color: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    secondary_color: Mapped[str | None] = mapped_column(String, nullable=True)
    pattern: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    fur_length: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    eye_color: Mapped[str | None] = mapped_column(String, nullable=True)
    body_size: Mapped[str | None] = mapped_column(String, nullable=True)
    features_json: Mapped[str | None] = mapped_column(Text, nullable=True)
