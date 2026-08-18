"""A match candidate carries enough photos to actually recognise the cat.

/sightings/match-check used to send one `last_photo_path` per candidate, and the
camera showed it in a 52px thumbnail. Two grey tabbies on the same street are
indistinguishable at that size, so users guessed — or bailed to "new cat", which
splits one real cat into duplicate records. Candidates now carry up to
MATCH_CANDIDATE_PHOTOS recent photos for a swipeable pager.

The photo list is a per-cat carousel, so it owes the same debts every other
carousel does: hidden posts stay out, and the newest photos win. `last_photo_path`
stays on the response for clients shipped before the pager.
"""

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
from app.models.explorer import ExplorerPost
from app.models.sighting import Sighting
from app.routers.sightings import MATCH_CANDIDATE_PHOTOS
from app.services.storage import LocalStorage, set_storage

LAT, LNG = 51.5007, -0.1246

# Enough overlapping features to clear MIN_COMPARABLE_WEIGHT, all matching so the
# score lands at 1.0 — well over CONFIRM_THRESHOLD. The cat fixture is built from
# the same dict, so scoring never gets in the way of what these tests are about.
FEATURES = {
    "primary_color": "grey",
    "secondary_color": "white",
    "pattern": "tabby",
    "fur_length": "short",
    "eye_color": "green",
    "body_size": "medium",
    "breed": "Domestic Shorthair",
}


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


def _make_cat(session, last_photo_path="uploads/newest.jpg"):
    """A cat near LAT/LNG that scores 1.0 against FEATURES."""
    cat = Cat(
        name="Whiskers",
        last_lat=LAT,
        last_lng=LNG,
        last_seen=datetime.now(timezone.utc),
        last_photo_path=last_photo_path,
        sighting_count=1,
        **FEATURES,
    )
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def _sighting(session, cat_id, photo_path, days_ago=0, hidden=False):
    s = Sighting(
        cat_id=cat_id,
        photo_path=photo_path,
        latitude=LAT,
        longitude=LNG,
        spotted_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    if hidden:
        session.add(
            ExplorerPost(
                sighting_id=s.id,
                photo_path=photo_path,
                latitude=LAT,
                longitude=LNG,
                hidden_at=datetime.now(timezone.utc),
            )
        )
        session.commit()
    return s


def _candidates(client):
    res = client.post(
        "/sightings/match-check",
        json={"latitude": LAT, "longitude": LNG, **FEATURES},
    )
    assert res.status_code == 200
    return res.json()["candidates"]


def test_candidate_carries_its_photos_newest_first(client):
    session = client.Session()
    cat = _make_cat(session)
    _sighting(session, cat.id, "uploads/old.jpg", days_ago=9)
    _sighting(session, cat.id, "uploads/mid.jpg", days_ago=5)
    _sighting(session, cat.id, "uploads/new.jpg", days_ago=1)
    session.close()

    [cand] = _candidates(client)
    assert cand["photos"] == ["uploads/new.jpg", "uploads/mid.jpg", "uploads/old.jpg"]


def test_candidate_photos_are_capped_at_the_newest(client):
    session = client.Session()
    cat = _make_cat(session)
    # 12 photos, oldest first, so days_ago descends as the index rises.
    for i in range(12):
        _sighting(session, cat.id, f"uploads/{i:02d}.jpg", days_ago=12 - i)
    session.close()

    [cand] = _candidates(client)
    assert len(cand["photos"]) == MATCH_CANDIDATE_PHOTOS
    assert cand["photos"] == [f"uploads/{i:02d}.jpg" for i in range(11, 3, -1)]


def test_hidden_post_is_left_out_of_candidate_photos(client):
    """One moderation decision has to cover every surface, this one included."""
    session = client.Session()
    cat = _make_cat(session)
    _sighting(session, cat.id, "uploads/fine.jpg", days_ago=2)
    _sighting(session, cat.id, "uploads/reported.jpg", days_ago=1, hidden=True)
    session.close()

    [cand] = _candidates(client)
    assert cand["photos"] == ["uploads/fine.jpg"]


def test_all_photos_hidden_leaves_the_list_empty(client):
    """No last_photo_path fallback here, unlike /cats/nearby.

    A candidate must have a last_lat, and only a sighting commit sets one — so a
    candidate always has sightings, and an empty bucket can only mean every photo
    was moderated away. Falling back would put the hidden photo straight back on
    screen.
    """
    session = client.Session()
    cat = _make_cat(session)
    _sighting(session, cat.id, "uploads/reported.jpg", days_ago=1, hidden=True)
    session.close()

    [cand] = _candidates(client)
    assert cand["photos"] == []
    assert cand["last_photo_path"] == "uploads/newest.jpg"


def test_a_repeated_photo_path_appears_once(client):
    """The demo seed gives a cat two sightings sharing one photo_path.

    Un-deduped that renders as the same picture twice under two dots, which reads
    as a broken pager.
    """
    session = client.Session()
    cat = _make_cat(session)
    _sighting(session, cat.id, "uploads/same.jpg", days_ago=3)
    _sighting(session, cat.id, "uploads/same.jpg", days_ago=1)
    session.close()

    [cand] = _candidates(client)
    assert cand["photos"] == ["uploads/same.jpg"]


def test_last_photo_path_is_still_sent(client):
    """Clients shipped before the pager read this field and nothing else."""
    session = client.Session()
    cat = _make_cat(session, last_photo_path="uploads/cover.jpg")
    _sighting(session, cat.id, "uploads/cover.jpg", days_ago=1)
    session.close()

    [cand] = _candidates(client)
    assert cand["last_photo_path"] == "uploads/cover.jpg"


def test_photos_of_other_cats_do_not_leak_into_a_candidate(client):
    session = client.Session()
    # Ids up front: a later commit expires these instances, and they're detached
    # once the session closes.
    mine = _make_cat(session).id
    theirs = _make_cat(session).id
    _sighting(session, mine, "uploads/mine.jpg", days_ago=1)
    _sighting(session, theirs, "uploads/theirs.jpg", days_ago=1)
    session.close()

    by_id = {c["cat_id"]: c for c in _candidates(client)}
    assert by_id[mine]["photos"] == ["uploads/mine.jpg"]
    assert by_id[theirs]["photos"] == ["uploads/theirs.jpg"]
