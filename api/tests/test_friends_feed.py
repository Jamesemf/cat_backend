"""GET /sightings/feed?scope=friends, and the is_friend flag both scopes carry.

Two properties matter most here:
  * the friends scope ignores distance entirely — that is the whole point of it,
    and it is why the client can serve it without a location fix;
  * is_friend is true in the *nearby* scope too, because the client's fog-of-paw
    bypass keys off it there as well.
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
from app.models.cat import Cat
from app.models.explorer import ExplorerPost
from app.models.sighting import Sighting
from app.models.user import User
from app.services.auth_service import create_access_token, hash_password

# London, and a point well outside any sane neighbourhood radius of it.
HOME = (51.5074, -0.1278)
FAR_AWAY = (55.9533, -3.1883)  # Edinburgh, ~530 km


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


def _make_user(session, email, display_name="Spotter"):
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


def _make_sighting(session, user_id, coords, name="Whiskers", hidden=False):
    """A sighting and the Explorer post it mirrors into, as the app creates them."""
    cat = Cat(name=name)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    lat, lng = coords
    s = Sighting(
        user_id=user_id,
        cat_id=cat.id,
        photo_path="uploads/x.jpg",
        latitude=lat,
        longitude=lng,
    )
    session.add(s)
    session.commit()
    session.refresh(s)

    import datetime

    post = ExplorerPost(
        user_id=user_id,
        sighting_id=s.id,
        cat_id=cat.id,
        photo_path=s.photo_path,
        latitude=lat,
        longitude=lng,
        hidden_at=datetime.datetime(2026, 1, 1) if hidden else None,
    )
    session.add(post)
    session.commit()
    return s.id


def _befriend(client, a, b):
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))
    client.post(f"/friends/requests/{a.id}/accept", headers=_auth(b))


def _setup(client, *, hidden=False):
    """Viewer A, friend B (spotting far away), stranger C (spotting far away)."""
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        b = _make_user(session, "b@example.com", "Bo")
        c = _make_user(session, "c@example.com", "Cy")
        friend_sighting = _make_sighting(
            session, b.id, FAR_AWAY, name="Bo's cat", hidden=hidden
        )
        stranger_sighting = _make_sighting(session, c.id, FAR_AWAY, name="Cy's cat")
    finally:
        session.close()
    _befriend(client, a, b)
    return a, b, c, friend_sighting, stranger_sighting


def test_friends_scope_ignores_distance(client):
    """No lat/lng at all, and a spot 500km away still comes back."""
    a, _, _, friend_sighting, _ = _setup(client)

    res = client.get("/sightings/feed?scope=friends", headers=_auth(a))
    assert res.status_code == 200
    assert [item["id"] for item in res.json()] == [friend_sighting]


def test_friends_scope_excludes_strangers(client):
    a, _, _, friend_sighting, stranger_sighting = _setup(client)

    ids = [item["id"] for item in client.get(
        "/sightings/feed?scope=friends", headers=_auth(a)
    ).json()]
    assert friend_sighting in ids
    assert stranger_sighting not in ids


def test_a_pending_request_is_not_a_friendship(client):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        b = _make_user(session, "b@example.com", "Bo")
        _make_sighting(session, b.id, FAR_AWAY)
    finally:
        session.close()

    # Asked, but not yet answered.
    client.post("/friends/requests", json={"user_id": b.id}, headers=_auth(a))

    assert client.get("/sightings/feed?scope=friends", headers=_auth(a)).json() == []


def test_friends_scope_with_no_friends_is_empty(client):
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        b = _make_user(session, "b@example.com", "Bo")
        _make_sighting(session, b.id, HOME)
    finally:
        session.close()

    assert client.get("/sightings/feed?scope=friends", headers=_auth(a)).json() == []


def test_friends_scope_requires_a_signed_in_user(client):
    res = client.get("/sightings/feed?scope=friends")
    assert res.status_code == 401


def test_a_friends_hidden_spot_is_still_withheld(client):
    """Friendship is not a moderation bypass."""
    a, _, _, _, _ = _setup(client, hidden=True)
    assert client.get("/sightings/feed?scope=friends", headers=_auth(a)).json() == []


def test_is_friend_is_set_in_both_scopes(client):
    """The nearby case is what the client's fog bypass depends on."""
    session = client.Session()
    try:
        a = _make_user(session, "a@example.com", "Ada")
        b = _make_user(session, "b@example.com", "Bo")
        c = _make_user(session, "c@example.com", "Cy")
        friend_sighting = _make_sighting(session, b.id, HOME, name="Bo's cat")
        stranger_sighting = _make_sighting(session, c.id, HOME, name="Cy's cat")
    finally:
        session.close()
    _befriend(client, a, b)

    lat, lng = HOME
    nearby = client.get(
        f"/sightings/feed?lat={lat}&lng={lng}", headers=_auth(a)
    ).json()
    by_id = {item["id"]: item for item in nearby}
    assert by_id[friend_sighting]["is_friend"] is True
    assert by_id[stranger_sighting]["is_friend"] is False

    friends = client.get("/sightings/feed?scope=friends", headers=_auth(a)).json()
    assert friends[0]["is_friend"] is True


def test_is_friend_is_false_for_an_anonymous_viewer(client):
    session = client.Session()
    try:
        b = _make_user(session, "b@example.com", "Bo")
        _make_sighting(session, b.id, HOME)
    finally:
        session.close()

    lat, lng = HOME
    items = client.get(f"/sightings/feed?lat={lat}&lng={lng}").json()
    assert items and all(item["is_friend"] is False for item in items)


def test_omitting_scope_behaves_as_before(client):
    """Old clients send no scope and must get the nearby feed unchanged."""
    a, _, _, _, _ = _setup(client)
    session = client.Session()
    try:
        near = _make_sighting(session, a.id, HOME, name="Local cat")
    finally:
        session.close()

    lat, lng = HOME
    ids = [
        item["id"]
        for item in client.get(f"/sightings/feed?lat={lat}&lng={lng}", headers=_auth(a)).json()
    ]
    # The friend's far-away spot is not pulled in by default.
    assert ids == [near]


def test_an_unknown_scope_is_rejected(client):
    res = client.get("/sightings/feed?scope=everyone")
    assert res.status_code == 422
