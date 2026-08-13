from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.media import MediaUrl, MediaUrlOpt, UtcDatetime, UtcDatetimeOpt

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
    # Null while a registration waits for review — its cat doesn't exist yet.
    cat_id: int | None = None
    status: str
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
    """Outcome of a claim submission — always "pending" now that a person decides."""

    status: str
    rejection_reason: str | None = None


class RegisterResult(BaseModel):
    """Outcome of registering a new cat.

    Deliberately not a Cat: no cat exists until a moderator approves the claim.
    """

    claim_id: int
    status: str


class OwnerCardUpdate(BaseModel):
    real_name: str | None = Field(default=None, max_length=40)
    likes_petting: bool | None = None
    accepts_treats: bool | None = None
    age_years: int | None = Field(default=None, ge=0, le=30)
    fun_fact: str | None = Field(default=None, max_length=280)
    indoor_outdoor: str | None = None


class MyClaimItem(ClaimOut):
    # A pending registration has no cat yet, so these fall back to the name the
    # claimant proposed and their own evidence photo.
    cat_name: str | None = None
    cat_photo_path: MediaUrlOpt = None
    cat_rarity_score: float | None = None
    source: str = "claim"


class ClaimPhotoOut(BaseModel):
    """One evidence photo, with what vision saw in it."""

    id: int
    photo_path: MediaUrl
    features: dict = {}
    # Cats in frame. Above one, `features` may be describing the wrong one — the
    # reviewer is shown the count so the row isn't read as fact about the cat.
    cat_count: int | None = None


class ClaimQueueItem(BaseModel):
    """One row of the claim review queue: the assertion, and what backs it.

    Carries no match score. The moderator compares `photos` against `cat_photo_path`
    and `claim_features` against `cat_features`, and decides.
    """

    claim_id: int
    source: str
    status: str
    created_at: UtcDatetime
    decided_at: UtcDatetimeOpt = None

    claimant_id: int
    claimant_name: str | None = None
    claimant_strikes: int = 0
    claimant_banned: bool = False

    # Null for a pending registration — the cat doesn't exist yet.
    cat_id: int | None = None
    cat_name: str | None = None
    cat_photo_path: MediaUrlOpt = None
    cat_sighting_count: int | None = None
    cat_features: dict = {}

    # The name the claimant says the cat actually has, plus the rest of the
    # owner card they're asserting.
    proposed_name: str | None = None
    likes_petting: bool | None = None
    accepts_treats: bool | None = None
    age_years: int | None = None
    fun_fact: str | None = None
    indoor_outdoor: str | None = None

    photos: list[ClaimPhotoOut] = []
    rejection_reason: str | None = None
    reviewed_by_name: str | None = None


class RejectClaimIn(BaseModel):
    """Why a claim was turned down. Shown to the claimant verbatim."""

    reason: str | None = Field(default=None, max_length=500)


class ClaimReviewResult(BaseModel):
    claim_id: int
    status: str
    cat_id: int | None = None
