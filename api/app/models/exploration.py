from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class ExploredTile(Base):
    """One hex tile a user has uncovered in the "Fog of Paw" map.

    The map already unlocks tiles client-side; this is the server-of-record copy
    so exploration survives a reinstall, restores the fog on a new device, and
    powers exploration achievements (and, later, leaderboards). One row per
    (user, tile) — re-reporting a tile is a no-op thanks to the unique constraint.

    A row whose checkpoint_id is set marks a *lit checkpoint*: a tile the user
    reached because it held a real landmark (a Mapbox POI). Folding that flag in
    here avoids a second table — "checkpoints lit" is just the count of rows with
    a non-null checkpoint_id.
    """

    __tablename__ = "explored_tiles"
    __table_args__ = (UniqueConstraint("user_id", "tile_key", name="uq_explored_user_tile"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    # Hex-grid key, "q,r" — the same key FogContext persists locally.
    tile_key: Mapped[str] = mapped_column(String, nullable=False)
    # Set when the tile was unlocked by walking to a landmark; null for plain
    # tiles unlocked by reveal-bombs around an explored area.
    checkpoint_id: Mapped[str | None] = mapped_column(String, nullable=True)
    checkpoint_name: Mapped[str | None] = mapped_column(String, nullable=True)
    # True for the free "home neighbourhood" tiles seeded when the user first sets
    # their start point. These are a gift, not exploration, so they're excluded
    # from tiles_explored (achievements + leaderboard) — mirroring the map HUD,
    # which counts only ground the user has actually walked out and uncovered.
    is_home: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
