"""The friendship state machine: asking, answering, withdrawing, removing.

One `friendships` row carries every state of a pair, so most of what is worth
testing here is that the row ends up in the right state — and that the pair is
stored order-normalised, since the unique constraint is what makes a duplicate
request impossible rather than merely unlikely.
"""

import datetime
from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registers every model on Base before create_all
from app.db.session import Base, get_db
from app.main import app
from app.models.friendship import Friendship
from app.models.user import User
from app.routers.friends import MAX_FRIEND_REQUESTS_PER_DAY
from app.services.auth_service import create_access_token, hash_password
from app.services.demo_seed import DEMO_EMAIL


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    def override_get_db():
        db = Session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    c = TestClient(app)
    c.Session = Session  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


class U(NamedTuple):
    """A created user, detached from the session that made it.

    The fixture's sessions are short-lived, so tests hold plain values rather
    than ORM instances that would raise DetachedInstanceError on attribute access.
    """

    id: int
    email: str
    display_name: str | None


def _make_user(session, email, display_name="Spotter", **overrides):
    fields = {
        "email": email,
        "hashed_password": hash_password("hunter2pw"),
        "display_name": display_name,
        "email_verified": True,
        "is_active": True,
    }
    fields.update(overrides)
    user = User(**fields)
    session.add(user)
    session.commit()
    session.refresh(user)
    return U(user.id, user.email, user.display_name)


def _auth(user):
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}


def _pair(client, a, b):
    session = client.Session()
    try:
        low, high = Friendship.normalise_pair(a.id, b.id)
        return (
            session.query(Friendship)
            .filter(Friendship.user_low_id == low, Friendship.user_high_id == high)
            .first()
        )
    finally:
        session.close()


def _rows(client):
    session = client.Session()
    try:
        return session.query(Friendship).count()
    finally:
        session.close()


def _two_users(client):
    session = client.Session()
    try:
        return (
            _make_user(session, "a@example.com", "Ada"),
            _make_user(session, "b@example.com", "Bo"),
        )
    finally:
        session.close()


# --- sending -----------------------------------------------------------------


@pytest.mark.parametrize("swap", [False, True])
def test_send_stores_one_normalised_row(client, swap):
    """Whichever direction the request goes, the pair is stored low-then-high."""
    a, b = _two_users(client)
    sender, target = (b, a) if swap else (a, b)

    res = client.post(
        "/friends/requests", json={"user_id": target.id}, headers=_auth(sender)
    )
    assert res.status_code == 201
    assert res.json() == {"user_id": target.id, "status": "outgoing"}

    row = _pair(client, a, b)
    assert row is not None
    assert row.status == "pending"
    assert row.requested_by_id == sender.id
    assert row.user_low_id < row.user_high_id
    assert {row.user_low_id, row.user_high_id} == {a.id, b.id}
    assert row.responded_at is None


