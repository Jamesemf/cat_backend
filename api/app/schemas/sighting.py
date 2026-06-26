from datetime import datetime

from pydantic import BaseModel

from app.schemas.media import MediaUrl, MediaUrlList, MediaUrlOpt


class MatchCandidate(BaseModel):
    cat_id: int
    name: str | None
    breed: str | None
    last_photo_path: MediaUrlOpt
    last_seen: datetime
    sighting_count: int
    confidence: float

    model_config = {"from_attributes": True}


class MatchCheckRequest(BaseModel):
    latitude: float
    longitude: float
    primary_color: str | None = None
    secondary_color: str | None = None
    pattern: str | None = None
    fur_length: str | None = None
    eye_color: str | None = None
    body_size: str | None = None
    breed: str | None = None


class MatchCheckResponse(BaseModel):
    candidates: list[MatchCandidate]


class FeedItem(BaseModel):
    id: int
    photo_path: MediaUrl
    # All photos of this sighting's cat (most recent first), so the client can
    # show a swipeable carousel when a cat has been seen more than once. Always
    # contains at least photo_path.
    photos: MediaUrlList = []
    latitude: float
    longitude: float
    spotted_at: datetime
    spotter_name: str | None
    breed_description: str | None
    vibes: str | None
    primary_color: str | None = None
    secondary_color: str | None = None
    pattern: str | None = None
    fur_length: str | None = None
    eye_color: str | None = None
    body_size: str | None = None
    cat_id: int | None
    cat_name: str | None
    cat_rarity_score: float | None
    cat_sighting_count: int | None
    spotter_emoji: str | None = None
    # The Explorer post this sighting was mirrored into, plus its interaction
    # counts — lets the Neighbourhood feed like/comment/report each spot. Older
    # sightings logged before the mirror existed have post_id = null.
    post_id: int | None = None
    meow_count: int = 0
    comment_count: int = 0
    meowed_by_me: bool = False
    is_mine: bool = False


class SightingOut(BaseModel):
    id: int
    cat_id: int | None
    photo_path: MediaUrl
    latitude: float
    longitude: float
    spotted_at: datetime
    spotter_name: str | None
    breed_description: str | None
    vibes: str | None

    is_cat: bool | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    pattern: str | None = None
    fur_length: str | None = None
    eye_color: str | None = None
    body_size: str | None = None
    features_json: str | None = None

    model_config = {"from_attributes": True}


class SightingAssign(BaseModel):
    cat_id: int


class SightingAnalysis(BaseModel):
    """Result of POST /sightings/analyze — pre-commit validation payload.

    Contains the saved photo_path the client must echo back when committing,
    plus the recognised features so the client can display them in the form.
    """
    photo_path: str
    is_cat: bool | None
    cat_count: int | None
    not_cat_reason: str | None
    primary_color: str | None
    secondary_color: str | None
    pattern: str | None
    fur_length: str | None
    eye_color: str | None
    body_size: str | None
    breed: str | None
    features_json: str | None


class SightingCommit(BaseModel):
    """Body for POST /sightings — commits an already-analyzed photo.

    All recognition fields (including breed) are echoed from the /analyze
    response; the client adds latitude/longitude and user-authored vibes.
    Optional cat_id links this sighting to an existing cat (user-confirmed or
    auto-linked by the Re-ID matcher). Omit to let the server decide.
    """
    cat_id: int | None = None
    photo_path: str
    latitude: float
    longitude: float
    spotter_name: str | None = None
    vibes: str | None = None
    is_cat: bool | None = None
    primary_color: str | None = None
    secondary_color: str | None = None
    pattern: str | None = None
    fur_length: str | None = None
    eye_color: str | None = None
    body_size: str | None = None
    breed: str | None = None
    features_json: str | None = None
