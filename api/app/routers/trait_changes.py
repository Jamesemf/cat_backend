import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.cat import Cat
from app.models.notification import Notification
from app.models.trait_change import TraitChangeRequest
from app.models.user import User
from app.schemas.trait_change import (
    MyTraitChange,
    TraitChangeCreate,
    TraitChangeResult,
    validate_trait_values,
)
from app.services.auth_service import get_current_user
from app.services.rate_limit import enforce_daily_limit

log = logging.getLogger(__name__)

router = APIRouter(tags=["trait-changes"])

# Filing a request is cheap for the sender and costs a moderator's attention, so
# the cap is per account rather than per IP.
MAX_TRAIT_REQUESTS_PER_DAY = 10


@router.post(
    "/cats/{cat_id}/trait-change", response_model=TraitChangeResult, status_code=201
)
def submit_trait_change(
    cat_id: int,
    body: TraitChangeCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Propose a correction to a cat's traits. Nothing changes until a moderator applies it."""
    cat = db.query(Cat).filter(Cat.id == cat_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail=f"Cat {cat_id} not found.")

    proposed = body.proposed or {}
    if not proposed:
        raise HTTPException(status_code=400, detail="Change something first.")

    validate_trait_values(proposed)

    # Drop anything that already matches the record, then refuse an empty
    # remainder. Checked after validation so a typo is reported as a typo
    # rather than as "nothing changed".
    changes = {k: v for k, v in proposed.items() if getattr(cat, k, None) != v}
    if not changes:
        raise HTTPException(
            status_code=400, detail="Those are already this cat's traits."
        )

    existing = (
        db.query(TraitChangeRequest)
        .filter(
            TraitChangeRequest.cat_id == cat_id,
            TraitChangeRequest.user_id == current_user.id,
            TraitChangeRequest.status == "pending",
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail="You've already suggested a change to this cat. It's still being reviewed.",
        )

    # Last in the ladder: this commits the spend immediately and on purpose, so
    # a request rejected above shouldn't have burned any of the budget.
    enforce_daily_limit(
        db,
        f"trait_changes:user:{current_user.id}",
        MAX_TRAIT_REQUESTS_PER_DAY,
        f"Daily limit of {MAX_TRAIT_REQUESTS_PER_DAY} trait suggestions reached. Come back tomorrow!",
    )

    request = TraitChangeRequest(
        cat_id=cat_id,
        user_id=current_user.id,
        status="pending",
        proposed_json=json.dumps(changes),
        note=(body.note or "").strip() or None,
    )
    db.add(request)
    db.flush()

    cat_label = cat.name or "this cat"
    db.add(
        Notification(
            user_id=current_user.id,
            type="trait_change_pending",
            title=f"Your suggestion for {cat_label} is being reviewed",
            body="We'll let you know once a moderator has looked at it.",
            cat_id=cat_id,
        )
    )
    db.commit()

    # No push, deliberately: the user is looking at the result screen right now.
    log.info(
        "Trait change %s filed on cat %s by user %s", request.id, cat_id, current_user.id
    )
    return TraitChangeResult(request_id=request.id, status=request.status, cat_id=cat_id)


@router.get("/cats/{cat_id}/trait-change/mine", response_model=MyTraitChange | None)
def my_trait_change(
    cat_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The caller's latest request for this cat, so the profile can say so.

    Returns null rather than 404 when there is none — "you haven't suggested
    anything" is an ordinary answer, not a missing resource.
    """
    request = (
        db.query(TraitChangeRequest)
        .filter(
            TraitChangeRequest.cat_id == cat_id,
            TraitChangeRequest.user_id == current_user.id,
        )
        .order_by(TraitChangeRequest.created_at.desc())
        .first()
    )
    if not request:
        return None

    try:
        proposed = json.loads(request.proposed_json)
    except (TypeError, ValueError):
        proposed = {}

    return MyTraitChange(
        request_id=request.id,
        status=request.status,
        created_at=request.created_at,
        decided_at=request.decided_at,
        proposed=proposed if isinstance(proposed, dict) else {},
        rejection_reason=request.rejection_reason,
    )
