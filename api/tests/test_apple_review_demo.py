"""The Apple review account: a fresh new-user run on every sign-in.

App Review is meant to arrive as a brand-new user — intro carousel, profile
setup, then setting a home neighbourhood — and find that neighbourhood already
populated with the seeded cats. These tests pin the three halves of that: the
account really is pristine after a sign-in, the seeded cats belong to procedural
neighbours (bar the one it owns), and none of it leaks to real users.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.db.session import Base, get_db
from app.main import app
from app.models.cat import Cat
from app.models.claim import CatClaim
from app.models.exploration import ExploredTile
from app.models.explorer import ExplorerPost
from app.models.sighting import Sighting
from app.models.user import User
from app.services.demo_seed import (
    DEMO_EMAIL,
    DEMO_PASSWORD,
    NEIGHBOUR_EMAILS,
    OWNED_CAT_KEY,
    seed_apple_review_demo,
)


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


@pytest.fixture
def client(session_factory):
    def override_get_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    with session_factory() as db:
        seed_apple_review_demo(db)

    app.dependency_overrides[get_db] = override_get_db
    c = TestClient(app)
    try:
        yield c
    finally:
        app.dependency_overrides.clear()


def _login(client):
    resp = client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    assert resp.status_code == 200, resp.text
    return resp.json()


def _demo_auth(client):
    return {"Authorization": f"Bearer {_login(client)['access_token']}"}


# ---------------------------------------------------------------------------
# Arriving as a new user
# ---------------------------------------------------------------------------


def test_login_asks_the_app_for_onboarding(client):
    body = _login(client)
    assert body["access_token"]
    assert body["needs_onboarding"] is True


def test_account_starts_with_no_profile_or_explored_ground(client, session_factory):
    headers = _demo_auth(client)

    me = client.get("/auth/me", headers=headers).json()
    assert me["email"] == DEMO_EMAIL
    # Nothing for character creation to skip over.
    assert me["display_name"] is None
    assert me["avatar_emoji"] is None

    with session_factory() as db:
        demo = db.query(User).filter(User.email == DEMO_EMAIL).first()
        assert demo.onboarded_at is None
        assert demo.catalog_layout is None
        # No name change on record, so profile setup can't hit the 30-day lock.
        assert demo.display_name_updated_at is None
        # Full fog: the map prompts them to set a home neighbourhood.
        assert db.query(ExploredTile).filter(ExploredTile.user_id == demo.id).count() == 0

    assert client.get("/auth/me/stats", headers=headers).json()["tiles_explored"] == 0


def test_onboarding_completes_but_the_next_sign_in_resets_it(client, session_factory):
    headers = _demo_auth(client)
    assert client.post("/auth/onboarded", headers=headers).status_code == 204

    with session_factory() as db:
        demo = db.query(User).filter(User.email == DEMO_EMAIL).first()
        assert demo.onboarded_at is not None

    # A second reviewer (or a resubmission) gets the same first run, no deploy.
    assert _login(client)["needs_onboarding"] is True


def test_onboarding_marks_a_normal_account_once(client, session_factory):
    with session_factory() as db:
        from app.services.auth_service import hash_password

        db.add(
            User(
                email="real@example.com",
                hashed_password=hash_password("Password123!"),
                email_verified=True,
            )
        )
        db.commit()

    first = client.post("/auth/login", json={"email": "real@example.com", "password": "Password123!"})
    assert first.json()["needs_onboarding"] is True
    headers = {"Authorization": f"Bearer {first.json()['access_token']}"}
    assert client.post("/auth/onboarded", headers=headers).status_code == 204

    again = client.post("/auth/login", json={"email": "real@example.com", "password": "Password123!"})
    assert again.json()["needs_onboarding"] is False


# ---------------------------------------------------------------------------
# The seeded neighbourhood
# ---------------------------------------------------------------------------


def test_catalog_starts_with_cats_but_not_all_of_them(client):
    headers = _demo_auth(client)

    cats = client.get("/cats", headers=headers).json()
    assert len(cats) == 6

    # Already collected: the cat they own, plus the ones they spotted themselves
    # a while back. Not an empty shelf, and not the finished set either.
    mine = client.get("/cats/mine", headers=headers).json()
    collected = {c["name"] for c in mine}
    assert collected == {"Clementine", "Biscuit", "Wednesday", "Mochi"}

    # The rest are on the map, waiting to be photographed — that's the demo.
    assert {c["name"] for c in cats} - collected == {"Ash", "Juniper"}

    # Every Cat-a-log card carries a photo the reviewer took (own_cover_photos
    # refuses to show someone else's), so none of them render blank.
    assert all(c["last_photo_path"] for c in mine)

    stats = client.get("/auth/me/stats", headers=headers).json()
    assert stats["unique_cats_spotted"] == 4
    assert stats["my_sightings"] == 5  # both of Clementine's, plus three others


def test_the_newest_sighting_of_each_cat_stays_a_neighbours(client, session_factory):
    """The reviewer's own spots are the older ones, so the feed, the "last seen
    by" line and the Explorer posts still belong to other people."""
    headers = _demo_auth(client)
    client.get("/cats", headers=headers)

    with session_factory() as db:
        demo = db.query(User).filter(User.email == DEMO_EMAIL).first()
        owned = db.query(Cat).filter(Cat.name == "Clementine").first()

        for cat in db.query(Cat).filter(Cat.id != owned.id).all():
            newest = (
                db.query(Sighting)
                .filter(Sighting.cat_id == cat.id)
                .order_by(Sighting.spotted_at.desc())
                .first()
            )
            assert newest.user_id != demo.id, cat.name

        # Their own cat aside, every seeded cat was last seen by a neighbour.
        spotters = {
            row[0]
            for row in db.query(User.email)
            .join(ExplorerPost, ExplorerPost.user_id == User.id)
            .filter(ExplorerPost.cat_id != owned.id)
            .distinct()
            .all()
        }
        assert spotters == set(NEIGHBOUR_EMAILS)


