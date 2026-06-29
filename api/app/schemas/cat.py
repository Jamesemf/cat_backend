from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, field_validator, model_validator

from app.schemas.claim import OwnerCard
from app.schemas.media import MediaUrl, MediaUrlList, MediaUrlOpt
from app.schemas.sighting import PhotoAdjust, _parse_photo_adjust


class CatNearby(BaseModel):
    """A cat for the onboarding "is your cat here?" picker: identity is downplayed
    (the nickname isn't the cat's real name) in favour of as many photos as we can
    show, so a user can recognise their own cat."""

    id: int
    name: str | None
    breed: str | None
    last_lat: float | None
    last_lng: float | None
    last_seen: datetime | None
    photos: MediaUrlList = []

    model_config = {"from_attributes": True}


class SightingOut(BaseModel):
    id: int
    cat_id: int | None
    photo_path: MediaUrl
    latitude: float
    longitude: float
    spotted_at: datetime
    spotter_name: str | None
    # The spotter's polaroid keepsake for this sighting (null = default polaroid).
    frame_id: str | None = None
    photo_adjust: PhotoAdjust | None = None
    caption: str | None = None

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
            "frame_id": data.frame_id,
            "photo_adjust": data.photo_adjust,
            "caption": data.caption,
        }

    _parse_adjust = field_validator("photo_adjust", mode="before")(_parse_photo_adjust)

    model_config = {"from_attributes": True}


class MyPhotoOut(BaseModel):
    """One of the current user's own photos of a cat — candidates for the photo
    they highlight on that cat's Cat-a-log card. photo_path is the raw storage
    key (not a resolved URL) so the client can round-trip it as the saved cover;
    the client resolves it for display."""

    photo_path: str
    spotted_at: datetime

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


class LeaderboardRow(BaseModel):
    user_id: int
    display_name: str | None
    avatar_emoji: str | None
    value: int


class Leaderboards(BaseModel):
    total_sightings: list[LeaderboardRow]
    tiles_explored: list[LeaderboardRow]
