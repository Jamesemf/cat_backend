"""DELETE /notifications — clearing the in-app inbox.

Covers the two shapes the app sends (a single swiped-away row, and "clear
all"), plus the guarantee that one user can never delete another's rows.
"""

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


def _make_user(session, email):
    user = User(
        email=email,
        hashed_password=hash_password("hunter2pw"),
        email_verified=True,
        is_active=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _make_notification(session, user_id, title="Whiskers was spotted"):
    n = Notification(user_id=user_id, type="sighting", title=title, body="Nearby.")
    session.add(n)
    session.commit()
    session.refresh(n)
    return n


def _auth(user):
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}


def test_delete_by_ids_removes_only_those_rows(client):
    session = client.Session()
    user = _make_user(session, "clearer@example.com")
    keep = _make_notification(session, user.id, "Keep me")
    drop = _make_notification(session, user.id, "Drop me")

    res = client.request(
        "DELETE", "/notifications", json={"ids": [drop.id]}, headers=_auth(user)
    )

    assert res.status_code == 200
    assert res.json() == {"deleted": 1}
    remaining = session.query(Notification).all()
    assert [n.id for n in remaining] == [keep.id]
    session.close()


def test_delete_all_empties_the_inbox(client):
    session = client.Session()
    user = _make_user(session, "emptier@example.com")
    _make_notification(session, user.id)
    read_one = _make_notification(session, user.id)
    client.post(
        "/notifications/mark-read", json={"ids": [read_one.id]}, headers=_auth(user)
    )

    res = client.request("DELETE", "/notifications", json={"all": True}, headers=_auth(user))

    assert res.status_code == 200
    assert res.json() == {"deleted": 2}
    assert session.query(Notification).count() == 0
    assert client.get("/notifications", headers=_auth(user)).json() == []
    session.close()


def test_delete_cannot_touch_another_users_notifications(client):
    session = client.Session()
    mine = _make_user(session, "mine@example.com")
    theirs = _make_user(session, "theirs@example.com")
    my_id = _make_notification(session, mine.id).id
    their_row = _make_notification(session, theirs.id)

    by_id = client.request(
        "DELETE", "/notifications", json={"ids": [their_row.id]}, headers=_auth(mine)
    )
    clear_all = client.request(
        "DELETE", "/notifications", json={"all": True}, headers=_auth(mine)
    )

    assert by_id.json() == {"deleted": 0}
    assert clear_all.json() == {"deleted": 1}
    assert [n.id for n in session.query(Notification).all()] == [their_row.id]
    assert my_id not in {n.id for n in session.query(Notification).all()}
    session.close()


def test_delete_requires_authentication(client):
    session = client.Session()
    user = _make_user(session, "anon@example.com")
    row_id = _make_notification(session, user.id).id

    res = client.request("DELETE", "/notifications", json={"all": True})

    # HTTPBearer answers a missing credential with 403, as elsewhere in the API.
    assert res.status_code == 403
    assert session.query(Notification).filter(Notification.id == row_id).count() == 1
    session.close()


def test_delete_with_empty_ids_is_a_no_op(client):
    session = client.Session()
    user = _make_user(session, "noop@example.com")
    _make_notification(session, user.id)

    res = client.request("DELETE", "/notifications", json={"ids": []}, headers=_auth(user))

    assert res.json() == {"deleted": 0}
    assert session.query(Notification).count() == 1
    session.close()
