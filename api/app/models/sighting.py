from datetime import datetime, timezone

from sqlalchemy import Boolean, Float, ForeignKey, Integer, String, DateTime, Text
from sqlalchemy.orm import Mapped, backref, mapped_column, relationship

from app.db.session import Base


class Sighting(Base):
    __tablename__ = "sightings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    cat_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("cats.id"), nullable=True, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    photo_path: Mapped[str] = mapped_column(String, nullable=False)
    latitude: Mapped[float] = mapped_column(Float, nullable=False)
    longitude: Mapped[float] = mapped_column(Float, nullable=False)
    spotted_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    spotter_name: Mapped[str | None] = mapped_column(String, nullable=True)
    breed_description: Mapped[str | None] = mapped_column(String, nullable=True)
    vibes: Mapped[str | None] = mapped_column(String, nullable=True)

    # Vision recognition fields (populated by Claude Haiku). Indexed columns are the
    # controlled-vocabulary features used for Re-ID SQL prefiltering.
    is_cat: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    primary_color: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    secondary_color: Mapped[str | None] = mapped_column(String, nullable=True)
    pattern: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    fur_length: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    eye_color: Mapped[str | None] = mapped_column(String, nullable=True)
    body_size: Mapped[str | None] = mapped_column(String, nullable=True)
    # Free-text and raw payload kept off the indexed path.
    features_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Polaroid keepsake customization, set by the spotter in the Nearby feed /
    # capture flow. frame_id names a style in the client's polaroidStyles;
    # photo_adjust holds the pan/zoom as JSON {scale, x, y}; caption replaces the
    # film-stamp date when set. All nullable — older sightings render the default
    # polaroid.
    frame_id: Mapped[str | None] = mapped_column(String, nullable=True)
    photo_adjust: Mapped[str | None] = mapped_column(Text, nullable=True)
    caption: Mapped[str | None] = mapped_column(String, nullable=True)

    # Newest first, so every reader of cat.sightings gets a defined order. Left
    # to insertion order, the profile's map highlighted the oldest spot as the
    # most recent and drew its direction gradient backwards.
    cat  = relationship("Cat",  backref=backref("sightings", order_by="Sighting.spotted_at.desc()"))
    user = relationship("User", foreign_keys=[user_id])

    # spotter_name is denormalised onto the row, but the spotter's id and chosen
    # emoji live on the user — surfaced here so SightingOut can carry them and
    # the client can show a real avatar and link to the profile behind it.
    @property
    def spotter_id(self) -> int | None:
        return self.user_id

    @property
    def spotter_emoji(self) -> str | None:
        return self.user.avatar_emoji if self.user else None
