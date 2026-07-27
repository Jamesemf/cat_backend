from __future__ import annotations

from pydantic import BaseModel, Field


class TileReport(BaseModel):
    """A single newly-uncovered tile the client is reporting."""

    tile_key: str = Field(..., max_length=32)
    # True for the free home-neighbourhood seed tiles, which don't count toward
    # tiles_explored (see ExploredTile.is_home).
    is_home: bool = Field(default=False)


class TilesReportRequest(BaseModel):
    """Batch of tiles unlocked since the last sync. The client may re-send tiles
    it already reported; the server upserts and ignores duplicates."""

    tiles: list[TileReport] = Field(default_factory=list, max_length=2000)


class ExplorationCounts(BaseModel):
    """Aggregate exploration totals that drive achievements. Returned from a tile
    report so the client gets fresh counts without re-fetching the whole set."""

    tiles_explored: int


class ExplorationState(ExplorationCounts):
    """The full explored-tile set (for restoring the fog on a new device) and the
    aggregate counts."""

    tile_keys: list[str]
    # The subset of tile_keys that are free home-neighbourhood seed tiles. Lets a
    # new device restore the user's home so it isn't re-prompted, and excludes
    # them from the locally-computed tiles_explored tally.
    home_tile_keys: list[str] = Field(default_factory=list)
