"""What GET /cats/{id} promises the profile screen.

Two things the screen leans on:

  * **Order.** cat.sightings used to come back in insertion order, because the
    backref had no order_by. The profile's mini-map reads it as newest-first, so
    it highlighted the *oldest* spot as the most recent and drew its direction
    gradient backwards. The relationship now states the order.
  * **Spotter identity.** SightingOut carried only the denormalised spotter_name,
    so the profile could show neither a real avatar nor a link to the spotter.
    spotter_id and spotter_emoji come off the related user — eagerly, or a
    well-spotted cat would fire a query per sighting.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registers every model on Base before create_all
from app.db.session import Base, get_db
from app.main import app
from app.models.cat import Cat
from app.models.sighting import Sighting
from app.models.user import User
from app.services.auth_service import hash_password


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


@pytest.fixture
def count_queries():
    """Count SQL statements issued inside the block."""

    class Counter:
        n = 0

    counter = Counter()

    def before(conn, cursor, statement, params, context, executemany):
        counter.n += 1

    event.listen(Engine, "before_cursor_execute", before)
    try:
        yield counter
    finally:
        event.remove(Engine, "before_cursor_execute", before)


def _make_user(session, email, emoji=None):
    user = User(
        email=email,
        hashed_password=hash_password("hunter2pw"),
        email_verified=True,
        is_active=True,
        display_name=email.split("@")[0],
        avatar_emoji=emoji,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _make_cat(session, name="Whiskers"):
    cat = Cat(name=name, sighting_count=0)
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def _add_sighting(session, cat, user, when, lat=51.5, lng=-0.12, vibes=None):
    s = Sighting(
        cat_id=cat.id,
        user_id=user.id if user else None,
        photo_path="uploads/spot.jpg",
        latitude=lat,
        longitude=lng,
        spotted_at=when,
        spotter_name=user.display_name if user else "A passer-by",
        vibes=vibes,
    )
    session.add(s)
    cat.sighting_count += 1
    session.commit()
    session.refresh(s)
    return s


# --- ordering ---------------------------------------------------------------

def test_sightings_come_back_newest_first(client):
    """Inserted oldest-last, so insertion order is not chronological order."""
    session = client.Session()
    cat = _make_cat(session)
    spotter = _make_user(session, "jamie@example.com")
    now = datetime.now(timezone.utc)

    # Deliberately out of order: middle, newest, oldest.
    _add_sighting(session, cat, spotter, now - timedelta(days=7))
    newest_id = _add_sighting(session, cat, spotter, now - timedelta(hours=1)).id
    oldest_id = _add_sighting(session, cat, spotter, now - timedelta(days=30)).id
    cat_id = cat.id
    session.close()

    body = client.get(f"/cats/{cat_id}").json()
    ids = [s["id"] for s in body["sightings"]]

    assert ids[0] == newest_id, "the map highlights sightings[0] as the most recent"
    assert ids[-1] == oldest_id
    times = [s["spotted_at"] for s in body["sightings"]]
    assert times == sorted(times, reverse=True)


# --- spotter identity -------------------------------------------------------

def test_sightings_carry_spotter_id_and_emoji(client):
    session = client.Session()
    cat = _make_cat(session)
    spotter = _make_user(session, "jamie@example.com", emoji="🐈")
    _add_sighting(session, cat, spotter, datetime.now(timezone.utc))
    cat_id, spotter_id = cat.id, spotter.id
    session.close()

    s = client.get(f"/cats/{cat_id}").json()["sightings"][0]
    assert s["spotter_id"] == spotter_id
    assert s["spotter_emoji"] == "🐈"
    assert s["spotter_name"] == "jamie"


def test_anonymous_sighting_has_no_spotter_identity(client):
    """Sightings logged without a user must not break the profile.

    Note this endpoint resolves spotter_name from the related user, not from the
    denormalised column — so an anonymous sighting reports no spotter at all.
    """
    session = client.Session()
    cat = _make_cat(session)
    _add_sighting(session, cat, None, datetime.now(timezone.utc))
    cat_id = cat.id
    session.close()

    s = client.get(f"/cats/{cat_id}").json()["sightings"][0]
    assert s["spotter_id"] is None
    assert s["spotter_emoji"] is None
    assert s["spotter_name"] is None


def test_spotter_emoji_absent_is_null_not_missing(client):
    session = client.Session()
    cat = _make_cat(session)
    spotter = _make_user(session, "noemoji@example.com", emoji=None)
    _add_sighting(session, cat, spotter, datetime.now(timezone.utc))
    cat_id = cat.id
    session.close()

    s = client.get(f"/cats/{cat_id}").json()["sightings"][0]
    assert s["spotter_emoji"] is None
    assert s["spotter_id"] is not None


# --- vibe_counts ------------------------------------------------------------
#
# The profile sizes each vibe by its count, so these rules are load-bearing:
# cats.vibes is overwritten by every new sighting, and can't be the source.

def _vibes(client, cat_id) -> list[tuple[str, int]]:
    body = client.get(f"/cats/{cat_id}").json()
    return [(v["label"], v["count"]) for v in body["vibe_counts"]]


def test_vibes_are_counted_across_sightings(client):
    session = client.Session()
    cat = _make_cat(session)
    spotter = _make_user(session, "jamie@example.com")
    now = datetime.now(timezone.utc)
    _add_sighting(session, cat, spotter, now - timedelta(days=3), vibes="playful, shy")
    _add_sighting(session, cat, spotter, now - timedelta(days=2), vibes="playful, bold")
    _add_sighting(session, cat, spotter, now - timedelta(days=1), vibes="playful")
    cat.vibes = "playful"  # what the latest spot overwrote it with
    session.commit()
    cat_id = cat.id
    session.close()

    assert _vibes(client, cat_id) == [("playful", 3), ("bold", 1), ("shy", 1)]


def test_vibe_matching_is_case_insensitive(client):
    """One vibe, counted twice — and the most recent spelling is the one shown,
    since sightings are tallied newest-first."""
    session = client.Session()
    cat = _make_cat(session)
    spotter = _make_user(session, "jamie@example.com")
    now = datetime.now(timezone.utc)
    _add_sighting(session, cat, spotter, now - timedelta(days=2), vibes="playful")
    _add_sighting(session, cat, spotter, now - timedelta(days=1), vibes="Playful")
    cat_id = cat.id
    session.close()

    assert _vibes(client, cat_id) == [("Playful", 2)]


def test_repeated_vibe_in_one_sighting_counts_once(client):
    """One spotter must not be able to inflate a vibe on their own."""
    session = client.Session()
    cat = _make_cat(session)
    spotter = _make_user(session, "jamie@example.com")
    _add_sighting(
        session, cat, spotter, datetime.now(timezone.utc), vibes="shy, shy, Shy"
    )
    cat_id = cat.id
    session.close()

    assert _vibes(client, cat_id) == [("shy", 1)]


def test_blank_and_whitespace_vibes_are_dropped(client):
    session = client.Session()
    cat = _make_cat(session)
    spotter = _make_user(session, "jamie@example.com")
    _add_sighting(
        session, cat, spotter, datetime.now(timezone.utc), vibes="  bold ,, ,  shy  "
    )
    cat_id = cat.id
    session.close()

    assert _vibes(client, cat_id) == [("bold", 1), ("shy", 1)]


def test_ties_break_alphabetically(client):
    """Stable order between requests, rather than dict insertion order."""
    session = client.Session()
    cat = _make_cat(session)
    spotter = _make_user(session, "jamie@example.com")
    _add_sighting(
        session, cat, spotter, datetime.now(timezone.utc), vibes="zoomy, bold, shy"
    )
    cat_id = cat.id
    session.close()

    assert _vibes(client, cat_id) == [("bold", 1), ("shy", 1), ("zoomy", 1)]


def test_falls_back_to_cat_vibes_when_no_sighting_has_any(client):
    """Cats logged before sightings recorded vibes would otherwise show nothing."""
    session = client.Session()
    cat = _make_cat(session)
    spotter = _make_user(session, "jamie@example.com")
    _add_sighting(session, cat, spotter, datetime.now(timezone.utc), vibes=None)
    cat.vibes = "grumpy, regal"
    session.commit()
    cat_id = cat.id
    session.close()

    assert _vibes(client, cat_id) == [("grumpy", 1), ("regal", 1)]


def test_no_vibes_anywhere_is_an_empty_list(client):
    session = client.Session()
    cat = _make_cat(session)
    spotter = _make_user(session, "jamie@example.com")
    _add_sighting(session, cat, spotter, datetime.now(timezone.utc), vibes=None)
    cat_id = cat.id
    session.close()

    assert _vibes(client, cat_id) == []


def test_sighting_vibes_beat_the_fallback(client):
    """The fallback is a last resort, not a supplement."""
    session = client.Session()
    cat = _make_cat(session)
    spotter = _make_user(session, "jamie@example.com")
    _add_sighting(session, cat, spotter, datetime.now(timezone.utc), vibes="shy")
    cat.vibes = "grumpy"
    session.commit()
    cat_id = cat.id
    session.close()

    assert _vibes(client, cat_id) == [("shy", 1)]


def test_sightings_no_longer_carry_vibes(client):
    """Counted server-side now, so the raw strings needn't be shipped."""
    session = client.Session()
    cat = _make_cat(session)
    spotter = _make_user(session, "jamie@example.com")
    _add_sighting(session, cat, spotter, datetime.now(timezone.utc), vibes="shy")
    cat_id = cat.id
    session.close()

    assert "vibes" not in client.get(f"/cats/{cat_id}").json()["sightings"][0]


# --- the N+1 the eager load exists to prevent -------------------------------

def test_query_count_does_not_scale_with_sightings(client, count_queries):
    """Reading spotter_emoji per row would fire one user query per sighting."""
    session = client.Session()
    cat = _make_cat(session)
    now = datetime.now(timezone.utc)
    for i in range(12):
        spotter = _make_user(session, f"spotter{i}@example.com", emoji="🐱")
        _add_sighting(session, cat, spotter, now - timedelta(days=i))
    cat_id = cat.id
    session.close()

    count_queries.n = 0
    body = client.get(f"/cats/{cat_id}").json()

    assert len(body["sightings"]) == 12
    assert all(s["spotter_emoji"] == "🐱" for s in body["sightings"])
    # cat + sightings + users + the owner-card lookup, not 12 user queries.
    assert count_queries.n <= 6, f"{count_queries.n} queries — looks like an N+1"