def test_duplicate_send_conflicts_and_adds_no_row(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    res = client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    assert res.status_code == 409
    assert _rows(client) == 1


def test_reverse_send_auto_accepts(client):
    """Both sides asked, so there is nothing left to decide."""
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    res = client.post("/friends/requests", json={"user_id": a.id}, headers=_auth(b))
    assert res.status_code == 201
    assert res.json() == {"user_id": a.id, "status": "friends"}

    assert _rows(client) == 1
    row = _pair(client, a, b)
    assert row.status == "accepted"
    # The original asker is remembered — normalisation must not overwrite it.
    assert row.requested_by_id == a.id
    assert row.responded_at is not None


def test_send_to_already_friends_conflicts(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    client.post(f"/friends/requests/{a.id}/accept", headers=_auth(b))

    res = client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    assert res.status_code == 409
    assert "already friends" in res.json()["detail"].lower()


def test_cannot_friend_yourself(client):
    a, _ = _two_users(client)
    res = client.post("/friends/requests", json={"user_id": a.id}, headers=_auth(a))
    assert res.status_code == 400
    assert _rows(client) == 0


@pytest.mark.parametrize(
    "overrides,email",
    [
        ({"is_active": False}, "inactive@example.com"),
        ({"banned_at": datetime.datetime(2026, 1, 1)}, "banned@example.com"),
        ({"email_verified": False}, "unverified@example.com"),
        ({"display_name": None}, "nameless@example.com"),
        ({}, DEMO_EMAIL),
    ],
)
def test_unaddressable_targets_404(client, overrides, email):
    """404 rather than 403 — the status code must not reveal *why*."""
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        target = _make_user(session, email, **{"display_name": "Target", **overrides})
    finally:
        session.close()

    res = client.post(
        "/friends/requests", json={"user_id": target.id}, headers=_auth(a)
    )
    assert res.status_code == 404
    assert res.json()["detail"] == "User not found"
    assert _rows(client) == 0


def test_unknown_target_404s(client):
    a, _ = _two_users(client)
    res = client.post("/friends/requests", json={"user_id": 99999}, headers=_auth(a))
    assert res.status_code == 404


# --- answering ---------------------------------------------------------------


def test_requester_cannot_accept_their_own_request(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    res = client.post(f"/friends/requests/{b.id}/accept", headers=_auth(a))
    assert res.status_code == 404
    assert _pair(client, a, b).status == "pending"


def test_accept_puts_each_in_the_others_list(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    res = client.post(f"/friends/requests/{a.id}/accept", headers=_auth(b))
    assert res.status_code == 200
    assert res.json() == {"user_id": a.id, "status": "friends"}

    for viewer, friend in ((a, b), (b, a)):
        listed = client.get("/friends", headers=_auth(viewer)).json()
        assert [f["id"] for f in listed] == [friend.id]
        assert listed[0]["friends_since"] is not None


def test_decline_empties_both_lists_and_keeps_the_row(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    res = client.post(f"/friends/requests/{a.id}/decline", headers=_auth(b))
    assert res.status_code == 200
    assert res.json() == {"user_id": a.id, "status": "none"}

    assert _pair(client, a, b).status == "declined"
    assert client.get("/friends", headers=_auth(a)).json() == []
    assert client.get("/friends", headers=_auth(b)).json() == []
    # The sender isn't told, so it reads as "no relationship" from both sides.
    assert client.get("/friends/requests/outgoing", headers=_auth(a)).json() == []
    assert client.get("/friends/requests/incoming", headers=_auth(b)).json() == []


def test_declined_pair_can_be_re_requested_the_other_way(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    client.post(f"/friends/requests/{a.id}/decline", headers=_auth(b))

    res = client.post("/friends/requests", json={"user_id": a.id}, headers=_auth(b))
    assert res.status_code == 201
    assert _rows(client) == 1
    row = _pair(client, a, b)
    assert row.status == "pending"
    assert row.requested_by_id == b.id
    assert row.responded_at is None


# --- withdrawing and removing ------------------------------------------------


def test_cancel_removes_the_row_entirely(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    res = client.delete(f"/friends/requests/{b.id}", headers=_auth(a))
    assert res.status_code == 204
    assert _rows(client) == 0
    assert client.get("/friends/requests/incoming", headers=_auth(b)).json() == []


def test_recipient_cannot_cancel_an_incoming_request(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    res = client.delete(f"/friends/requests/{a.id}", headers=_auth(b))
    assert res.status_code == 404
    assert _rows(client) == 1


@pytest.mark.parametrize("remover_is_requester", [True, False])
def test_either_side_can_unfriend(client, remover_is_requester):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    client.post(f"/friends/requests/{a.id}/accept", headers=_auth(b))

    remover, other = (a, b) if remover_is_requester else (b, a)
    res = client.delete(f"/friends/{other.id}", headers=_auth(remover))
    assert res.status_code == 204
    assert _rows(client) == 0


def test_unfriend_a_non_friend_404s(client):
    a, b = _two_users(client)
    res = client.delete(f"/friends/{b.id}", headers=_auth(a))
    assert res.status_code == 404


def test_pending_route_is_not_captured_by_the_int_param(client):
    """/friends/pending-count must not be parsed as /friends/{user_id}."""
    a, _ = _two_users(client)
    res = client.get("/friends/pending-count", headers=_auth(a))
    assert res.status_code == 200


# --- counts and lists --------------------------------------------------------


def test_pending_count_counts_incoming_only(client):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        b = _make_user(session, "b@example.com", "Bo")
        c = _make_user(session, "c@example.com", "Cy")
        d = _make_user(session, "d@example.com", "Di")
    finally:
        session.close()

    # Two people ask A; A asks C; A and D become friends.
    client.post("/friends/requests", json={"user_id": a.id}, headers=_auth(b))
    client.post("/friends/requests", json={"user_id": a.id}, headers=_auth(d))
    client.post("/friends/requests", json={"user_id": c.id}, headers=_auth(a))
    client.post(f"/friends/requests/{d.id}/accept", headers=_auth(a))

    assert client.get("/friends/pending-count", headers=_auth(a)).json() == {"count": 1}


def test_request_lists_report_the_other_users_id(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    outgoing = client.get("/friends/requests/outgoing", headers=_auth(a)).json()
    assert [r["id"] for r in outgoing] == [b.id]
    assert outgoing[0]["display_name"] == "Bo"

    incoming = client.get("/friends/requests/incoming", headers=_auth(b)).json()
    assert [r["id"] for r in incoming] == [a.id]
    assert incoming[0]["requested_at"] is not None

    # Neither list shows the caller's own side of the request.
    assert client.get("/friends/requests/incoming", headers=_auth(a)).json() == []
    assert client.get("/friends/requests/outgoing", headers=_auth(b)).json() == []


def test_banned_friend_drops_out_of_the_list(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    client.post(f"/friends/requests/{a.id}/accept", headers=_auth(b))

    session = client.Session()
    try:
        session.query(User).filter(User.id == b.id).update(
            {User.banned_at: datetime.datetime(2026, 1, 1)}
        )
        session.commit()
    finally:
        session.close()

    assert client.get("/friends", headers=_auth(a)).json() == []


# --- rate limiting -----------------------------------------------------------


def test_daily_send_limit(client):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        targets = [
            _make_user(session, f"t{i}@example.com", f"T{i}")
            for i in range(MAX_FRIEND_REQUESTS_PER_DAY + 1)
        ]
    finally:
        session.close()

    for target in targets[:MAX_FRIEND_REQUESTS_PER_DAY]:
        res = client.post(
            "/friends/requests", json={"user_id": target.id}, headers=_auth(a)
        )
        assert res.status_code == 201

    res = client.post(
        "/friends/requests", json={"user_id": targets[-1].id}, headers=_auth(a)
    )
    assert res.status_code == 429


def test_a_rejected_send_does_not_burn_budget(client):
    """The limiter is called last in the ladder, so a 409 costs nothing."""
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        b = _make_user(session, "b@example.com", "Bo")
        spares = [
            _make_user(session, f"s{i}@example.com", f"S{i}")
            for i in range(MAX_FRIEND_REQUESTS_PER_DAY - 1)
        ]
    finally:
        session.close()

    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    # Burn a dozen rejected duplicates.
    for _ in range(12):
        assert (
            client.post(
                "/friends/requests", json={"user_id": b.id}, headers=_auth(a)
            ).status_code
            == 409
        )

    # The remaining budget is untouched: one send already succeeded, so every
    # spare should still go through.
    for spare in spares:
        res = client.post(
            "/friends/requests", json={"user_id": spare.id}, headers=_auth(a)
        )
        assert res.status_code == 201, res.json()


# --- auth --------------------------------------------------------------------


@pytest.mark.parametrize(
    "method,path",
    [
        ("post", "/friends/requests"),
        ("post", "/friends/requests/1/accept"),
        ("post", "/friends/requests/1/decline"),
        ("delete", "/friends/requests/1"),
        ("delete", "/friends/1"),
        ("get", "/friends"),
        ("get", "/friends/requests/incoming"),
        ("get", "/friends/requests/outgoing"),
        ("get", "/friends/pending-count"),
    ],
)
def test_every_endpoint_requires_auth(client, method, path):
    kwargs = {"json": {"user_id": 1}} if path == "/friends/requests" else {}
    res = getattr(client, method)(path, **kwargs)
    assert res.status_code in (401, 403)