def test_the_reviewer_already_owns_one_cat(client, session_factory):
    headers = _demo_auth(client)
    clementine = [
        c for c in client.get("/cats", headers=headers).json() if c["name"] == "Clementine"
    ][0]

    assert client.get(f"/cats/{clementine['id']}", headers=headers).status_code == 200

    with session_factory() as db:
        demo = db.query(User).filter(User.email == DEMO_EMAIL).first()
        claims = db.query(CatClaim).filter(CatClaim.user_id == demo.id).all()
        # Exactly one verified claim — the Verified Owner badge and owner card.
        assert [(c.cat_id, c.status) for c in claims] == [(clementine["id"], "verified")]
    assert OWNED_CAT_KEY == "clementine"


def test_feed_shows_the_neighbours_spots(client):
    headers = _demo_auth(client)
    feed = client.get("/sightings/feed", headers=headers).json()
    assert len(feed) >= 6
    assert all(not item["hidden"] for item in feed)

    posts = client.get("/explorer/feed", headers=headers).json()
    assert len(posts) == 6


def test_reviewer_can_open_a_neighbours_profile(client, session_factory):
    headers = _demo_auth(client)
    with session_factory() as db:
        neighbour = db.query(User).filter(User.email.in_(NEIGHBOUR_EMAILS)).first()
        neighbour_id = neighbour.id

    resp = client.get(f"/users/{neighbour_id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["display_name"]
    # Their Cat-a-log isn't empty when the reviewer looks at it.
    assert len(body["cats"]) >= 1

    # A real user tapping the same profile still sees none of the seeded cats.
    assert client.get(f"/users/{neighbour_id}").json()["cats"] == []


# ---------------------------------------------------------------------------
# Isolation from real users
# ---------------------------------------------------------------------------


def test_demo_cats_are_hidden_from_public_lists(client):
    assert client.get("/cats").json() == []
    assert client.get("/sightings/feed").json() == []
    assert client.get("/explorer/feed").json() == []

    stats = client.get("/cats/stats").json()
    assert stats["total_cats"] == 0
    assert stats["total_sightings"] == 0


def test_public_cannot_open_demo_cat_detail(client):
    headers = _demo_auth(client)
    demo_cat_id = client.get("/cats", headers=headers).json()[0]["id"]

    assert client.get(f"/cats/{demo_cat_id}").status_code == 404
    assert client.get(f"/cats/{demo_cat_id}", headers=headers).status_code == 200


def test_procedural_neighbours_stay_off_the_leaderboard(client):
    _demo_auth(client)
    boards = client.get("/cats/stats/leaderboard").json()
    named = {row["display_name"] for rows in boards.values() for row in rows}
    assert named == set()


def test_neighbour_accounts_cannot_be_signed_into(client):
    for email in NEIGHBOUR_EMAILS:
        resp = client.post("/auth/login", json={"email": email, "password": DEMO_PASSWORD})
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Landing in the reviewer's own neighbourhood
# ---------------------------------------------------------------------------


def test_cats_move_to_the_reviewers_neighbourhood(client):
    headers = _demo_auth(client)
    lat, lng = 40.7128, -74.0060

    cats = client.get(f"/cats?lat={lat}&lng={lng}", headers=headers).json()

    assert cats
    assert all(abs(c["last_lat"] - lat) < 0.01 for c in cats)
    assert all(abs(c["last_lng"] - lng) < 0.01 for c in cats)


def test_cats_stay_put_once_they_are_in_the_neighbourhood(client):
    """The pins must not trail the reviewer around after the home is set."""
    headers = _demo_auth(client)
    lat, lng = 40.7128, -74.0060
    placed = client.get(f"/cats?lat={lat}&lng={lng}", headers=headers).json()
    placed_by_id = {c["id"]: (c["last_lat"], c["last_lng"]) for c in placed}

    # A few hundred metres down the road — inside the same neighbourhood.
    walked = client.get(f"/cats?lat={lat + 0.003}&lng={lng + 0.003}", headers=headers).json()
    assert {c["id"]: (c["last_lat"], c["last_lng"]) for c in walked} == placed_by_id

    # Genuinely somewhere else: they follow again.
    moved = client.get("/cats?lat=51.5074&lng=-0.1278", headers=headers).json()
    assert all(abs(c["last_lat"] - 51.5074) < 0.01 for c in moved)


def test_onboarding_cat_picker_finds_the_seeded_cats(client):
    """The "do you own a cat?" step searches within ~3km of the reviewer, so the
    cats have to have been moved by the time it runs — and it has to be
    authenticated, or the seeded cats are filtered out as they are for the public."""
    headers = _demo_auth(client)
    lat, lng = 40.7128, -74.0060

    nearby = client.get(f"/cats/nearby?lat={lat}&lng={lng}", headers=headers).json()
    assert len(nearby) == 6

    assert client.get(f"/cats/nearby?lat={lat}&lng={lng}").json() == []


def test_reviewer_can_recognise_a_seeded_cat_when_spotting(client):
    """Photographing a cat that's already on their map has to offer that cat as a
    match — otherwise the reviewer's own photo silently forks a duplicate and the
    seeded neighbourhood can't be spotted into."""
    headers = _demo_auth(client)
    lat, lng = 40.7128, -74.0060
    biscuit = [
        c
        for c in client.get(f"/cats?lat={lat}&lng={lng}", headers=headers).json()
        if c["name"] == "Biscuit"
    ][0]

    body = {
        "latitude": biscuit["last_lat"],
        "longitude": biscuit["last_lng"],
        "primary_color": "orange",
        "secondary_color": "white",
        "pattern": "tabby",
        "fur_length": "short",
        "eye_color": "amber",
        "body_size": "large",
        "breed": "Orange Tabby",
    }
    as_reviewer = client.post("/sightings/match-check", json=body, headers=headers).json()
    assert biscuit["id"] in {c["cat_id"] for c in as_reviewer["candidates"]}

    # A real user standing in the same spot is offered nothing seeded.
    anonymous = client.post("/sightings/match-check", json=body).json()
    assert anonymous["candidates"] == []


def test_relocation_only_happens_for_the_review_account(client, session_factory):
    headers = _demo_auth(client)
    before = {c["id"]: (c["last_lat"], c["last_lng"]) for c in client.get("/cats", headers=headers).json()}

    # An anonymous caller passing a location can't drag the seeded cats around.
    client.get("/cats?lat=40.7128&lng=-74.0060")

    after = {c["id"]: (c["last_lat"], c["last_lng"]) for c in client.get("/cats", headers=headers).json()}
    assert after == before
