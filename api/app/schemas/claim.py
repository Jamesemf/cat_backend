from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.media import MediaUrlOpt, UtcDatetime, UtcDatetimeOpt

INDOOR_OUTDOOR_VALUES = {"indoor", "outdoor", "both"}


class OwnerCard(BaseModel):
    """Public owner info shown on a cat's profile."""

    display_name: str | None = None
    avatar_emoji: str | None = None
    real_name: str | None = None
    likes_petting: bool | None = None
    accepts_treats: bool | None = None
    age_years: int | None = None
    fun_fact: str | None = None
    indoor_outdoor: str | None = None
    claimed_at: UtcDatetimeOpt = None


class ClaimOut(BaseModel):
    id: int
    cat_id: int
    status: str
    avg_confidence: float | None = None
    rejection_reason: str | None = None
    real_name: str | None = None
    likes_petting: bool | None = None
    accepts_treats: bool | None = None
    age_years: int | None = None
    fun_fact: str | None = None
    indoor_outdoor: str | None = None
    created_at: UtcDatetime
    decided_at: UtcDatetimeOpt = None

    model_config = {"from_attributes": True}


class ClaimStatusResponse(BaseModel):
    """Claim state for one cat, as seen by the requesting user."""

    owner: OwnerCard | None = None
    my_claim: ClaimOut | None = None
    can_claim: bool = False
    cooldown_until: UtcDatetimeOpt = None


class ClaimResult(BaseModel):
    """Outcome of a claim submission."""

    status: str
    avg_confidence: float | None = None
    per_photo_confidences: list[float] = []
    rejection_reason: str | None = None


class OwnerCardUpdate(BaseModel):
    real_name: str | None = Field(default=None, max_length=40)
    likes_petting: bool | None = None
    accepts_treats: bool | None = None
    age_years: int | None = Field(default=None, ge=0, le=30)
    fun_fact: str | None = Field(default=None, max_length=280)
    indoor_outdoor: str | None = None


class MyClaimItem(ClaimOut):
    cat_name: str | None = None
    cat_photo_path: MediaUrlOpt = None
    cat_rarity_score: float | None = None
