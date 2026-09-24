from __future__ import annotations

from pydantic import BaseModel

from app.schemas.media import UtcDatetime

# `avatar_emoji` is a literal emoji character, not a storage key, so it stays a
# plain str — same as PublicProfileOut. No field in this feature is a media path,
# which is why media.MediaUrl* doesn't appear here.


class FriendRequestCreate(BaseModel):
    user_id: int


class FriendStatusOut(BaseModel):
    """Where the caller now stands with one other user, after a mutation.

    `status` speaks the vocabulary in services.friends: none | outgoing |
    incoming | friends | self. The client mirrors it straight into the
    Add Friend button's state.
    """

    user_id: int
    status: str


class FriendOut(BaseModel):
    # The friend's own user id, so a row can link to their public profile.
    id: int
    display_name: str | None
    avatar_emoji: str | None
    cats_spotted: int = 0
    friends_since: UtcDatetime


class FriendRequestOut(BaseModel):
    # The *other* user's id, not the friendship row's — every action the client
    # can take from one of these rows (accept, decline, cancel) is addressed by
    # who the request is with.
    id: int
    display_name: str | None
    avatar_emoji: str | None
    cats_spotted: int = 0
    requested_at: UtcDatetime


class PendingCount(BaseModel):
    count: int


class UserSearchOut(BaseModel):
    id: int
    display_name: str | None
    avatar_emoji: str | None
    cats_spotted: int = 0
    # The searcher's standing with this user, so a result row can render the
    # right button without a follow-up request per row.
    friend_status: str = "none"
