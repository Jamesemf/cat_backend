from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel

from app.schemas.media import MediaUrlOpt


class NotificationOut(BaseModel):
    id: int
    type: str
    title: str
    body: str
    cat_id: int | None = None
    sighting_id: int | None = None
    post_id: int | None = None
    created_at: datetime
    read_at: datetime | None = None
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
