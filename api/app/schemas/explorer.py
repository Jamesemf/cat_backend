from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.media import MediaUrl


class ExplorerPostOut(BaseModel):
    id: int
    photo_path: MediaUrl
    caption: str | None = None
    created_at: datetime
    latitude: float | None = None
    longitude: float | None = None
    user_id: int | None = None
    user_name: str | None = None
    user_emoji: str | None = None
    # Set when the post originated from a sighting.
    sighting_id: int | None = None
    cat_id: int | None = None
    cat_name: str | None = None
    meow_count: int = 0
    comment_count: int = 0
    meowed_by_me: bool = False
    is_mine: bool = False


class MeowResult(BaseModel):
    meowed: bool
    meow_count: int


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=500)


class CommentOut(BaseModel):
    id: int
    post_id: int
    body: str
    created_at: datetime
    user_id: int | None = None
    user_name: str | None = None
    user_emoji: str | None = None
    # True when the requesting user may delete this comment (its author, or
    # the owner of the post it sits on).
    can_delete: bool = False


REPORT_REASONS = {"not_a_cat", "inappropriate", "spam", "animal_harm", "other"}


class ReportCreate(BaseModel):
    reason: str
    detail: str | None = Field(default=None, max_length=500)
