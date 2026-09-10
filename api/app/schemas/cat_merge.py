from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.media import MediaUrlOpt, UtcDatetime, UtcDatetimeOpt


class MergeRequestCreate(BaseModel):
    """A report that the cat in the path and `other_cat_id` are the same animal.

    `suggested_keep_id` is which of the two the requester would keep. It is a
    hint the queue shows the moderator, not a decision — left out when they had
    no preference.
    """

    other_cat_id: int
    suggested_keep_id: int | None = None
    note: str | None = Field(default=None, max_length=280)


class MergeRequestResult(BaseModel):
    """The outcome of filing or deciding a report."""

    request_id: int
    status: str


class MyMergeRequest(BaseModel):
    """The caller's own latest report touching a cat, so the profile can say so."""

    request_id: int
    status: str
    created_at: UtcDatetime
    decided_at: UtcDatetimeOpt = None
    other_cat_id: int | None = None
    other_cat_name: str | None = None
    merged_into_cat_id: int | None = None
    rejection_reason: str | None = None


class MergeQueueCat(BaseModel):
    """One side of a reported pair, with everything needed to judge it by eye.

    `traits` is a flat dict keyed by TRAIT_FIELDS so the dashboard can lay the
    two cats side by side without knowing the field list, the same way the trait
    queue carries `current` and `proposed`.
    """

    cat_id: int
    name: str | None = None
    photo_path: MediaUrlOpt = None
    traits: dict = {}

    sighting_count: int = 0
    first_seen: UtcDatetimeOpt = None
    last_seen: UtcDatetimeOpt = None

    # Why a merge might be refused before it is attempted. Shown up front so a
    # moderator isn't surprised by a 409 on click.
    has_verified_owner: bool = False
    pending_claims: int = 0


class MergeQueueItem(BaseModel):
    """One row of the duplicate-cats queue.

    Either cat may be absent: approving a different report can delete one out
    from under this row, which is what `superseded` means. The name snapshots
    taken at filing time are what keeps such a row readable.
    """

    request_id: int
    status: str
    created_at: UtcDatetime
    decided_at: UtcDatetimeOpt = None

    requester_id: int | None = None
    requester_name: str | None = None
    # Surfaced for the same reason the trait queue carries requester_strikes: a
    # history of harmful uploads is context for whether to trust a report.
    requester_strikes: int = 0
    requester_banned: bool = False
    # The requester owns one of these two cats. A badge, not a bypass.
    is_owner: bool = False

    cat_a: MergeQueueCat | None = None
    cat_b: MergeQueueCat | None = None
    # Names as they read when the report was filed, so a decided row still says
    # which two cats it was about once one of them is gone.
    cat_a_name: str | None = None
    cat_b_name: str | None = None

    suggested_keep_id: int | None = None
    merged_into_cat_id: int | None = None

    note: str | None = None
    rejection_reason: str | None = None
    reviewed_by_name: str | None = None


class ApproveMergeIn(BaseModel):
    """Which cat the moderator decided to keep. Must be one of the reported pair."""

    keep_cat_id: int


class RejectMergeIn(BaseModel):
    """Why a report was turned down. Shown to the requester verbatim."""

    reason: str | None = Field(default=None, max_length=500)
