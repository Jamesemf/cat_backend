from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.media import MediaUrl, UtcDatetime


class ExplorerPostOut(BaseModel):
    id: int
    photo_path: MediaUrl
    caption: str | None = None
    created_at: UtcDatetime
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
    # Withheld pending moderator review. Only ever true for the post's author
    # (who gets a "hidden" badge instead of a silent disappearance) or an admin.
    hidden: bool = False


class MeowResult(BaseModel):
    meowed: bool
    meow_count: int


class CommentCreate(BaseModel):
    body: str = Field(min_length=1, max_length=500)


class CommentOut(BaseModel):
    id: int
    post_id: int
    body: str
    created_at: UtcDatetime
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


class ReportOut(BaseModel):
    id: int
    reason: str
    detail: str | None = None
    created_at: UtcDatetime
    reporter_id: int
    reporter_name: str | None = None


class ReportedPostOut(BaseModel):
    """One row of the moderation queue: a post plus the case against it."""

    post_id: int
    photo_path: MediaUrl
    caption: str | None = None
    created_at: UtcDatetime
    hidden: bool = False
    hidden_reason: str | None = None
    # The post's author — nullable, matching legacy anonymous sightings.
    author_id: int | None = None
    author_name: str | None = None
    author_strikes: int = 0
    cat_id: int | None = None
    sighting_id: int | None = None
    open_report_count: int = 0
    # Distinct reasons given, most-reported first — the gist without opening it.
    reasons: list[str] = []
    reports: list[ReportOut] = []


class ModerationActionResult(BaseModel):
    post_id: int
    hidden: bool
    open_report_count: int
