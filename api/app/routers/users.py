"""Public user profiles — tap another spotter to see their Cat-a-log."""

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

router = APIRouter(prefix="/users", tags=["users"])

# Cap how many cats we embed in a public profile response.
MAX_PROFILE_CATS = 60


class PublicProfileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    display_name: str | None
    avatar_emoji: str | None
    cats_spotted: int
    tiles_explored: int
    cats: list[CatOut]


@router.get("/{user_id}", response_model=PublicProfileOut)
def get_public_profile(user_id: int, db: Session = Depends(get_db)):
    """A spotter's public profile: display name + avatar, a couple of
    exploration stats, and the cats they've spotted (their Cat-a-log)."""
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

    tiles_explored = (
        db.query(ExploredTile).filter(ExploredTile.user_id == user_id).count()
    )

    return PublicProfileOut(
        id=user.id,
        display_name=user.display_name,
        avatar_emoji=user.avatar_emoji,
        cats_spotted=len(cat_ids),
        tiles_explored=tiles_explored,
        cats=[CatOut.model_validate(c) for c in cats],
    )
