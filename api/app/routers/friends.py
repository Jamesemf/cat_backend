"""Mutual friendships: asking, answering, and the lists that result.

One `friendships` row per unordered pair carries every state, so each mutation
here is a single-row insert, update or delete. See models/friendship.py for why
the pair is order-normalised and what `requested_by_id` is for.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.friendship import Friendship
from app.models.notification import Notification
from app.models.user import User
from app.schemas.friend import (
    FriendOut,
    FriendRequestCreate,
    FriendRequestOut,
    FriendStatusOut,
    PendingCount,
)
from app.services.auth_service import get_current_user
from app.services.demo_seed import DEMO_ACCOUNT_EMAILS
from app.services.friends import (
    get_pair,
    pending_incoming_count,
    spotted_cat_counts,
)
from app.services.push import push_to_user
from app.services.rate_limit import enforce_daily_limit

log = logging.getLogger(__name__)

router = APIRouter(prefix="/friends", tags=["friends"])

# Sending is cheap for the sender and costs someone else's attention, so the cap
# is per account rather than per IP — same reasoning as trait suggestions.
MAX_FRIEND_REQUESTS_PER_DAY = 30


def _addressable(db: Session, user_id: int) -> User:
    """The user a request may be sent to, or 404.

    Every rejection here is a 404 rather than a 403: whether an account is
    banned, unverified or seeded is nobody else's business, and a distinct status
    code would turn this endpoint into a way to find out.
    """
    user = db.query(User).filter(User.id == user_id).first()
    if (
        not user
        or not user.is_active
        or user.banned_at is not None
        or not user.email_verified
        or not user.display_name
        or user.email.lower() in DEMO_ACCOUNT_EMAILS
    ):
        raise HTTPException(status_code=404, detail="User not found")
    return user


def _notify(
    db: Session,
    background_tasks: BackgroundTasks,
    *,
    user_id: int,
    notif_type: str,
    title: str,
    body: str,
    data: dict,
) -> None:
    """Inbox row committed now, push handed off — the house pattern."""
    db.add(Notification(user_id=user_id, type=notif_type, title=title, body=body))
    db.commit()
    background_tasks.add_task(push_to_user, user_id, title, body, data)


def _other_side(row: Friendship, user_id: int) -> int:
    return row.user_low_id if row.user_high_id == user_id else row.user_high_id


@router.post("/requests", response_model=FriendStatusOut, status_code=201)
def send_request(
    body: FriendRequestCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Ask another spotter to be friends.

    If they have already asked you, this accepts instead of queueing a second
    request — both parties have said yes, so there is nothing left to decide.
    """
    if body.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You can't add yourself as a friend.")

    target = _addressable(db, body.user_id)
    who = current_user.display_name or "Someone"
    existing = get_pair(db, current_user.id, target.id)

    if existing and existing.status == "accepted":
        raise HTTPException(status_code=409, detail="You're already friends.")

    if existing and existing.status == "pending":
        if existing.requested_by_id == current_user.id:
            raise HTTPException(
                status_code=409,
                detail="You've already asked to be friends. They haven't answered yet.",
            )
        # They asked first and now we have too. Accept it rather than filing a
        # mirror request nobody would ever need to answer.
        existing.status = "accepted"
        existing.responded_at = datetime.now(timezone.utc)
        db.commit()
        _notify(
            db,
            background_tasks,
            user_id=target.id,
            notif_type="friend_accepted",
            title="You're friends!",
            body=f"{who} accepted your friend request.",
            data={"friends_tab": "list"},
        )
        log.info("Friendship %s auto-accepted by user %s", existing.id, current_user.id)
        return FriendStatusOut(user_id=target.id, status="friends")

    # Last in the ladder: the spend commits immediately and on purpose, so a
    # request rejected above shouldn't have burned any of the budget.
    enforce_daily_limit(
        db,
        f"friend_requests:user:{current_user.id}",
        MAX_FRIEND_REQUESTS_PER_DAY,
        f"Daily limit of {MAX_FRIEND_REQUESTS_PER_DAY} friend requests reached. Come back tomorrow!",
    )

    if existing:
        # A declined pair, re-approached. Reopening the row keeps the decline
        # visible in history rather than pretending this is a first contact.
        existing.status = "pending"
        existing.requested_by_id = current_user.id
        existing.responded_at = None
    else:
        low, high = Friendship.normalise_pair(current_user.id, target.id)
        db.add(
            Friendship(
                user_low_id=low,
                user_high_id=high,
                requested_by_id=current_user.id,
                status="pending",
            )
        )
    db.commit()

    _notify(
        db,
        background_tasks,
        user_id=target.id,
        notif_type="friend_request",
        title="New friend request",
        body=f"{who} wants to be friends.",
        data={"friends_tab": "requests"},
    )
    return FriendStatusOut(user_id=target.id, status="outgoing")


def _pending_for_me(db: Session, viewer_id: int, other_id: int) -> Friendship:
    """A pending request from `other_id` that the viewer may answer, or 404."""
    row = get_pair(db, viewer_id, other_id)
    if row is None or row.status != "pending" or row.requested_by_id == viewer_id:
        raise HTTPException(status_code=404, detail="No pending request from that spotter.")
    return row


