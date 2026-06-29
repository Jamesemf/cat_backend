import logging

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.exploration import ExploredTile
from app.models.user import User
from app.schemas.exploration import (
    ExplorationCounts,
    ExplorationState,
    TileReport,
    TilesReportRequest,
)
from app.services.auth_service import get_current_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/exploration", tags=["exploration"])


def _counts(db: Session, user_id: int) -> tuple[int, int]:
    """(tiles_explored, checkpoints_lit) for a user. Home-seed tiles are a free
    gift, not exploration, so they're excluded from the tiles_explored total."""
    tiles = (
        db.query(func.count(ExploredTile.id))
        .filter(ExploredTile.user_id == user_id, ExploredTile.is_home.is_(False))
        .scalar()
        or 0
    )
    checkpoints = (
        db.query(func.count(ExploredTile.id))
        .filter(ExploredTile.user_id == user_id, ExploredTile.checkpoint_id.isnot(None))
        .scalar()
        or 0
    )
    return tiles, checkpoints


@router.get("/tiles", response_model=ExplorationState)
def get_tiles(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The user's explored tiles + lit landmarks — used to restore the fog and
    the visited-points tally on a new device."""
    rows = (
        db.query(ExploredTile.tile_key)
        .filter(ExploredTile.user_id == current_user.id)
        .all()
    )
    keys = [r[0] for r in rows]
    cp_rows = (
        db.query(ExploredTile.checkpoint_id)
        .filter(
            ExploredTile.user_id == current_user.id,
            ExploredTile.checkpoint_id.isnot(None),
        )
        .distinct()
        .all()
    )
    checkpoint_ids = [r[0] for r in cp_rows]
    tiles, checkpoints = _counts(db, current_user.id)
    return ExplorationState(
        tile_keys=keys,
        checkpoint_ids=checkpoint_ids,
        tiles_explored=tiles,
        checkpoints_lit=checkpoints,
    )


@router.post("/tiles", response_model=ExplorationCounts)
def report_tiles(
    body: TilesReportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record newly-uncovered tiles. Idempotent: tiles already stored are skipped,
    so the client can safely re-send its batch (e.g. retrying a failed sync).

    If a tile arrives first as a plain tile and later as a checkpoint (or vice
    versa), the checkpoint metadata is filled in but never cleared — so a tile
    only ever gains a landmark, it doesn't lose one."""
    # De-dupe within the batch first, preferring an entry that carries a
    # checkpoint, so we only look up (and touch) the keys actually in this batch.
    incoming: dict[str, TileReport] = {}
    for t in body.tiles:
        prev = incoming.get(t.tile_key)
        if prev is None or (prev.checkpoint_id is None and t.checkpoint_id is not None):
            incoming[t.tile_key] = t

    # Existing rows for *just this batch* — bounded by the batch size, not the
    # user's whole exploration history, so the cost stays flat as they explore.
    existing: dict[str, ExploredTile] = {}
    if incoming:
        existing = {
            r.tile_key: r
            for r in db.query(ExploredTile).filter(
                ExploredTile.user_id == current_user.id,
                ExploredTile.tile_key.in_(incoming.keys()),
            )
        }

    for key, t in incoming.items():
        row = existing.get(key)
        if row is None:
            db.add(
                ExploredTile(
                    user_id=current_user.id,
                    tile_key=key,
                    checkpoint_id=t.checkpoint_id,
                    checkpoint_name=t.checkpoint_name,
                    is_home=t.is_home,
                )
            )
        elif t.checkpoint_id is not None and row.checkpoint_id is None:
            # Upgrade a previously-plain tile to a checkpoint.
            row.checkpoint_id = t.checkpoint_id
            row.checkpoint_name = t.checkpoint_name

    db.commit()

    tiles, checkpoints = _counts(db, current_user.id)
    return ExplorationCounts(tiles_explored=tiles, checkpoints_lit=checkpoints)
