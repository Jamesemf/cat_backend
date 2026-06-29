"""Public user profiles — tap another spotter to see their Cat-a-log."""

import json

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import distinct
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.cat import Cat
from app.models.exploration import ExploredTile
from app.models.sighting import Sighting
from app.models.user import User
from app.schemas.cat import CatOut
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/users", tags=["users"])

# Cap how many cats we embed in a public profile response.
MAX_PROFILE_CATS = 60


class PhotoAdjust(BaseModel):
    """How a cat's cover photo is positioned in its square frame: zoom (>=1) and a
    normalised pan in [-1, 1] per axis."""

    scale: float = 1.0
    x: float = 0.0
    y: float = 0.0


class PublicProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str | None
    avatar_emoji: str | None
    cats_spotted: int
    tiles_explored: int
    # Cats ordered the way the owner arranged their Cat-a-log (unranked cats fall
    # to the end, newest first), with their chosen polaroid frame and the pan/zoom
    # of each cover photo per cat.
    cats: list[CatOut]
    frames: dict[str, str] = {}
    adjusts: dict[str, PhotoAdjust] = {}


class CatalogLayoutIn(BaseModel):
    """The owner's Cat-a-log arrangement: card order, per-cat frame choices,
    per-cat cover photo (catId -> raw storage key), and per-cat photo pan/zoom."""

    order: list[int] = []
    frames: dict[str, str] = {}
    covers: dict[str, str] = {}
    adjusts: dict[str, PhotoAdjust] = {}


def _parse_layout(
    raw: str | None,
) -> tuple[list[int], dict[str, str], dict[str, str], dict[str, PhotoAdjust]]:
    """Decode a stored catalog_layout JSON blob into (order, frames, covers,
    adjusts). Tolerates null/corrupt data by returning empties (the default)."""
    if not raw:
        return [], {}, {}, {}
    try:
        data = json.loads(raw)
        order = [int(x) for x in data.get("order", []) if isinstance(x, (int, str))]
        frames_raw = data.get("frames", {})
        frames = {str(k): str(v) for k, v in frames_raw.items()} if isinstance(frames_raw, dict) else {}
        covers_raw = data.get("covers", {})
        covers = {str(k): str(v) for k, v in covers_raw.items()} if isinstance(covers_raw, dict) else {}
        adjusts_raw = data.get("adjusts", {})
        adjusts: dict[str, PhotoAdjust] = {}
        if isinstance(adjusts_raw, dict):
            for k, v in adjusts_raw.items():
                try:
                    adjusts[str(k)] = PhotoAdjust.model_validate(v)
                except (ValueError, TypeError):
                    continue
        return order, frames, covers, adjusts
    except (ValueError, TypeError, AttributeError):
        return [], {}, {}, {}


@router.put("/me/catalog", status_code=204)
def update_my_catalog(
    body: CatalogLayoutIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save how the current user has arranged their Cat-a-log so it shows the
    same way on their public profile. Cosmetic and idempotent."""
    current_user.catalog_layout = json.dumps(
        {
            "order": body.order,
            "frames": body.frames,
            "covers": body.covers,
            "adjusts": {k: v.model_dump() for k, v in body.adjusts.items()},
        }
    )
    db.commit()


@router.get("/{user_id}", response_model=PublicProfileOut)
def get_public_profile(user_id: int, db: Session = Depends(get_db)):
    """A spotter's public profile: display name + avatar, a couple of
    exploration stats, and the cats they've spotted (their Cat-a-log) arranged
    and framed the way the owner designed it."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    cat_id_rows = (
        db.query(distinct(Sighting.cat_id))
        .filter(Sighting.user_id == user_id, Sighting.cat_id.isnot(None))
        .all()
    )
    cat_ids = [row[0] for row in cat_id_rows]

    cats: list[Cat] = []
    if cat_ids:
        cats = (
            db.query(Cat)
            .filter(Cat.id.in_(cat_ids))
            .order_by(Cat.last_seen.desc())
            .limit(MAX_PROFILE_CATS)
            .all()
        )

    # Apply the owner's saved arrangement: cats they've ranked come first in that
    # order, anything unranked keeps the newest-first fallback. A stable sort on
    # the already-sorted list preserves that tail order.
    order, frames, covers, adjusts = _parse_layout(user.catalog_layout)
    if order:
        rank = {cat_id: i for i, cat_id in enumerate(order)}
        cats.sort(key=lambda c: rank.get(c.id, len(order)))

    # Swap in each cat's chosen cover photo (a raw storage key, re-resolved to a
    # URL on output) so the public card matches what the owner highlighted.
    cat_out: list[CatOut] = []
    for c in cats:
        co = CatOut.model_validate(c)
        cover = covers.get(str(c.id))
        if cover:
            co.last_photo_path = cover
        cat_out.append(co)

    tiles_explored = (
        db.query(ExploredTile).filter(ExploredTile.user_id == user_id).count()
    )

    return PublicProfileOut(
        id=user.id,
        display_name=user.display_name,
        avatar_emoji=user.avatar_emoji,
        cats_spotted=len(cat_ids),
        tiles_explored=tiles_explored,
        cats=cat_out,
        frames=frames,
        adjusts=adjusts,
    )
