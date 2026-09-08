from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel, Field

from app.schemas.media import MediaUrlOpt, UtcDatetime, UtcDatetimeOpt
from app.services.vision import (
    BODY_SIZES,
    EYE_COLORS,
    FUR_LENGTHS,
    PATTERNS,
    PRIMARY_COLORS,
    SECONDARY_COLORS,
)

# The seven fields a trait change request can touch, in the order a person reads
# them. Also what the claim queue compares by eye — routers.moderation imports
# this as COMPARED_FEATURES rather than keeping a second copy in step.
TRAIT_FIELDS = (
    "primary_color",
    "secondary_color",
    "pattern",
    "fur_length",
    "eye_color",
    "body_size",
    "breed",
)

# Built from the vision lists rather than retyped, so the values a user may
# propose cannot drift from the values the model is allowed to write.
#
# `breed` is deliberately absent. It has no single closed vocabulary: the app
# offers real breeds ("Domestic Shorthair") while vision writes appearance
# labels ("Orange Tabby"), and live cats carry values from both. Validating it
# strictly against either list would make it impossible to submit a form that
# leaves an existing breed alone. It is length-checked instead.
STRICT_TRAIT_VOCAB: dict[str, set[str]] = {
    "primary_color": set(PRIMARY_COLORS),
    "secondary_color": set(SECONDARY_COLORS),
    "pattern": set(PATTERNS),
    "fur_length": set(FUR_LENGTHS),
    "eye_color": set(EYE_COLORS),
    "body_size": set(BODY_SIZES),
}

MAX_BREED_LENGTH = 60


def validate_trait_values(values: dict[str, str | None]) -> None:
    """Reject unknown fields and out-of-vocabulary values, or raise 400.

    Shared by the submit endpoint and the moderator's apply endpoint, so a
    moderator cannot write a value the app itself would have refused.
    """
    for field, value in values.items():
        if field not in TRAIT_FIELDS:
            raise HTTPException(status_code=400, detail=f"{field} is not a trait.")
        if value is None:
            continue
        if not isinstance(value, str):
            raise HTTPException(status_code=400, detail=f"{field} must be text or empty.")
        if field == "breed":
            if not value.strip():
                raise HTTPException(
                    status_code=400, detail="Breed can't be blank — leave it out instead."
                )
            if len(value) > MAX_BREED_LENGTH:
                raise HTTPException(status_code=400, detail="That breed name is too long.")
            continue
        if value not in STRICT_TRAIT_VOCAB[field]:
            raise HTTPException(
                status_code=400, detail=f"{value!r} isn't a recognised {field}."
            )


class TraitChangeCreate(BaseModel):
    """A proposed correction: only the fields being changed.

    An explicit null clears a trait ("this cat has no second colour"); a field
    left out is left alone.
    """

    proposed: dict[str, str | None]
    note: str | None = Field(default=None, max_length=280)


class TraitChangeResult(BaseModel):
    """The outcome of filing or deciding a request."""

    request_id: int
    status: str
    cat_id: int


class MyTraitChange(BaseModel):
    """The caller's own latest request for a cat, so the profile can say so."""

    request_id: int
    status: str
    created_at: UtcDatetime
    decided_at: UtcDatetimeOpt = None
    proposed: dict = {}
    rejection_reason: str | None = None


class TraitChangeQueueItem(BaseModel):
    """One row of the trait review queue: what's on record, and what's proposed.

    `current` and `proposed` are flat dicts keyed by TRAIT_FIELDS so the
    dashboard can lay them side by side without knowing the field list.
    """

    request_id: int
    status: str
    created_at: UtcDatetime
    decided_at: UtcDatetimeOpt = None

    requester_id: int
    requester_name: str | None = None
    # Surfaced for the same reason the claim queue carries claimant_strikes: a
    # history of harmful uploads is context for whether to trust a suggestion.
    requester_strikes: int = 0
    requester_banned: bool = False
    # This cat's verified owner. A badge for the moderator, not a bypass.
    is_owner: bool = False

    cat_id: int
    cat_name: str | None = None
    cat_photo_path: MediaUrlOpt = None

    current: dict = {}
    proposed: dict = {}
    # What the moderator actually wrote. Null while pending.
    applied: dict | None = None

    note: str | None = None
    rejection_reason: str | None = None
    reviewed_by_name: str | None = None


class TraitChangeApplyIn(BaseModel):
    """The moderator's final values — not necessarily the ones proposed."""

    values: dict[str, str | None]


class RejectTraitChangeIn(BaseModel):
    """Why a request was turned down. Shown to the requester verbatim."""

    reason: str | None = Field(default=None, max_length=500)
