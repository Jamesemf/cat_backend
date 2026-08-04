"""The report pipeline end to end: threshold auto-hide, the review queue, and
the three moderator outcomes.

Covers the behaviour that makes reporting more than a write-only table:
  * enough distinct reports hide a post, and hiding pulls it from every surface
    the photo reaches (Explorer feed, sighting feed, cat photo carousels);
  * a hidden post can't be meowed or commented on;
  * the author still sees their own post, flagged hidden; strangers get a 404;
  * dismiss restores the post and drains the queue without leaving it primed to
    re-hide off the same stale reports;
  * remove deletes the sighting too, so the photo leaves the map.
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
from app.models.explorer import ExplorerPost, PostReport
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


def _make_post(session, author, with_sighting=True):
    """A sighting mirrored into an Explorer post — the only shape posts come in."""
    cat = Cat(name="Whiskers")
    session.add(cat)
    session.commit()
    session.refresh(cat)

    sighting_id = None
    if with_sighting:
        s = Sighting(
            user_id=author.id,
            cat_id=cat.id,
            photo_path="uploads/spot.jpg",
            latitude=51.5,
            longitude=-0.12,
        )
        session.add(s)
        session.commit()
        session.refresh(s)
        sighting_id = s.id

    post = ExplorerPost(
        user_id=author.id,
        sighting_id=sighting_id,
        photo_path="uploads/spot.jpg",
        caption="a cat",
        latitude=51.5,
        longitude=-0.12,
    )
    session.add(post)
    session.commit()
    session.refresh(post)
    return post


def _report(client, post_id, reporter, reason="inappropriate"):
    return client.post(
        f"/explorer/posts/{post_id}/report",
        json={"reason": reason},
        headers=_auth(reporter),
    )


def _pile_on(client, session, post_id, n=AUTO_HIDE_REPORT_COUNT, start=0):
    """n distinct reporters file against a post."""
    for i in range(n):
        reporter = _make_user(session, f"reporter{start + i}@example.com")
        assert _report(client, post_id, reporter).status_code == 201


# --- the threshold ----------------------------------------------------------

def test_post_stays_visible_below_threshold(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    post = _make_post(session, author)
    post_id = post.id
    _pile_on(client, session, post_id, n=AUTO_HIDE_REPORT_COUNT - 1)

    fresh = session.get(ExplorerPost, post_id)
    session.refresh(fresh)
    assert fresh.hidden_at is None
    session.close()

    assert [p["id"] for p in client.get("/explorer/feed").json()] == [post_id]


def test_threshold_hides_post_and_pulls_it_from_the_feed(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    post = _make_post(session, author)
    post_id = post.id
    _pile_on(client, session, post_id)

    fresh = session.get(ExplorerPost, post_id)
    session.refresh(fresh)
    assert fresh.hidden_at is not None
    assert fresh.hidden_reason == "auto_reports"
    session.close()

    assert client.get("/explorer/feed").json() == []
    assert client.get(f"/explorer/posts/{post_id}").status_code == 404


def test_one_user_cannot_reach_the_threshold_alone(client):
    """Repeat reports from the same account are a no-op, not a hide button."""
    session = client.Session()
    author = _make_user(session, "author@example.com")
    lone = _make_user(session, "lone@example.com")
    post = _make_post(session, author)
    post_id = post.id
    lone_headers = _auth(lone)
    session.close()

    for _ in range(AUTO_HIDE_REPORT_COUNT + 2):
        r = client.post(
            f"/explorer/posts/{post_id}/report",
            json={"reason": "spam"},
            headers=lone_headers,
        )
        assert r.status_code == 201

    session = client.Session()
    assert session.query(PostReport).filter(PostReport.post_id == post_id).count() == 1
    assert session.get(ExplorerPost, post_id).hidden_at is None
    session.close()


def test_hidden_post_hides_from_sighting_feed_and_carousels(client):
    """The photo also reaches users via the map feed and per-cat carousels."""
    session = client.Session()
    author = _make_user(session, "author@example.com")
    post = _make_post(session, author)
    post_id = post.id
    session.close()

    assert len(client.get("/sightings/feed").json()) == 1
    assert client.get("/cats/nearby").json()[0]["photos"] == ["uploads/spot.jpg"]

    session = client.Session()
    _pile_on(client, session, post_id)
    session.close()

    assert client.get("/sightings/feed").json() == []
    assert client.get("/cats/nearby").json()[0]["photos"] != ["uploads/spot.jpg"]


# --- what a hidden post permits --------------------------------------------

def test_hidden_post_rejects_meows_and_comments(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    post = _make_post(session, author)
    post_id = post.id
    _pile_on(client, session, post_id)
    bystander = _make_user(session, "bystander@example.com")
    headers = _auth(bystander)
    session.close()

    assert client.post(f"/explorer/posts/{post_id}/meow", headers=headers).status_code == 403
    assert client.post(
        f"/explorer/posts/{post_id}/comments", json={"body": "hi"}, headers=headers
    ).status_code == 403
    assert client.get(f"/explorer/posts/{post_id}/comments", headers=headers).status_code == 404


def test_author_still_sees_their_hidden_post_flagged(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    post = _make_post(session, author)
    post_id = post.id
    _pile_on(client, session, post_id)
    headers = _auth(author)
    session.close()

    body = client.get(f"/explorer/posts/{post_id}", headers=headers).json()
    assert body["hidden"] is True
    assert [p["id"] for p in client.get("/explorer/feed", headers=headers).json()] == [post_id]


def test_reporting_is_still_allowed_on_an_already_hidden_post(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    post = _make_post(session, author)
    post_id = post.id
    _pile_on(client, session, post_id)
    late = _make_user(session, "late@example.com")
    session.close()

    assert _report(client, post_id, late).status_code == 201


# --- the queue --------------------------------------------------------------

def test_queue_requires_admin(client):
    session = client.Session()
    plain = _make_user(session, "plain@example.com")
    session.close()

    assert client.get("/moderation/reports").status_code in (401, 403)
    assert client.get("/moderation/reports", headers=_auth(plain)).status_code == 403


def test_queue_groups_by_post_and_ranks_worst_first(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    admin = _make_user(session, "admin@example.com", is_admin=True)
    light = _make_post(session, author)
    heavy = _make_post(session, author)
    light_id, heavy_id = light.id, heavy.id
    _pile_on(client, session, light_id, n=1, start=0)
    _pile_on(client, session, heavy_id, n=3, start=10)
    headers = _auth(admin)
    session.close()

    rows = client.get("/moderation/reports", headers=headers).json()
    assert [r["post_id"] for r in rows] == [heavy_id, light_id]

    worst = rows[0]
    assert worst["open_report_count"] == 3
    assert len(worst["reports"]) == 3
    assert worst["hidden"] is True
    assert worst["author_name"] == "author"
    assert worst["reasons"] == ["inappropriate"]


def test_queue_carries_reason_detail_and_reporter(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    admin = _make_user(session, "admin@example.com", is_admin=True)
    reporter = _make_user(session, "nosy@example.com")
    post = _make_post(session, author)
    post_id = post.id
    headers = _auth(admin)
    reporter_headers = _auth(reporter)
    session.close()

    client.post(
        f"/explorer/posts/{post_id}/report",
        json={"reason": "animal_harm", "detail": "looks injured"},
        headers=reporter_headers,
    )

    rows = client.get("/moderation/reports", headers=headers).json()
    report = rows[0]["reports"][0]
    assert report["reason"] == "animal_harm"
    assert report["detail"] == "looks injured"
    assert report["reporter_name"] == "nosy"


# --- the three outcomes -----------------------------------------------------

def test_dismiss_restores_the_post_and_drains_the_queue(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    admin = _make_user(session, "admin@example.com", is_admin=True)
    post = _make_post(session, author)
    post_id = post.id
    _pile_on(client, session, post_id)
    headers = _auth(admin)
    session.close()

    r = client.post(f"/moderation/posts/{post_id}/dismiss", headers=headers)
    assert r.status_code == 200
    assert r.json() == {"post_id": post_id, "hidden": False, "open_report_count": 0}

    assert [p["id"] for p in client.get("/explorer/feed").json()] == [post_id]
    assert client.get("/moderation/reports", headers=headers).json() == []
    assert len(client.get("/sightings/feed").json()) == 1


def test_dismissed_post_needs_fresh_reports_to_hide_again(client):
    """Resolved reports are spent — they must not re-trip the threshold."""
    session = client.Session()
    author = _make_user(session, "author@example.com")
    admin = _make_user(session, "admin@example.com", is_admin=True)
    post = _make_post(session, author)
    post_id = post.id
    _pile_on(client, session, post_id, start=0)
    headers = _auth(admin)
    session.close()

    client.post(f"/moderation/posts/{post_id}/dismiss", headers=headers)

    # One new reporter must not re-hide it off the back of the old, spent ones.
    session = client.Session()
    _pile_on(client, session, post_id, n=1, start=50)
    assert session.get(ExplorerPost, post_id).hidden_at is None

    # A full fresh set does.
    _pile_on(client, session, post_id, n=AUTO_HIDE_REPORT_COUNT - 1, start=60)
    fresh = session.get(ExplorerPost, post_id)
    session.refresh(fresh)
    assert fresh.hidden_at is not None
    session.close()


def test_moderator_hide_survives_dismissal_of_nothing(client):
    """Hiding a post with no reports at all is a valid pre-emptive action."""
    session = client.Session()
    author = _make_user(session, "author@example.com")
    admin = _make_user(session, "admin@example.com", is_admin=True)
    post = _make_post(session, author)
    post_id = post.id
    headers = _auth(admin)
    session.close()

    r = client.post(f"/moderation/posts/{post_id}/hide", headers=headers)
    assert r.status_code == 200
    assert r.json()["hidden"] is True

    session = client.Session()
    assert session.get(ExplorerPost, post_id).hidden_reason == "moderator"
    session.close()
    assert client.get("/explorer/feed").json() == []


def test_remove_deletes_the_post_the_sighting_and_the_reports(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    admin = _make_user(session, "admin@example.com", is_admin=True)
    post = _make_post(session, author)
    post_id, sighting_id = post.id, post.sighting_id
    _pile_on(client, session, post_id)
    headers = _auth(admin)
    session.close()

    assert client.delete(f"/moderation/posts/{post_id}", headers=headers).status_code == 204

    session = client.Session()
    assert session.get(ExplorerPost, post_id) is None
    assert session.get(Sighting, sighting_id) is None
    assert session.query(PostReport).filter(PostReport.post_id == post_id).count() == 0
    session.close()

    assert client.get("/moderation/reports", headers=headers).json() == []
    assert client.get("/sightings/feed").json() == []


def test_remove_does_not_strike_or_ban_the_author(client):
    """Reports are unverified; only vision's verdict may cost the author."""
    session = client.Session()
    author = _make_user(session, "author@example.com")
    admin = _make_user(session, "admin@example.com", is_admin=True)
    post = _make_post(session, author)
    post_id, author_id = post.id, author.id
    _pile_on(client, session, post_id)
    headers = _auth(admin)
    session.close()

    client.delete(f"/moderation/posts/{post_id}", headers=headers)

    session = client.Session()
    fresh_author = session.get(User, author_id)
    assert fresh_author.content_strikes == 0
    assert fresh_author.banned_at is None
    session.close()


