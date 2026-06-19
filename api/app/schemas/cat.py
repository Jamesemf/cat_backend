from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, model_validator

from app.schemas.claim import OwnerCard
from app.schemas.media import MediaUrl, MediaUrlOpt


class SightingOut(BaseModel):
    id: int
    cat_id: int | None
    photo_path: MediaUrl
    latitude: float
    longitude: float
    spotted_at: datetime
    spotter_name: str | None

    @model_validator(mode="before")
    @classmethod
    def resolve_spotter_name(cls, data: object) -> object:
        if not hasattr(data, "__dict__"):
            return data
        user = getattr(data, "user", None)
        display_name = getattr(user, "display_name", None) if user else None
        return {
            "id": data.id,
            "cat_id": data.cat_id,
            "photo_path": data.photo_path,
            "latitude": data.latitude,
            "longitude": data.longitude,
            "spotted_at": data.spotted_at,
            "spotter_name": display_name,
        }

    model_config = {"from_attributes": True}


class CatCreate(BaseModel):
    name: str | None = None
    breed: str | None = None


class CatMergeRequest(BaseModel):
    # The cat to keep; the cat in the path is the duplicate that gets merged in.
    target_id: int


class CatBase(BaseModel):
    name: str | None = None
    breed: str | None = None


class CatOut(CatBase):
    id: int
    rarity_score: float
    sighting_count: int
    first_seen: datetime
    last_seen: datetime
    last_lat: float | None
    last_lng: float | None
    last_photo_path: MediaUrlOpt = None
    vibes: str | None = None

    is_cat: bool | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    pattern: str | None = None
    fur_length: str | None = None
    eye_color: str | None = None
    body_size: str | None = None
    features_json: str | None = None

    model_config = {"from_attributes": True}


class CatWithSightings(CatOut):
    sightings: list[SightingOut] = []
    owner: OwnerCard | None = None
    follower_count: int = 0
    is_following: bool = False


class FollowResult(BaseModel):
    following: bool
    follower_count: int


class TerritoryOut(BaseModel):
    cat_id: int
    sighting_count: int
    geojson: dict | None  # GeoJSON Feature (Polygon), null when < 3 non-collinear sightings


class CountItem(BaseModel):
    label: str
    count: int


class TopCat(BaseModel):
    id: int
    name: str | None
    sighting_count: int
    last_photo_path: MediaUrlOpt


class GlobalStats(BaseModel):
    total_cats: int
    total_sightings: int
    top_breeds: list[CountItem]
    top_colors: list[CountItem]
    top_patterns: list[CountItem]
    top_fur_lengths: list[CountItem]
    most_spotted: TopCat | None
