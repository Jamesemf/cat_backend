"""The friend audience in the sighting fan-out, and the prefs that gate it.

notify_sighting_audiences opens its own session (it runs in a BackgroundTasks
worker), so these call it directly with a patched SessionLocal rather than going
through the endpoint — the same reason it takes ids rather than ORM objects.
"""

import datetime
from typing import NamedTuple

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registers every model on Base before create_all
import app.services.sighting_notifications as fanout
from app.db.session import Base
from app.models.cat import Cat
from app.models.exploration import ExploredTile
from app.models.friendship import Friendship
from app.models.notification import Notification
from app.models.sighting import Sighting
from app.models.user import User
from app.services.auth_service import hash_password
from app.utils.hexgrid import tile_at

HOME = (51.5074, -0.1278)
FAR_AWAY = (55.9533, -3.1883)  # ~530 km from HOME


def _tile_key(lat: float, lng: float) -> str:
    """The explored-tile key covering a point, in the "q,r" form disk_keys emits."""
    q, r = tile_at(lng, lat)
    return f"{q},{r}"


@pytest.fixture
def db(monkeypatch):
    """A session factory, with the fan-out's own SessionLocal pointed at it."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    # The task opens its own session; without this it would reach for the real
    # dev database instead of this one.
    monkeypatch.setattr(fanout, "SessionLocal", Session)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


class U(NamedTuple):
    id: int


def _user(session, email, display_name="Spotter", **overrides):
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
    return U(user.id)


def _befriend(session, a, b):
    low, high = Friendship.normalise_pair(a.id, b.id)
    session.add(
        Friendship(
            user_low_id=low,
            user_high_id=high,
            requested_by_id=a.id,
            status="accepted",
            responded_at=datetime.datetime(2026, 1, 1),
        )
    )
    session.commit()


def _pending(session, a, b):
    low, high = Friendship.normalise_pair(a.id, b.id)
    session.add(
        Friendship(
            user_low_id=low, user_high_id=high, requested_by_id=a.id, status="pending"
        )
    )
    session.commit()


def _spot(session, user_id, coords=FAR_AWAY):
    cat = Cat(name="Mittens")
    session.add(cat)
    session.commit()
    session.refresh(cat)
    lat, lng = coords
    s = Sighting(
        user_id=user_id, cat_id=cat.id, photo_path="uploads/x.jpg", latitude=lat, longitude=lng
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s, cat


def _run(session, spotter, *, is_new_cat, coords=FAR_AWAY, spotter_name="Ada"):
    s, cat = _spot(session, spotter.id, coords)
    fanout.notify_sighting_audiences(
        s.id,
        cat.id,
        cat.name,
        s.latitude,
        s.longitude,
        is_new_cat,
        {spotter.id},
        spotter.id,
        spotter_name,
    )
    return s, cat


def _notifs(session, user, notif_type=None):
    session.expire_all()
    q = session.query(Notification).filter(Notification.user_id == user.id)
    if notif_type:
        q = q.filter(Notification.type == notif_type)
    return q.all()


def test_a_new_cat_notifies_friends_at_any_distance(db):
    """The default-on case: a friend finds something nobody has logged."""
    ada = _user(db, "ada@example.com", "Ada")
    bo = _user(db, "bo@example.com", "Bo")
    _befriend(db, ada, bo)

    _run(db, ada, is_new_cat=True)

    rows = _notifs(db, bo, "friend_new_cat")
    assert len(rows) == 1
    assert rows[0].title == "Ada found a new cat!"
    # Deep-link ids are carried, unlike the request/accepted types.
    assert rows[0].cat_id is not None
    assert rows[0].sighting_id is not None


def test_a_routine_sighting_is_off_by_default(db):
    ada = _user(db, "ada@example.com", "Ada")
    bo = _user(db, "bo@example.com", "Bo")
    _befriend(db, ada, bo)

    _run(db, ada, is_new_cat=False)

    assert _notifs(db, bo) == []


def test_a_routine_sighting_notifies_once_opted_in(db):
    ada = _user(db, "ada@example.com", "Ada")
    bo = _user(db, "bo@example.com", "Bo", notify_friend_sightings=True)
    _befriend(db, ada, bo)

    _run(db, ada, is_new_cat=False)

    rows = _notifs(db, bo, "friend_sighting")
    assert len(rows) == 1
    assert rows[0].title == "Ada spotted Mittens"


def test_opting_out_of_new_cats_silences_them(db):
    ada = _user(db, "ada@example.com", "Ada")
    bo = _user(db, "bo@example.com", "Bo", notify_friend_new_cats=False)
    _befriend(db, ada, bo)

    _run(db, ada, is_new_cat=True)

    assert _notifs(db, bo) == []


def test_strangers_and_pending_requests_are_not_notified(db):
    ada = _user(db, "ada@example.com", "Ada")
    stranger = _user(db, "s@example.com", "Sam")
    asked = _user(db, "p@example.com", "Pat")
    _pending(db, ada, asked)

    _run(db, ada, is_new_cat=True)

    assert _notifs(db, stranger) == []
    assert _notifs(db, asked) == []


def test_the_spotter_is_never_notified_about_their_own_spot(db):
    ada = _user(db, "ada@example.com", "Ada")
    bo = _user(db, "bo@example.com", "Bo")
    _befriend(db, ada, bo)

    _run(db, ada, is_new_cat=True)

    assert _notifs(db, ada) == []


def test_a_friend_who_is_also_nearby_hears_it_as_a_friend(db):
    """Friends run first, so the more specific notification wins the dedupe."""
    ada = _user(db, "ada@example.com", "Ada")
    bo = _user(db, "bo@example.com", "Bo")
    _befriend(db, ada, bo)
    # Bo has explored the ground the spot lands on, so he qualifies for both.
    lat, lng = HOME
    db.add(ExploredTile(user_id=bo.id, tile_key=_tile_key(lat, lng), is_home=False))
    db.commit()

    _run(db, ada, is_new_cat=True, coords=HOME)

    rows = _notifs(db, bo)
    assert len(rows) == 1
    assert rows[0].type == "friend_new_cat"


def test_a_nearby_stranger_still_gets_the_area_notification(db):
    """The friend block must not have displaced the existing audience."""
    ada = _user(db, "ada@example.com", "Ada")
    neighbour = _user(db, "n@example.com", "Nadia")
    lat, lng = HOME
    db.add(ExploredTile(user_id=neighbour.id, tile_key=_tile_key(lat, lng), is_home=False))
    db.commit()

    _run(db, ada, is_new_cat=True, coords=HOME)

    rows = _notifs(db, neighbour)
    assert len(rows) == 1
    assert rows[0].type == "new_cat"


def test_an_anonymous_spot_notifies_no_friends(db):
    """No spotter id, no friends to look up — and no crash."""
    ada = _user(db, "ada@example.com", "Ada")
    bo = _user(db, "bo@example.com", "Bo")
    _befriend(db, ada, bo)

    s, cat = _spot(db, None)
    fanout.notify_sighting_audiences(
        s.id, cat.id, cat.name, s.latitude, s.longitude, True, set(), None, None
    )

    assert _notifs(db, bo) == []


def test_a_missing_display_name_falls_back(db):
    ada = _user(db, "ada@example.com", None)
    bo = _user(db, "bo@example.com", "Bo")
    _befriend(db, ada, bo)

    _run(db, ada, is_new_cat=True, spotter_name=None)

    assert _notifs(db, bo, "friend_new_cat")[0].title == "A friend found a new cat!"
