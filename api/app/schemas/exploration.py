from __future__ import annotations

from pydantic import BaseModel, Field


class TileReport(BaseModel):
    """A single newly-uncovered tile the client is reporting."""

    tile_key: str = Field(..., max_length=32)
    # Present when the tile was unlocked by reaching a landmark (a checkpoint).
    checkpoint_id: str | None = Field(default=None, max_length=128)
    checkpoint_name: str | None = Field(default=None, max_length=200)


class TilesReportRequest(BaseModel):
    """Batch of tiles unlocked since the last sync. The client may re-send tiles
    it already reported; the server upserts and ignores duplicates."""

    tiles: list[TileReport] = Field(default_factory=list, max_length=2000)


class ExplorationCounts(BaseModel):
    """Aggregate exploration totals that drive achievements. Returned from a tile
    report so the client gets fresh counts without re-fetching the whole set."""

    tiles_explored: int
    checkpoints_lit: int


class ExplorationState(ExplorationCounts):
    """The full explored-tile set (for restoring the fog on a new device), the
    distinct landmarks (checkpoints) lit, and the aggregate counts."""

    tile_keys: list[str]
    checkpoint_ids: list[str]
