"""Which friend actions notify, which stay silent, and that the inbox survives them.

Friend notifications carry no cat, sighting or post id, so the last test here is
the regression guard for the inbox's enrichment: every field it reads is already
guarded, and a friend row must pass through as nulls rather than 500.
"""

from typing import NamedTuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registers every model on Base before create_all
from app.db.session import Base, get_db
from app.main import app
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


def _two_users(client):
    session = client.Session()
    try:
        return (
            _make_user(session, "a@example.com", "Ada"),
            _make_user(session, "b@example.com", "Bo"),
        )
    finally:
        session.close()


def _notifs(client, user, notif_type=None):
    session = client.Session()
    try:
        q = session.query(Notification).filter(Notification.user_id == user.id)
        if notif_type:
            q = q.filter(Notification.type == notif_type)
        return q.all()
    finally:
        session.close()


def test_sending_notifies_only_the_recipient(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    rows = _notifs(client, b, "friend_request")
    assert len(rows) == 1
    assert rows[0].title == "New friend request"
    assert "Ada" in rows[0].body
    # No deep-link ids: these route by type.
    assert (rows[0].cat_id, rows[0].sighting_id, rows[0].post_id) == (None, None, None)

    assert _notifs(client, a) == []


def test_accepting_notifies_the_requester(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    client.post(f"/friends/requests/{a.id}/accept", headers=_auth(b))

    rows = _notifs(client, a, "friend_accepted")
    assert len(rows) == 1
    assert rows[0].title == "You're friends!"
    assert "Bo" in rows[0].body


def test_auto_accept_notifies_the_original_requester(client):
    """B asking back accepts, so A hears about it the same way."""
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    client.post("/friends/requests", json={"user_id": a.id}, headers=_auth(b))

    assert len(_notifs(client, a, "friend_accepted")) == 1
    # And no second request row lands on B.
    assert len(_notifs(client, b, "friend_request")) == 1


def test_declining_is_silent(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    client.post(f"/friends/requests/{a.id}/decline", headers=_auth(b))

    assert _notifs(client, a) == []


def test_cancelling_and_unfriending_are_silent(client):
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    client.delete(f"/friends/requests/{b.id}", headers=_auth(a))
    assert _notifs(client, a) == []

    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    client.post(f"/friends/requests/{a.id}/accept", headers=_auth(b))
    before = len(_notifs(client, b))
    client.delete(f"/friends/{b.id}", headers=_auth(a))
    assert len(_notifs(client, b)) == before


def test_the_inbox_renders_a_friend_notification(client):
    """The enrichment loop reads cat/sighting fields — a friend row has none."""
    a, b = _two_users(client)
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    res = client.get("/notifications", headers=_auth(b))
    assert res.status_code == 200
    row = res.json()[0]
    assert row["type"] == "friend_request"
    assert row["cat_name"] is None
    assert row["cat_photo_path"] is None
    assert row["latitude"] is None
    assert row["longitude"] is None

    assert client.get("/notifications/unread-count", headers=_auth(b)).json() == {
        "count": 1
    }