def test_photo_route_is_admin_only(client, storage):
    """The public /uploads route serves the feed, so the review tool needs its
    own gated fetch — otherwise anyone with an account can still pull a photo
    that moderation has hidden."""
    storage.save("uploads/spot.jpg", b"\xff\xd8\xff\xe0 not really a jpeg")

    session = client.Session()
    plain = _make_user(session, "plain@example.com")
    admin = _make_user(session, "admin@example.com", is_admin=True)
    plain_headers, admin_headers = _auth(plain), _auth(admin)
    session.close()

    url = "/moderation/photo?key=uploads/spot.jpg"
    assert client.get(url).status_code in (401, 403)
    assert client.get(url, headers=plain_headers).status_code == 403

    ok = client.get(url, headers=admin_headers)
    assert ok.status_code == 200
    assert ok.content.startswith(b"\xff\xd8\xff")

    # Keys outside the uploads tree are refused before any file access.
    assert client.get(
        "/moderation/photo?key=../cats.db", headers=admin_headers
    ).status_code == 404


def test_moderator_actions_require_admin(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    plain = _make_user(session, "plain@example.com")
    post = _make_post(session, author)
    post_id = post.id
    headers = _auth(plain)
    session.close()

    assert client.post(f"/moderation/posts/{post_id}/hide", headers=headers).status_code == 403
    assert client.post(f"/moderation/posts/{post_id}/dismiss", headers=headers).status_code == 403
    assert client.delete(f"/moderation/posts/{post_id}", headers=headers).status_code == 403
