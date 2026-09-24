"""Friendship lookups shared by the friends router, the feed and public profiles.

Everything here reads `friendships` through Friendship.normalise_pair, so a pair
is found whichever order it is asked about. The batched helpers exist for the
same reason the feed batches its meow counts: a friend list is rendered as a
list, and a per-row query would be an N+1.
"""

from __future__ import annotations

from sqlalchemy import distinct, func, or_
from sqlalchemy.orm import Session

from app.models.friendship import Friendship
from app.models.sighting import Sighting
from app.models.user import User
from app.services.demo_seed import visible_sightings_query

# The vocabulary every friend-status field speaks, from the viewer's side:
#   self     — the other user is the viewer
#   none     — no row, or a declined one (a decline is not shown to either party)
#   outgoing — the viewer asked and is waiting
#   incoming — the other user asked and the viewer has yet to answer
#   friends  — accepted
FriendStatus = str


def friend_ids(db: Session, user_id: int) -> set[int]:
    """The ids of everyone this user is actually friends with.

    One query, served by ix_friendship_low_status / ix_friendship_high_status.
    Pending and declined rows are not friendships and never appear here.
    """
    rows = (
        db.query(Friendship.user_low_id, Friendship.user_high_id)
        .filter(
            Friendship.status == "accepted",
            or_(Friendship.user_low_id == user_id, Friendship.user_high_id == user_id),
        )
        .all()
    )
    return {low if high == user_id else high for low, high in rows}


def get_pair(db: Session, one: int, other: int) -> Friendship | None:
    """The row for this unordered pair, in whatever state it is in."""
    low, high = Friendship.normalise_pair(one, other)
    return (
        db.query(Friendship)
        .filter(Friendship.user_low_id == low, Friendship.user_high_id == high)
        .first()
    )


def _status_from_row(row: Friendship | None, viewer_id: int) -> FriendStatus:
    """Read a stored row from one side's point of view."""
    if row is None or row.status == "declined":
        # A declined approach reads as "none" to both parties: the sender isn't
        # told they were turned down, and the recipient gets a clean slate.
        return "none"
    if row.status == "accepted":
        return "friends"
    return "outgoing" if row.requested_by_id == viewer_id else "incoming"


def pair_status(db: Session, viewer_id: int, other_id: int) -> FriendStatus:
    """How the viewer stands with one other user. Backs the profile screen."""
    if viewer_id == other_id:
        return "self"
    return _status_from_row(get_pair(db, viewer_id, other_id), viewer_id)


def pair_statuses(
    db: Session, viewer_id: int, other_ids: list[int]
) -> dict[int, FriendStatus]:
    """pair_status for many users in one query. Backs user search."""
    others = [uid for uid in other_ids if uid != viewer_id]
    result: dict[int, FriendStatus] = {uid: "none" for uid in other_ids}
    if viewer_id in result:
        result[viewer_id] = "self"
    if not others:
        return result

    rows = (
        db.query(Friendship)
        .filter(
            or_(
                Friendship.user_low_id == viewer_id,
                Friendship.user_high_id == viewer_id,
            ),
            or_(
                Friendship.user_low_id.in_(others),
                Friendship.user_high_id.in_(others),
            ),
        )
        .all()
    )
    for row in rows:
        other = row.user_low_id if row.user_high_id == viewer_id else row.user_high_id
        if other in result:
            result[other] = _status_from_row(row, viewer_id)
    return result


def pending_incoming_count(db: Session, user_id: int) -> int:
    """How many requests are waiting on this user to answer. Drives the badge."""
    return (
        db.query(Friendship)
        .filter(
            Friendship.status == "pending",
            Friendship.requested_by_id != user_id,
            or_(Friendship.user_low_id == user_id, Friendship.user_high_id == user_id),
        )
        .count()
    )


def spotted_cat_counts(
    db: Session, user_ids: list[int], current_user: User | None
) -> dict[int, int]:
    """How many distinct cats each of these users has spotted, in one query.

    Scoped to the viewer, so seeded demo sightings don't inflate a figure for
    anyone who can't see that content in the first place. Users with no visible
    sightings are absent from the result — callers should default to 0.
    """
    if not user_ids:
        return {}
    rows = (
        visible_sightings_query(
            db.query(Sighting.user_id, func.count(distinct(Sighting.cat_id))),
            current_user,
        )
        .filter(Sighting.user_id.in_(user_ids), Sighting.cat_id.isnot(None))
        .group_by(Sighting.user_id)
        .all()
    )
    return {user_id: count for user_id, count in rows}
