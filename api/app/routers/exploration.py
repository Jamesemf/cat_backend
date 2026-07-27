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


def _tiles_explored(db: Session, user_id: int) -> int:
    """tiles_explored for a user. Home-seed tiles are a free gift, not
    exploration, so they're excluded from the total."""
    return (
        db.query(func.count(ExploredTile.id))
        .filter(ExploredTile.user_id == user_id, ExploredTile.is_home.is_(False))
        .scalar()
        or 0
    )


@router.get("/tiles", response_model=ExplorationState)
def get_tiles(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """The user's explored tiles — used to restore the fog on a new device."""
    rows = (
        db.query(ExploredTile.tile_key)
        .filter(ExploredTile.user_id == current_user.id)
        .all()
    )
    keys = [r[0] for r in rows]
    home_rows = (
        db.query(ExploredTile.tile_key)
        .filter(ExploredTile.user_id == current_user.id, ExploredTile.is_home.is_(True))
        .all()
    )
    home_keys = [r[0] for r in home_rows]
    return ExplorationState(
        tile_keys=keys,
        home_tile_keys=home_keys,
        tiles_explored=_tiles_explored(db, current_user.id),
    )


@router.post("/tiles", response_model=ExplorationCounts)
def report_tiles(
    body: TilesReportRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Record newly-uncovered tiles. Idempotent: tiles already stored are skipped,
    so the client can safely re-send its batch (e.g. retrying a failed sync)."""
    # De-dupe within the batch first, so we only look up (and touch) the keys
    # actually in this batch.
    incoming: dict[str, TileReport] = {}
    for t in body.tiles:
        if t.tile_key not in incoming:
            incoming[t.tile_key] = t

    # Existing keys for *just this batch* — bounded by the batch size, not the
    # user's whole exploration history, so the cost stays flat as they explore.
    existing: set[str] = set()
    if incoming:
        existing = {
            r[0]
            for r in db.query(ExploredTile.tile_key).filter(
                ExploredTile.user_id == current_user.id,
                ExploredTile.tile_key.in_(incoming.keys()),
            )
        }

    for key, t in incoming.items():
        if key not in existing:
            db.add(
                ExploredTile(
                    user_id=current_user.id,
                    tile_key=key,
                    is_home=t.is_home,
                )
            )

    db.commit()

    return ExplorationCounts(tiles_explored=_tiles_explored(db, current_user.id))