@router.post("/requests/{user_id}/accept", response_model=FriendStatusOut)
def accept_request(
    user_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Accept a request someone sent you."""
    row = _pending_for_me(db, current_user.id, user_id)
    row.status = "accepted"
    row.responded_at = datetime.now(timezone.utc)
    db.commit()

    who = current_user.display_name or "Someone"
    _notify(
        db,
        background_tasks,
        user_id=user_id,
        notif_type="friend_accepted",
        title="You're friends!",
        body=f"{who} accepted your friend request.",
        data={"friends_tab": "list"},
    )
    return FriendStatusOut(user_id=user_id, status="friends")


@router.post("/requests/{user_id}/decline", response_model=FriendStatusOut)
def decline_request(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Turn down a request.

    Silent by design — the sender isn't told, and the row reads as "none" to both
    sides from here on. It is kept only so a fresh approach is a visible state
    change rather than an insert that forgets this ever happened.
    """
    row = _pending_for_me(db, current_user.id, user_id)
    row.status = "declined"
    row.responded_at = datetime.now(timezone.utc)
    db.commit()
    return FriendStatusOut(user_id=user_id, status="none")


@router.delete("/requests/{user_id}", status_code=204)
def cancel_request(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Withdraw a request you sent. Deleted outright — it leaves no trace."""
    row = get_pair(db, current_user.id, user_id)
    if row is None or row.status != "pending" or row.requested_by_id != current_user.id:
        raise HTTPException(
            status_code=404, detail="You haven't asked that spotter to be friends."
        )
    db.delete(row)
    db.commit()


@router.get("/pending-count", response_model=PendingCount)
def pending_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Requests waiting on the caller to answer. Drives the profile badge."""
    return PendingCount(count=pending_incoming_count(db, current_user.id))


def _live_users(db: Session, user_ids: list[int]) -> dict[int, User]:
    """The still-usable accounts among these ids.

    A friend who was since banned or deactivated drops out of every list rather
    than rendering as a row you can tap into nothing.
    """
    if not user_ids:
        return {}
    rows = (
        db.query(User)
        .filter(
            User.id.in_(user_ids),
            User.is_active.is_(True),
            User.banned_at.is_(None),
        )
        .all()
    )
    return {u.id: u for u in rows}


@router.get("/requests/incoming", response_model=list[FriendRequestOut])
def list_incoming(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Requests other people are waiting on you to answer, newest first."""
    return _request_list(db, current_user, limit, offset, outgoing=False)


@router.get("/requests/outgoing", response_model=list[FriendRequestOut])
def list_outgoing(
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Requests you sent that haven't been answered, newest first."""
    return _request_list(db, current_user, limit, offset, outgoing=True)


def _request_list(
    db: Session, current_user: User, limit: int, offset: int, *, outgoing: bool
) -> list[FriendRequestOut]:
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    uid = current_user.id
    direction = (
        Friendship.requested_by_id == uid if outgoing else Friendship.requested_by_id != uid
    )
    rows = (
        db.query(Friendship)
        .filter(
            Friendship.status == "pending",
            direction,
            or_(Friendship.user_low_id == uid, Friendship.user_high_id == uid),
        )
        .order_by(Friendship.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    others = [_other_side(row, uid) for row in rows]
    users = _live_users(db, others)
    counts = spotted_cat_counts(db, list(users), current_user)
    return [
        FriendRequestOut(
            id=user.id,
            display_name=user.display_name,
            avatar_emoji=user.avatar_emoji,
            cats_spotted=counts.get(user.id, 0),
            requested_at=row.created_at,
        )
        for row, other_id in zip(rows, others)
        if (user := users.get(other_id)) is not None
    ]


@router.get("", response_model=list[FriendOut])
def list_friends(
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """The caller's friends, most recently accepted first."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    uid = current_user.id
    rows = (
        db.query(Friendship)
        .filter(
            Friendship.status == "accepted",
            or_(Friendship.user_low_id == uid, Friendship.user_high_id == uid),
        )
        .order_by(Friendship.responded_at.desc(), Friendship.id.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    others = [_other_side(row, uid) for row in rows]
    users = _live_users(db, others)
    counts = spotted_cat_counts(db, list(users), current_user)
    return [
        FriendOut(
            id=user.id,
            display_name=user.display_name,
            avatar_emoji=user.avatar_emoji,
            cats_spotted=counts.get(user.id, 0),
            # Pre-accept rows can't reach here, but responded_at is nullable in
            # the model, so fall back rather than hand Pydantic a None.
            friends_since=row.responded_at or row.created_at,
        )
        for row, other_id in zip(rows, others)
        if (user := users.get(other_id)) is not None
    ]


@router.delete("/{user_id}", status_code=204)
def unfriend(
    user_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Remove a friend. Either side may, and the row goes entirely.

    Declared after the literal /friends/... paths above so none of them is
    captured by this int param.
    """
    row = get_pair(db, current_user.id, user_id)
    if row is None or row.status != "accepted":
        raise HTTPException(status_code=404, detail="You aren't friends with that spotter.")
    db.delete(row)
    db.commit()
