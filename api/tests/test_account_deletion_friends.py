"""DELETE /auth/me must take every friendship row with it.

SQLite has no FK cascades configured and Postgres would refuse the delete
outright, so the sweep in delete_me is explicit — and it relies on
`requested_by_id` always being one of the two pair columns. The third case below
is what holds that invariant honest.
"""

from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, or_
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registers every model on Base before create_all
from app.db.session import Base, get_db
from app.main import app
from app.models.friendship import Friendship
from app.models.notification import Notification
from app.models.user import User
from app.services.auth_service import create_access_token, hash_password


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
    id: int


def _make_user(session, email, display_name):
    user = User(
        email=email,
        hashed_password=hash_password("hunter2pw"),
        display_name=display_name,
        email_verified=True,
        is_active=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return U(user.id)


def _auth(user):
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}


def _rows_for(client, user):
    session = client.Session()
    try:
        return (
            session.query(Friendship)
            .filter(
                or_(
                    Friendship.user_low_id == user.id,
                    Friendship.user_high_id == user.id,
                )
            )
            .count()
        )
    finally:
        session.close()


def _pair(client):
    """Two users whose ids bracket each other, so either can be the low side."""
    session = client.Session()
    try:
        return (
            _make_user(session, "a@example.com", "Ada"),
            _make_user(session, "b@example.com", "Bo"),
        )
    finally:
        session.close()


@pytest.mark.parametrize("leaver_is_low", [True, False])
def test_deleting_an_account_removes_the_friendship_from_either_side(
    client, leaver_is_low
):
    a, b = _pair(client)
    # a.id < b.id, so a is always the low side of the stored pair.
    leaver, stayer = (a, b) if leaver_is_low else (b, a)

    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    client.post(f"/friends/requests/{a.id}/accept", headers=_auth(b))
    assert _rows_for(client, stayer) == 1

    assert client.delete("/auth/me", headers=_auth(leaver)).status_code == 204
    assert _rows_for(client, stayer) == 0

    # The survivor's list still works and no longer mentions them.
    res = client.get("/friends", headers=_auth(stayer))
    assert res.status_code == 200
    assert res.json() == []


def test_deleting_the_requester_removes_the_row_it_points_at(client):
    """Covers requested_by_id — the column the single or_ filter relies on."""
    a, b = _pair(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    session = client.Session()
    try:
        row = session.query(Friendship).first()
        assert row.requested_by_id == a.id
    finally:
        session.close()

    assert client.delete("/auth/me", headers=_auth(a)).status_code == 204

    session = client.Session()
    try:
        assert session.query(Friendship).count() == 0
    finally:
        session.close()


def test_pending_rows_in_both_directions_go(client):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        b = _make_user(session, "b@example.com", "Bo")
        c = _make_user(session, "c@example.com", "Cy")
    finally:
        session.close()

    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))  # a asked b
    client.post("/friends/requests", json={"user_id": a.id}, headers=_auth(c))  # c asked a

    assert client.delete("/auth/me", headers=_auth(a)).status_code == 204

    session = client.Session()
    try:
        assert session.query(Friendship).count() == 0
    finally:
        session.close()
    assert client.get("/friends/pending-count", headers=_auth(c)).json() == {"count": 0}


def test_a_sent_friend_notification_survives_the_senders_deletion(client):
    """Why there is no from_user_id: nothing here dangles when an account goes."""
    a, b = _pair(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    assert client.delete("/auth/me", headers=_auth(a)).status_code == 204

    session = client.Session()
    try:
        rows = session.query(Notification).filter(Notification.user_id == b.id).all()
        assert len(rows) == 1
        assert rows[0].type == "friend_request"
        assert "Ada" in rows[0].body
    finally:
        session.close()

    # And B's inbox still renders it.
    res = client.get("/notifications", headers=_auth(b))
    assert res.status_code == 200
    assert res.json()[0]["type"] == "friend_request"
