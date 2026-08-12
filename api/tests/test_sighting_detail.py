"""GET /sightings/{id} — one sighting, addressable regardless of where you are.

The sighting screen reached from a cat's profile can't go through /sightings/feed:
that endpoint is scoped to a 10km radius and capped at 30 items, so tapping a spot
on a far-away cat's profile used to resolve to nothing. These tests pin the two
endpoints' contracts against each other:

  * the detail endpoint ignores location entirely, including for a sighting the
    nearby feed legitimately excludes;
  * it still honours the feed's moderation rule — a withheld photo isn't reachable
    by guessing an id, while its author and admins keep seeing it.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registers every model on Base before create_all
from app.db.session import Base, get_db
from app.main import app
from app.models.cat import Cat
from app.models.explorer import ExplorerPost, PostMeow
from app.models.sighting import Sighting
from app.models.user import User
from app.services.auth_service import create_access_token, hash_password
from app.services.moderation import AUTO_HIDE_REPORT_COUNT


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


def _make_user(session, email, is_admin=False):
    user = User(
        email=email,
        hashed_password=hash_password("hunter2pw"),
        email_verified=True,
        is_active=True,
        is_admin=is_admin,
        display_name=email.split("@")[0],
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _auth(user):
    return {"Authorization": f"Bearer {create_access_token({'sub': str(user.id)})}"}


def _make_post(session, author, lat=51.5, lng=-0.12, name="Whiskers"):
    """A sighting mirrored into an Explorer post, at a chosen location."""
    cat = Cat(name=name, rarity_score=42.0, sighting_count=1)
    session.add(cat)
    session.commit()
    session.refresh(cat)

    s = Sighting(
        user_id=author.id,
        cat_id=cat.id,
        photo_path="uploads/spot.jpg",
        latitude=lat,
        longitude=lng,
    )
    session.add(s)
    session.commit()
    session.refresh(s)

    post = ExplorerPost(
        user_id=author.id,
        sighting_id=s.id,
        photo_path="uploads/spot.jpg",
        caption="a cat",
        latitude=lat,
        longitude=lng,
    )
    session.add(post)
    session.commit()
    session.refresh(post)
    return s.id, post.id, cat.id


def _pile_on(client, session, post_id, n=AUTO_HIDE_REPORT_COUNT):
    """n distinct reporters file against a post, tripping the auto-hide."""
    for i in range(n):
        reporter = _make_user(session, f"reporter{i}@example.com")
        r = client.post(
            f"/explorer/posts/{post_id}/report",
            json={"reason": "inappropriate"},
            headers=_auth(reporter),
        )
        assert r.status_code == 201


# --- the shape it serves ----------------------------------------------------

def test_returns_the_sighting_with_cat_and_post_state(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    sighting_id, post_id, cat_id = _make_post(session, author)
    headers = _auth(author)
    session.close()

    r = client.get(f"/sightings/{sighting_id}", headers=headers)
    assert r.status_code == 200
    body = r.json()

    assert body["id"] == sighting_id
    assert body["cat_id"] == cat_id
    assert body["cat_name"] == "Whiskers"
    assert body["cat_rarity_score"] == 42.0
    assert body["spotter_name"] == "author"
    assert body["spotter_id"] is not None
    assert body["post_id"] == post_id
    assert body["meow_count"] == 0
    assert body["comment_count"] == 0
    assert body["is_mine"] is True
    assert body["hidden"] is False


def test_interaction_counts_are_populated(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    fan = _make_user(session, "fan@example.com")
    sighting_id, post_id, _ = _make_post(session, author)
    session.add(PostMeow(post_id=post_id, user_id=fan.id))
    session.commit()
    fan_headers, author_headers = _auth(fan), _auth(author)
    session.close()

    body = client.get(f"/sightings/{sighting_id}", headers=fan_headers).json()
    assert body["meow_count"] == 1
    assert body["meowed_by_me"] is True
    assert body["is_mine"] is False

    # The author didn't meow it — same counts, different personal state.
    body = client.get(f"/sightings/{sighting_id}", headers=author_headers).json()
    assert body["meow_count"] == 1
    assert body["meowed_by_me"] is False
    assert body["is_mine"] is True


def test_readable_without_auth(client):
    """Deep links open before the app has restored a session."""
    session = client.Session()
    author = _make_user(session, "author@example.com")
    sighting_id, _, _ = _make_post(session, author)
    session.close()

    r = client.get(f"/sightings/{sighting_id}")
    assert r.status_code == 200
    assert r.json()["id"] == sighting_id
    # Nothing personal leaks to an anonymous caller.
    assert r.json()["is_mine"] is False
    assert r.json()["meowed_by_me"] is False


def test_unknown_id_404s(client):
    assert client.get("/sightings/999999").status_code == 404


# --- the bug this endpoint exists for ---------------------------------------

def test_out_of_range_sighting_resolves(client):
    """The regression: a spot the nearby feed excludes is still reachable by id.

    Edinburgh is ~530km from London — far outside the feed's 10km radius. The
    cat's profile links to the sighting regardless of where the viewer stands.
    """
    session = client.Session()
    author = _make_user(session, "author@example.com")
    far_id, _, _ = _make_post(session, author, lat=55.95, lng=-3.19, name="Nessie")
    session.close()

    # The nearby feed legitimately excludes it...
    feed = client.get("/sightings/feed", params={"lat": 51.5, "lng": -0.12}).json()
    assert far_id not in [item["id"] for item in feed]

    # ...but the detail endpoint serves it anyway.
    r = client.get(f"/sightings/{far_id}")
    assert r.status_code == 200
    assert r.json()["id"] == far_id
    assert r.json()["cat_name"] == "Nessie"


def test_feed_path_is_not_captured_by_the_id_route(client):
    """/sightings/feed must keep resolving to the feed, not to id parsing."""
    r = client.get("/sightings/feed")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# --- moderation parity with the feed ----------------------------------------

def test_hidden_sighting_404s_for_strangers(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    stranger = _make_user(session, "stranger@example.com")
    sighting_id, post_id, _ = _make_post(session, author)
    _pile_on(client, session, post_id)
    stranger_headers = _auth(stranger)
    session.close()

    # Gone from the feed, and not reachable by guessing the id either.
    assert client.get("/sightings/feed").json() == []
    assert client.get(f"/sightings/{sighting_id}").status_code == 404
    assert client.get(f"/sightings/{sighting_id}", headers=stranger_headers).status_code == 404


def test_author_still_sees_their_hidden_sighting(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    sighting_id, post_id, _ = _make_post(session, author)
    _pile_on(client, session, post_id)
    headers = _auth(author)
    session.close()

    r = client.get(f"/sightings/{sighting_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["hidden"] is True
    assert r.json()["is_mine"] is True


def test_admin_sees_hidden_sightings(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    admin = _make_user(session, "admin@example.com", is_admin=True)
    sighting_id, post_id, _ = _make_post(session, author)
    _pile_on(client, session, post_id)
    headers = _auth(admin)
    session.close()

    r = client.get(f"/sightings/{sighting_id}", headers=headers)
    assert r.status_code == 200
    assert r.json()["hidden"] is True
    assert r.json()["is_mine"] is False
