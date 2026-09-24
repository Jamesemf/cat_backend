from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.media import MediaUrlOpt, UtcDatetime, UtcDatetimeOpt


class NotificationOut(BaseModel):
    id: int
    type: str
    title: str
    body: str
    cat_id: int | None = None
    sighting_id: int | None = None
    post_id: int | None = None
    created_at: UtcDatetime
    read_at: UtcDatetimeOpt = None
    # Enrichment for inbox rows
    cat_name: str | None = None
    cat_photo_path: MediaUrlOpt = None
    latitude: float | None = None
    longitude: float | None = None


class UnreadCount(BaseModel):
    count: int


class PushTokenIn(BaseModel):
    token: str
    platform: str | None = None


class MarkReadIn(BaseModel):
    ids: list[int] | None = None
    all: bool = False


class DeleteIn(BaseModel):
    """Inbox rows to remove — ``all`` empties it, otherwise just ``ids``."""

    ids: list[int] | None = Field(default=None, max_length=500)
    all: bool = False


class NotificationPrefs(BaseModel):
    nearby_sightings: bool
    new_cat_in_area: bool
    friend_new_cats: bool
    friend_sightings: bool


class NotificationPrefsUpdate(BaseModel):
    """Partial update — omitted fields are left unchanged."""

    nearby_sightings: bool | None = None
    new_cat_in_area: bool | None = None
    friend_new_cats: bool | None = None
    friend_sightings: bool | None = None
