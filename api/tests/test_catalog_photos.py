"""A Cat-a-log card only ever shows a photo its owner took.

`cats.last_photo_path` is global — the most recent photo of that cat by anyone —
so serving it straight from `GET /cats/mine` put another spotter's photo on your
card as soon as they photographed a cat you'd already logged. Cards now resolve
through `own_cover_photos`: the highlight the owner picked if it's still one of
theirs, else their own latest. Same on the public profile, where an unvalidated
cover could additionally point a card at an arbitrary storage key.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registers every model on Base before create_all
from app.db.session import Base, get_db
from app.main import app
from app.models.cat import Cat
from app.models.sighting import Sighting
from app.models.user import User
from app.services.auth_service import create_access_token, hash_password
from app.services.catalog import own_cover_photos, parse_covers
from app.services.storage import LocalStorage, set_storage


@pytest.fixture
def client(tmp_path):
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
    # LocalStorage returns keys unchanged, so responses carry the raw paths.
    set_storage(LocalStorage(tmp_path))
    c = TestClient(app)
    c.Session = Session  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        set_storage(None)
        engine.dispose()


def _make_user(session, email, catalog_layout=None):
    user = User(
        email=email,
        hashed_password=hash_password("hunter2pw"),
        email_verified=True,
        is_active=True,
        catalog_layout=catalog_layout,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _token(user):
    return create_access_token({"sub": str(user.id)})


def _make_cat(session, last_photo_path=None, name="Whiskers"):
    cat = Cat(name=name, last_photo_path=last_photo_path)
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def _make_sighting(session, user_id, cat_id, photo_path, days_ago=0):
    s = Sighting(
        user_id=user_id,
        cat_id=cat_id,
        photo_path=photo_path,
        latitude=51.5,
        longitude=-0.12,
        spotted_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def _auth(user):
    return {"Authorization": f"Bearer {_token(user)}"}


def test_my_catalog_never_shows_another_spotters_photo(client):
    """The reported bug: someone else photographed my cat more recently, so the
    global last_photo_path — and my card — became their photo."""
    session = client.Session()
    me = _make_user(session, "me@example.com")
    them = _make_user(session, "them@example.com")
    cat = _make_cat(session, last_photo_path="uploads/theirs.jpg")
    _make_sighting(session, me.id, cat.id, "uploads/mine.jpg", days_ago=2)
    _make_sighting(session, them.id, cat.id, "uploads/theirs.jpg", days_ago=1)
    headers = _auth(me)
    session.close()

    r = client.get("/cats/mine", headers=headers)
    assert r.status_code == 200, r.text
    assert [c["last_photo_path"] for c in r.json()] == ["uploads/mine.jpg"]


def test_my_catalog_uses_my_latest_photo_when_no_highlight_chosen(client):
    """With no explicit highlight, the card falls back to my own newest photo."""
    session = client.Session()
    me = _make_user(session, "me@example.com")
    cat = _make_cat(session)
    _make_sighting(session, me.id, cat.id, "uploads/old.jpg", days_ago=5)
    _make_sighting(session, me.id, cat.id, "uploads/new.jpg", days_ago=1)
    headers = _auth(me)
    session.close()

    r = client.get("/cats/mine", headers=headers)
    assert r.json()[0]["last_photo_path"] == "uploads/new.jpg"


def test_my_catalog_honours_my_chosen_highlight(client):
    """An explicitly picked highlight wins over my newest photo."""
    session = client.Session()
    layout = json.dumps({"order": [], "frames": {}, "covers": {}, "adjusts": {}})
    me = _make_user(session, "me@example.com", catalog_layout=layout)
    cat = _make_cat(session)
    _make_sighting(session, me.id, cat.id, "uploads/old.jpg", days_ago=5)
    _make_sighting(session, me.id, cat.id, "uploads/new.jpg", days_ago=1)
    me.catalog_layout = json.dumps({"covers": {str(cat.id): "uploads/old.jpg"}})
    session.commit()
    headers = _auth(me)
    session.close()

    r = client.get("/cats/mine", headers=headers)
    assert r.json()[0]["last_photo_path"] == "uploads/old.jpg"


def test_highlight_pointing_at_someone_elses_photo_is_ignored(client):
    """A stale or crafted cover naming a photo I didn't take falls back to mine."""
    session = client.Session()
    me = _make_user(session, "me@example.com")
    them = _make_user(session, "them@example.com")
    cat = _make_cat(session)
    _make_sighting(session, me.id, cat.id, "uploads/mine.jpg", days_ago=2)
    _make_sighting(session, them.id, cat.id, "uploads/theirs.jpg", days_ago=1)
    me.catalog_layout = json.dumps({"covers": {str(cat.id): "uploads/theirs.jpg"}})
    session.commit()
    headers = _auth(me)
    session.close()

    r = client.get("/cats/mine", headers=headers)
    assert r.json()[0]["last_photo_path"] == "uploads/mine.jpg"


def test_public_profile_shows_that_spotters_own_photo(client):
    """Someone else's Cat-a-log is theirs too — cards show photos they took."""
    session = client.Session()
    me = _make_user(session, "me@example.com")
    them = _make_user(session, "them@example.com")
    cat = _make_cat(session, last_photo_path="uploads/mine.jpg")
    _make_sighting(session, them.id, cat.id, "uploads/theirs.jpg", days_ago=2)
    _make_sighting(session, me.id, cat.id, "uploads/mine.jpg", days_ago=1)
    them_id = them.id
    session.close()

    r = client.get(f"/users/{them_id}")
    assert r.status_code == 200, r.text
    assert [c["last_photo_path"] for c in r.json()["cats"]] == ["uploads/theirs.jpg"]


def test_get_my_catalog_round_trips_the_saved_arrangement(client):
    """The client must be able to read its arrangement back, or a fresh install
    would push an empty one over it."""
    session = client.Session()
    me = _make_user(session, "me@example.com")
    headers = _auth(me)
    session.close()

    body = {
        "order": [3, 1, 2],
        "frames": {"1": "gold"},
        "covers": {"1": "uploads/pick.jpg"},
        "adjusts": {"1": {"scale": 1.5, "x": 0.2, "y": -0.1}},
    }
    assert client.put("/users/me/catalog", json=body, headers=headers).status_code == 204

    r = client.get("/users/me/catalog", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json() == body


def test_get_my_catalog_requires_auth(client):
    assert client.get("/users/me/catalog").status_code in (401, 403)


def test_own_cover_photos_ignores_cats_with_no_photo_of_mine(db):
    """A cat I have no photo of resolves to None, not to another spotter's photo."""
    me = _make_user(db, "me@example.com")
    them = _make_user(db, "them@example.com")
    cat = _make_cat(db, last_photo_path="uploads/theirs.jpg")
    _make_sighting(db, them.id, cat.id, "uploads/theirs.jpg")

    assert own_cover_photos(db, me.id, [cat.id], {}) == {cat.id: None}


def test_parse_covers_tolerates_corrupt_layouts():
    assert parse_covers(None) == {}
    assert parse_covers("not json") == {}
    assert parse_covers(json.dumps({"covers": "nope"})) == {}
    assert parse_covers(json.dumps({"order": [1]})) == {}
    assert parse_covers(json.dumps({"covers": {1: "uploads/a.jpg"}})) == {"1": "uploads/a.jpg"}
