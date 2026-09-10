import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.cat import Cat
from app.models.cat_merge import CatMergeRequest
from app.models.notification import Notification
from app.models.user import User
from app.schemas.cat_merge import (
    MergeRequestCreate,
    MergeRequestResult,
    MyMergeRequest,
)
from app.services.auth_service import get_current_user
from app.services.demo_seed import visible_cats_query
from app.services.rate_limit import enforce_daily_limit

log = logging.getLogger(__name__)

router = APIRouter(tags=["cat-merges"])

# Lower than the trait-suggestion cap. Each of these puts two whole profiles in
# front of a moderator, and nobody finds five duplicates in a day honestly.
MAX_MERGE_REQUESTS_PER_DAY = 5


@router.post(
    "/cats/{cat_id}/merge-request", response_model=MergeRequestResult, status_code=201
)
def submit_merge_request(
    cat_id: int,
    body: MergeRequestCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Report that two cat profiles are the same animal.

    Nothing is merged until a moderator decides which of the two to keep — the
    requester can see two profiles, not which one carries a verified owner or
    the longer history.
    """
    other_id = body.other_cat_id
    if cat_id == other_id:
        raise HTTPException(
            status_code=400, detail="Pick a different cat to compare this one with."
        )

    # Resolved through the visibility filter so a demo cat isn't reportable by
    # someone who can't see it in the first place.
    found = (
        visible_cats_query(db.query(Cat), current_user)
        .filter(Cat.id.in_((cat_id, other_id)))
        .all()
    )
    cats = {cat.id: cat for cat in found}
    if cat_id not in cats or other_id not in cats:
        raise HTTPException(status_code=404, detail="Cat not found.")

    if body.suggested_keep_id is not None and body.suggested_keep_id not in (
        cat_id,
        other_id,
    ):
        raise HTTPException(
            status_code=400, detail="You can only suggest keeping one of these two cats."
        )

    a_id, b_id = CatMergeRequest.normalise_pair(cat_id, other_id)

    # Per pair, not per person: a pair only needs deciding once, however many
    # people notice it. Trait suggestions are the other way round, because two
    # people can propose genuinely different corrections to the same cat.
    existing = (
        db.query(CatMergeRequest)
        .filter(
            CatMergeRequest.cat_a_id == a_id,
            CatMergeRequest.cat_b_id == b_id,
            CatMergeRequest.status == "pending",
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="These two have already been reported as the same cat. It's still being reviewed.",
        )

    # Last in the ladder: this commits the spend immediately and on purpose, so
    # a request rejected above shouldn't have burned any of the budget.
    enforce_daily_limit(
        db,
        f"cat_merges:user:{current_user.id}",
        MAX_MERGE_REQUESTS_PER_DAY,
        f"Daily limit of {MAX_MERGE_REQUESTS_PER_DAY} duplicate reports reached. Come back tomorrow!",
    )

    request = CatMergeRequest(
        cat_a_id=a_id,
        cat_b_id=b_id,
        cat_a_name=cats[a_id].name,
        cat_b_name=cats[b_id].name,
        suggested_keep_id=body.suggested_keep_id,
        user_id=current_user.id,
        status="pending",
        note=(body.note or "").strip() or None,
    )
    db.add(request)
    db.flush()

    label = cats[cat_id].name or "this cat"
    db.add(
        Notification(
            user_id=current_user.id,
            type="merge_request_pending",
            title=f"Your duplicate report for {label} is being reviewed",
            body="A moderator will compare the two profiles and decide which to keep.",
            cat_id=cat_id,
        )
    )
    db.commit()

    # No push, deliberately: the user is looking at the result screen right now.
    log.info(
        "Merge request %s filed on cats %s+%s by user %s",
        request.id, a_id, b_id, current_user.id,
    )
    return MergeRequestResult(request_id=request.id, status=request.status)


@router.get("/cats/{cat_id}/merge-request/mine", response_model=MyMergeRequest | None)
def my_merge_request(
    cat_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The caller's latest report touching this cat, so the profile can say so.

    Matches on either side of the pair — the report reads the same from both
    cats. Returns null rather than 404 when there is none: "you haven't reported
    anything" is an ordinary answer, not a missing resource.
    """
    request = (
        db.query(CatMergeRequest)
        .filter(
            CatMergeRequest.user_id == current_user.id,
            or_(
                CatMergeRequest.cat_a_id == cat_id,
                CatMergeRequest.cat_b_id == cat_id,
            ),
        )
        .order_by(CatMergeRequest.created_at.desc())
        .first()
    )
    if not request:
        return None

    # Which of the pair is the *other* one, from where the caller is standing.
    if request.cat_a_id == cat_id:
        other_id, other_name = request.cat_b_id, request.cat_b_name
    else:
        other_id, other_name = request.cat_a_id, request.cat_a_name

    return MyMergeRequest(
        request_id=request.id,
        status=request.status,
        created_at=request.created_at,
        decided_at=request.decided_at,
        other_cat_id=other_id,
        other_cat_name=other_name,
        merged_into_cat_id=request.merged_into_cat_id,
        rejection_reason=request.rejection_reason,
    )
