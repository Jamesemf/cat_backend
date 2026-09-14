"""Duplicate cats: a report changes nothing until a moderator picks a survivor.

Two things are being tested here, and the second is the reason the first can
exist at all. The queue itself — filing, the per-pair guard, and a decision that
folds one profile into the other — and the referential cleanup underneath it,
which was already broken before this feature and would have made approving a
merge fail outright on Postgres.

That cleanup is deliberately exercised through the endpoints rather than by
calling the service directly, because the bug it fixes was never in the service:
it was in what the callers forgot to tell it about.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registers every model on Base before create_all
from app.db.session import Base, get_db
from app.main import app
from app.models.cat import Cat
from app.models.cat_merge import CatMergeRequest
from app.models.claim import CatClaim
from app.models.explorer import ExplorerPost
from app.models.notification import Notification
from app.models.sighting import Sighting
from app.models.trait_change import TraitChangeRequest
from app.models.user import User
from app.routers.cat_merges import MAX_MERGE_REQUESTS_PER_DAY
from app.services.auth_service import create_access_token, hash_password


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

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
def strict_client():
    """A client whose SQLite actually enforces foreign keys.

    SQLite leaves them off by default, which is why the dangling-reference bugs
    this feature sits on top of went unnoticed: they only bite on Postgres,
    where a delete with a child row still pointing at it is refused outright.
    Turning the pragma on reproduces that here.
    """
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

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


def _make_cat(session, name="Mittens", **overrides):
    fields = {
        "name": name,
        "last_photo_path": f"uploads/{name.lower()}.jpg",
        "primary_color": "ginger",
        "pattern": "tabby",
        "sighting_count": 0,
        **overrides,
    }
    cat = Cat(**fields)
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def _add_sighting(session, cat, days_ago=0, photo=None):
    s = Sighting(
        cat_id=cat.id,
        photo_path=photo or f"uploads/s{cat.id}-{days_ago}.jpg",
        latitude=51.5,
        longitude=-0.12,
        spotted_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    session.add(s)
    cat.sighting_count = (
        session.query(Sighting).filter(Sighting.cat_id == cat.id).count() + 1
    )
    session.commit()
    session.refresh(s)
    return s


def _file(client, user, cat_id, other_id, keep=None, note=None):
    return client.post(
        f"/cats/{cat_id}/merge-request",
        json={"other_cat_id": other_id, "suggested_keep_id": keep, "note": note},
        headers=_auth(user),
    )


# ---------------------------------------------------------------------------
# Filing a report
# ---------------------------------------------------------------------------


def test_a_pending_report_merges_nothing(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")

    res = _file(client, user, a.id, b.id)
    assert res.status_code == 201
    assert res.json()["status"] == "pending"

    # Both cats still there, untouched.
    assert session.query(Cat).filter(Cat.id == a.id).first() is not None
    assert session.query(Cat).filter(Cat.id == b.id).first() is not None


def test_the_pair_is_stored_order_normalised(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")

    # Filed from the higher id, so the stored pair has to be flipped.
    _file(client, user, b.id, a.id)

    row = session.query(CatMergeRequest).first()
    assert (row.cat_a_id, row.cat_b_id) == (min(a.id, b.id), max(a.id, b.id))


def test_names_are_snapshotted_at_filing_time(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")

    _file(client, user, a.id, b.id)

    row = session.query(CatMergeRequest).first()
    assert {row.cat_a_name, row.cat_b_name} == {"Mittens", "Socks"}


def test_a_cat_cannot_be_reported_against_itself(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    a = _make_cat(session)

    res = _file(client, user, a.id, a.id)
    assert res.status_code == 400


def test_an_unknown_cat_is_a_404(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    a = _make_cat(session)

    assert _file(client, user, a.id, 9999).status_code == 404


def test_suggesting_a_keeper_outside_the_pair_is_refused(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    a, b, c = (
        _make_cat(session, "Mittens"),
        _make_cat(session, "Socks"),
        _make_cat(session, "Tom"),
    )

    assert _file(client, user, a.id, b.id, keep=c.id).status_code == 400


def test_the_open_guard_is_per_pair_not_per_person(client):
    """Two people noticing the same duplicate is one thing to decide, not two."""
    session = client.Session()
    first = _make_user(session, "first@example.com")
    second = _make_user(session, "second@example.com")
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")

    assert _file(client, first, a.id, b.id).status_code == 201
    # A different person, and the other way round. Still the same pair.
    res = _file(client, second, b.id, a.id)
    assert res.status_code == 409
    assert session.query(CatMergeRequest).count() == 1


def test_a_decided_pair_can_be_reported_again(client):
    """Rejection isn't permanent: cats change, and so can the answer."""
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")

    _file(client, user, a.id, b.id)
    request_id = session.query(CatMergeRequest).first().id
    client.post(
        f"/moderation/merges/{request_id}/reject", json={"reason": ""}, headers=_auth(admin)
    )

    assert _file(client, user, a.id, b.id).status_code == 201


def test_the_daily_limit_is_enforced(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    cats = [_make_cat(session, f"Cat{i}") for i in range(MAX_MERGE_REQUESTS_PER_DAY * 2 + 2)]

    for i in range(MAX_MERGE_REQUESTS_PER_DAY):
        res = _file(client, user, cats[i * 2].id, cats[i * 2 + 1].id)
        assert res.status_code == 201

    over = _file(client, user, cats[-2].id, cats[-1].id)
    assert over.status_code == 429


def test_a_rejected_report_does_not_burn_the_daily_budget(client):
    """The limit is spent last on purpose, so a 400 costs nothing."""
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    a = _make_cat(session)

    for _ in range(MAX_MERGE_REQUESTS_PER_DAY + 3):
        assert _file(client, user, a.id, a.id).status_code == 400

    b = _make_cat(session, "Socks")
    assert _file(client, user, a.id, b.id).status_code == 201


def test_filing_notifies_the_requester_without_pushing(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")

    _file(client, user, a.id, b.id)

    note = session.query(Notification).filter(Notification.user_id == user.id).first()
    assert note.type == "merge_request_pending"
    assert "Mittens" in note.title


def test_signing_in_is_required(client):
    session = client.Session()
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")

    res = client.post(f"/cats/{a.id}/merge-request", json={"other_cat_id": b.id})
    assert res.status_code in (401, 403)


# ---------------------------------------------------------------------------
# Reading your own report back
# ---------------------------------------------------------------------------


def test_mine_reads_the_same_from_either_cat(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _file(client, user, a.id, b.id)

    from_a = client.get(f"/cats/{a.id}/merge-request/mine", headers=_auth(user)).json()
    from_b = client.get(f"/cats/{b.id}/merge-request/mine", headers=_auth(user)).json()

    assert from_a["request_id"] == from_b["request_id"]
    # "The other cat" is whichever one you aren't looking at.
    assert from_a["other_cat_id"] == b.id and from_a["other_cat_name"] == "Socks"
    assert from_b["other_cat_id"] == a.id and from_b["other_cat_name"] == "Mittens"


def test_mine_is_null_rather_than_404_when_there_is_none(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    a = _make_cat(session)

    res = client.get(f"/cats/{a.id}/merge-request/mine", headers=_auth(user))
    assert res.status_code == 200
    assert res.json() is None


def test_mine_does_not_show_someone_elses_report(client):
    session = client.Session()
    author = _make_user(session, "author@example.com")
    other = _make_user(session, "other@example.com")
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _file(client, author, a.id, b.id)

    assert client.get(f"/cats/{a.id}/merge-request/mine", headers=_auth(other)).json() is None


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def test_the_queue_is_admin_only(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")

    assert client.get("/moderation/merges", headers=_auth(user)).status_code == 403


def test_the_queue_carries_both_cats_side_by_side(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a = _make_cat(session, "Mittens", primary_color="ginger")
    b = _make_cat(session, "Socks", primary_color="black")
    _file(client, user, a.id, b.id, keep=a.id, note="Same notch in the left ear")

    row = client.get("/moderation/merges", headers=_auth(admin)).json()[0]

    assert row["cat_a"]["traits"]["primary_color"] == "ginger"
    assert row["cat_b"]["traits"]["primary_color"] == "black"
    assert row["suggested_keep_id"] == a.id
    assert row["note"] == "Same notch in the left ear"
    assert row["requester_name"] == "spotter"


def test_the_queue_flags_the_blockers_before_the_click(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    session.add(CatClaim(cat_id=a.id, user_id=user.id, status="verified"))
    session.add(CatClaim(cat_id=b.id, user_id=user.id, status="pending"))
    session.commit()
    _file(client, user, a.id, b.id)

    row = client.get("/moderation/merges", headers=_auth(admin)).json()[0]

    assert row["cat_a"]["has_verified_owner"] is True
    assert row["cat_b"]["pending_claims"] == 1
    # Owning either side earns the badge.
    assert row["is_owner"] is True


def test_resolved_reports_are_hidden_until_asked_for(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _file(client, user, a.id, b.id)
    rid = session.query(CatMergeRequest).first().id
    client.post(f"/moderation/merges/{rid}/reject", json={"reason": ""}, headers=_auth(admin))

    assert client.get("/moderation/merges", headers=_auth(admin)).json() == []
    assert len(
        client.get("/moderation/merges?include_resolved=true", headers=_auth(admin)).json()
    ) == 1


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def _approve(client, admin, request_id, keep_cat_id):
    return client.post(
        f"/moderation/merges/{request_id}/approve",
        json={"keep_cat_id": keep_cat_id},
        headers=_auth(admin),
    )


def test_approving_folds_the_other_cat_in(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _add_sighting(session, keep, days_ago=5)
    _add_sighting(session, dupe, days_ago=1)
    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    assert _approve(client, admin, rid, keep.id).status_code == 200

    assert session.query(Cat).filter(Cat.id == dupe.id).first() is None
    survivor = session.query(Cat).filter(Cat.id == keep.id).first()
    session.refresh(survivor)
    assert survivor.sighting_count == 2
    assert session.query(Sighting).filter(Sighting.cat_id == dupe.id).count() == 0


def test_the_moderator_may_keep_the_cat_the_requester_did_not_suggest(client):
    """The suggestion seeds the choice; it doesn't make it."""
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _file(client, user, a.id, b.id, keep=a.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, b.id)

    assert session.query(Cat).filter(Cat.id == a.id).first() is None
    assert session.query(Cat).filter(Cat.id == b.id).first() is not None
    row = session.query(CatMergeRequest).filter(CatMergeRequest.id == rid).first()
    session.refresh(row)
    assert row.merged_into_cat_id == b.id


def test_the_decided_row_survives_its_lost_cat(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    row = session.query(CatMergeRequest).filter(CatMergeRequest.id == rid).first()
    session.refresh(row)
    assert row.status == "merged"
    # No column still points at the deleted cat, but the names remain readable.
    assert dupe.id not in (row.cat_a_id, row.cat_b_id)
    assert {row.cat_a_name, row.cat_b_name} == {"Mittens", "Socks"}


def test_keeping_a_cat_outside_the_pair_is_refused(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a, b, c = (
        _make_cat(session, "Mittens"),
        _make_cat(session, "Socks"),
        _make_cat(session, "Tom"),
    )
    _file(client, user, a.id, b.id)
    rid = session.query(CatMergeRequest).first().id

    assert _approve(client, admin, rid, c.id).status_code == 400
    assert session.query(Cat).count() == 3


def test_two_verified_owners_block_the_merge(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    other = _make_user(session, "other@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    session.add(CatClaim(cat_id=a.id, user_id=user.id, status="verified"))
    session.add(CatClaim(cat_id=b.id, user_id=other.id, status="verified"))
    session.commit()
    _file(client, user, a.id, b.id)
    rid = session.query(CatMergeRequest).first().id

    res = _approve(client, admin, rid, a.id)
    assert res.status_code == 409
    assert "verified owner" in res.json()["detail"]
    assert session.query(Cat).count() == 2


def test_a_claim_awaiting_review_blocks_the_merge(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    session.add(CatClaim(cat_id=b.id, user_id=user.id, status="pending"))
    session.commit()
    _file(client, user, a.id, b.id)
    rid = session.query(CatMergeRequest).first().id

    res = _approve(client, admin, rid, a.id)
    assert res.status_code == 409
    assert session.query(Cat).count() == 2


def test_deciding_twice_is_refused(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _file(client, user, a.id, b.id)
    rid = session.query(CatMergeRequest).first().id

    assert _approve(client, admin, rid, a.id).status_code == 200
    again = _approve(client, admin, rid, a.id)
    assert again.status_code == 409
    assert "already merged" in again.json()["detail"]


def test_rejecting_leaves_both_cats_alone(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _file(client, user, a.id, b.id)
    rid = session.query(CatMergeRequest).first().id

    res = client.post(
        f"/moderation/merges/{rid}/reject",
        json={"reason": "Different ear notches."},
        headers=_auth(admin),
    )
    assert res.status_code == 200
    assert session.query(Cat).count() == 2

    note = (
        session.query(Notification)
        .filter(Notification.type == "merge_request_rejected")
        .first()
    )
    assert note.body == "Different ear notches."


def test_a_blank_rejection_still_says_something(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _file(client, user, a.id, b.id)
    rid = session.query(CatMergeRequest).first().id

    client.post(f"/moderation/merges/{rid}/reject", json={"reason": "  "}, headers=_auth(admin))

    note = (
        session.query(Notification)
        .filter(Notification.type == "merge_request_rejected")
        .first()
    )
    assert note.body.strip()


def test_approving_tells_the_requester_which_way_it_went(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    note = (
        session.query(Notification)
        .filter(Notification.type == "merge_request_merged")
        .first()
    )
    assert "Socks" in note.title and "Mittens" in note.title
    assert note.cat_id == keep.id


# ---------------------------------------------------------------------------
# The referential cleanup underneath
# ---------------------------------------------------------------------------


def test_a_merge_repoints_trait_suggestions(client):
    """The gap that made this whole feature impossible on Postgres.

    trait_change_requests.cat_id is NOT NULL, so a suggestion left pointing at
    the duplicate doesn't merely dangle — it makes the delete fail.
    """
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    session.add(
        TraitChangeRequest(
            cat_id=dupe.id,
            user_id=user.id,
            status="pending",
            proposed_json=json.dumps({"primary_color": "ginger"}),
        )
    )
    session.commit()
    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    assert _approve(client, admin, rid, keep.id).status_code == 200

    suggestion = session.query(TraitChangeRequest).first()
    session.refresh(suggestion)
    assert suggestion.cat_id == keep.id


def test_a_merge_moves_tagged_posts_and_notifications(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    session.add(
        ExplorerPost(user_id=user.id, cat_id=dupe.id, photo_path="uploads/p.jpg")
    )
    session.add(
        Notification(
            user_id=user.id, type="sighting", title="t", body="b", cat_id=dupe.id
        )
    )
    session.commit()
    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    assert session.query(ExplorerPost).filter(ExplorerPost.cat_id == dupe.id).count() == 0
    assert session.query(ExplorerPost).filter(ExplorerPost.cat_id == keep.id).count() == 1
    assert session.query(Notification).filter(Notification.cat_id == dupe.id).count() == 0


def test_a_merge_rewrites_catalog_layouts(client):
    """A merged-away cat shouldn't linger in anyone's Cat-a-log arrangement."""
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    user.catalog_layout = json.dumps(
        {
            "order": [dupe.id, 999],
            "frames": {str(dupe.id): "gold"},
            "covers": {str(dupe.id): "uploads/socks.jpg"},
        }
    )
    session.commit()
    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    session.refresh(user)
    layout = json.loads(user.catalog_layout)
    assert layout["order"] == [keep.id, 999]
    assert layout["frames"] == {str(keep.id): "gold"}
    # The cover moves honestly: that sighting moved to the survivor too.
    assert layout["covers"] == {str(keep.id): "uploads/socks.jpg"}


def test_an_existing_setting_for_the_survivor_wins(client):
    """Their choice about the cat they're keeping isn't overwritten."""
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    user.catalog_layout = json.dumps(
        {
            "order": [keep.id, dupe.id],
            "frames": {str(keep.id): "silver", str(dupe.id): "gold"},
        }
    )
    session.commit()
    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    session.refresh(user)
    layout = json.loads(user.catalog_layout)
    assert layout["order"] == [keep.id]
    assert layout["frames"] == {str(keep.id): "silver"}


def test_another_report_naming_the_lost_cat_is_superseded(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep = _make_cat(session, "Mittens")
    dupe = _make_cat(session, "Socks")
    third = _make_cat(session, "Tom")
    _file(client, user, keep.id, dupe.id)
    _file(client, user, dupe.id, third.id)
    decided = (
        session.query(CatMergeRequest)
        .filter(CatMergeRequest.cat_a_id.in_((keep.id, dupe.id)))
        .filter(CatMergeRequest.cat_b_id.in_((keep.id, dupe.id)))
        .first()
    )

    _approve(client, admin, decided.id, keep.id)

    other = (
        session.query(CatMergeRequest).filter(CatMergeRequest.id != decided.id).first()
    )
    session.refresh(other)
    assert other.status == "superseded"
    assert other.decided_at is not None
    # And the report that was actually decided is not caught by that sweep.
    session.refresh(decided)
    assert decided.status == "merged"


def test_deleting_a_cat_lets_go_of_its_reports_and_suggestions(client):
    """delete_cat runs on its own whenever an unclaimed cat loses its last sighting."""
    from app.services.content_deletion import delete_cat

    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    session.add(
        TraitChangeRequest(
            cat_id=a.id,
            user_id=user.id,
            status="pending",
            proposed_json=json.dumps({"pattern": "tabby"}),
        )
    )
    session.commit()
    _file(client, user, a.id, b.id)

    doomed = session.query(Cat).filter(Cat.id == a.id).first()
    delete_cat(session, doomed)
    session.commit()

    assert session.query(TraitChangeRequest).count() == 0
    report = session.query(CatMergeRequest).first()
    session.refresh(report)
    assert report.status == "superseded"
    assert a.id not in (report.cat_a_id, report.cat_b_id)


def test_deleting_an_account_leaves_no_dangling_reference(client):
    session = client.Session()
    user = _make_user(session, "leaver@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _file(client, user, a.id, b.id)
    session.add(
        TraitChangeRequest(
            cat_id=a.id,
            user_id=user.id,
            status="pending",
            proposed_json=json.dumps({"pattern": "tabby"}),
        )
    )
    session.commit()
    uid = user.id

    assert client.delete("/auth/me", headers=_auth(user)).status_code == 204

    # Their trait suggestion goes with them; the duplicate report stays, because
    # it's an observation about two profiles rather than a request for anything.
    assert session.query(TraitChangeRequest).filter(TraitChangeRequest.user_id == uid).count() == 0
    report = session.query(CatMergeRequest).first()
    session.refresh(report)
    assert report.user_id is None
    assert report.status == "pending"

    # And it can still be decided, with nobody left to notify.
    assert _approve(client, admin, report.id, a.id).status_code == 200


def test_the_survivor_keeps_the_earlier_first_seen(client):
    """Absorbing an older record can't make a cat younger than its own history."""
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    recent = datetime.now(timezone.utc) - timedelta(days=5)
    ancient = datetime.now(timezone.utc) - timedelta(days=400)
    keep = _make_cat(session, "Mittens", first_seen=recent)
    dupe = _make_cat(session, "Socks", first_seen=ancient)
    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    survivor = session.query(Cat).filter(Cat.id == keep.id).first()
    session.refresh(survivor)
    # Within a second — the column stores naive UTC, so compare loosely.
    assert abs((survivor.first_seen - ancient.replace(tzinfo=None)).total_seconds()) < 2


def test_first_seen_is_untouched_when_the_survivor_is_already_older(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    ancient = datetime.now(timezone.utc) - timedelta(days=400)
    keep = _make_cat(session, "Mittens", first_seen=ancient)
    dupe = _make_cat(session, "Socks", first_seen=datetime.now(timezone.utc))
    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    survivor = session.query(Cat).filter(Cat.id == keep.id).first()
    session.refresh(survivor)
    assert abs((survivor.first_seen - ancient.replace(tzinfo=None)).total_seconds()) < 2


def test_the_vibes_follow_the_newest_photo(client):
    """last_photo_path and vibes describe the same sighting, or neither means much."""
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep = _make_cat(session, "Mittens", vibes="grumpy")
    dupe = _make_cat(session, "Socks")
    # The survivor's own sighting is older, so the duplicate's becomes the newest.
    _add_sighting(session, keep, days_ago=9)
    newest = _add_sighting(session, dupe, days_ago=1)
    newest.vibes = "extremely fluffy"
    session.commit()
    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    survivor = session.query(Cat).filter(Cat.id == keep.id).first()
    session.refresh(survivor)
    assert survivor.last_photo_path == newest.photo_path
    assert survivor.vibes == "extremely fluffy"


def test_a_wordless_newest_sighting_leaves_the_vibes_alone(client):
    """Matches the reassignment path: only real words overwrite real words."""
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep = _make_cat(session, "Mittens", vibes="grumpy")
    dupe = _make_cat(session, "Socks")
    _add_sighting(session, keep, days_ago=9)
    _add_sighting(session, dupe, days_ago=1)  # no vibes on it
    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    survivor = session.query(Cat).filter(Cat.id == keep.id).first()
    session.refresh(survivor)
    assert survivor.vibes == "grumpy"


def test_demo_content_cannot_be_merged(client):
    """The seeded neighbourhood is rebuilt from scratch on every startup.

    Keeping the demo cat would hide real sightings behind the demo filter and
    then delete them at the next restart; keeping the real one orphans the
    seed's own rows. Refused in both directions.
    """
    from app.services.demo_seed import demo_features

    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    real = _make_cat(session, "Mittens")
    demo = _make_cat(session, "Seeded", features_json=demo_features("cat:willow"))
    _add_sighting(session, real, days_ago=1)

    # Admins can see demo content, so an admin is who could file this at all.
    assert _file(client, admin, real.id, demo.id).status_code == 201
    rid = session.query(CatMergeRequest).first().id

    keeping_the_real_one = _approve(client, admin, rid, real.id)
    assert keeping_the_real_one.status_code == 409
    assert "demo content" in keeping_the_real_one.json()["detail"]

    assert _approve(client, admin, rid, demo.id).status_code == 409
    # Nothing was half-done on the way to being refused.
    assert session.query(Cat).count() == 2
    assert session.query(Sighting).filter(Sighting.cat_id == real.id).count() == 1


def test_a_normal_user_cannot_even_report_a_demo_cat(client):
    from app.services.demo_seed import demo_features

    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    real = _make_cat(session, "Mittens")
    demo = _make_cat(session, "Seeded", features_json=demo_features("cat:willow"))

    assert _file(client, user, real.id, demo.id).status_code == 404


# ---------------------------------------------------------------------------
# Telling the cat's owner
#
# Claiming is the one standing per-cat subscription in the app, so a merge that
# moves ownership onto a different record is what that relationship is for.
# ---------------------------------------------------------------------------


def _owner_notes(session, user_id):
    return (
        session.query(Notification)
        .filter(Notification.user_id == user_id, Notification.type == "cat_merged")
        .all()
    )


def test_the_owner_of_the_duplicate_is_told_their_cat_moved(client):
    session = client.Session()
    spotter = _make_user(session, "spotter@example.com")
    owner = _make_user(session, "owner@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    session.add(CatClaim(cat_id=dupe.id, user_id=owner.id, status="verified"))
    session.commit()
    _file(client, spotter, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    notes = _owner_notes(session, owner.id)
    assert len(notes) == 1
    assert "Socks" in notes[0].title and "Mittens" in notes[0].title
    assert "still own" in notes[0].body
    # Deep-links to the cat that still exists.
    assert notes[0].cat_id == keep.id


def test_the_owner_of_the_survivor_is_told_it_absorbed_one(client):
    session = client.Session()
    spotter = _make_user(session, "spotter@example.com")
    owner = _make_user(session, "owner@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    session.add(CatClaim(cat_id=keep.id, user_id=owner.id, status="verified"))
    session.commit()
    _file(client, spotter, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    notes = _owner_notes(session, owner.id)
    assert len(notes) == 1
    # Their cat didn't move; it grew. The wording has to say which happened.
    assert "still own" not in notes[0].body
    assert "moved onto Mittens" in notes[0].body


def test_an_owner_who_filed_the_report_is_told_once(client):
    """Owning the cat you reported shouldn't mean two pushes for one event."""
    session = client.Session()
    owner = _make_user(session, "owner@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    session.add(CatClaim(cat_id=dupe.id, user_id=owner.id, status="verified"))
    session.commit()
    _file(client, owner, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    assert _owner_notes(session, owner.id) == []
    decided = (
        session.query(Notification)
        .filter(
            Notification.user_id == owner.id,
            Notification.type == "merge_request_merged",
        )
        .all()
    )
    assert len(decided) == 1


def test_an_unowned_merge_tells_nobody_extra(client):
    session = client.Session()
    spotter = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _file(client, spotter, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    _approve(client, admin, rid, keep.id)

    assert (
        session.query(Notification).filter(Notification.type == "cat_merged").count() == 0
    )


def test_a_rejected_report_tells_the_owner_nothing(client):
    """Nothing happened to their cat, so there is nothing to say."""
    session = client.Session()
    spotter = _make_user(session, "spotter@example.com")
    owner = _make_user(session, "owner@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    session.add(CatClaim(cat_id=dupe.id, user_id=owner.id, status="verified"))
    session.commit()
    _file(client, spotter, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    client.post(
        f"/moderation/merges/{rid}/reject", json={"reason": "Different cats."},
        headers=_auth(admin),
    )

    assert _owner_notes(session, owner.id) == []


# ---------------------------------------------------------------------------
# What it looks like to the people who photographed the duplicate
# ---------------------------------------------------------------------------


def _spotter_sighting(session, cat, user, days_ago, photo):
    s = Sighting(
        cat_id=cat.id,
        user_id=user.id,
        photo_path=photo,
        latitude=51.5,
        longitude=-0.12,
        spotted_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
    )
    session.add(s)
    session.commit()
    return s


def test_everyone_who_photographed_the_duplicate_keeps_their_card(client):
    """A spotter's Cat-a-log is derived from their own sightings, so it follows.

    Someone whose only photo was of the duplicate must not lose the cat from
    their collection, and someone who had both must end up with one card rather
    than a live one beside a dead one.
    """
    session = client.Session()
    both = _make_user(session, "both@example.com")
    only_dupe = _make_user(session, "onlydupe@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")

    _spotter_sighting(session, keep, both, days_ago=8, photo="uploads/both-keep.jpg")
    _spotter_sighting(session, dupe, both, days_ago=4, photo="uploads/both-dupe.jpg")
    _spotter_sighting(session, dupe, only_dupe, days_ago=2, photo="uploads/only-dupe.jpg")

    assert len(client.get("/cats/mine", headers=_auth(both)).json()) == 2
    assert len(client.get("/cats/mine", headers=_auth(only_dupe)).json()) == 1

    _file(client, both, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id
    assert _approve(client, admin, rid, keep.id).status_code == 200

    # Two cards collapse into one, and it's the survivor.
    both_cats = client.get("/cats/mine", headers=_auth(both)).json()
    assert [c["id"] for c in both_cats] == [keep.id]

    # The person who only ever saw the duplicate keeps the cat, under its new name.
    only_cats = client.get("/cats/mine", headers=_auth(only_dupe)).json()
    assert [c["id"] for c in only_cats] == [keep.id]
    assert only_cats[0]["name"] == "Mittens"


def test_a_chosen_cover_photo_survives_the_merge(client):
    """The highlighted polaroid is a key into the spotter's own sightings.

    Those sightings move to the survivor, so the key stays valid — but only if
    the layout is rewritten to point at the survivor. Left alone, the card
    silently falls back to whatever they photographed most recently.
    """
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")

    # Their favourite shot is an older one of the duplicate, so a fallback to
    # "most recent" would visibly pick the wrong photo.
    favourite = _spotter_sighting(
        session, dupe, user, days_ago=30, photo="uploads/the-good-one.jpg"
    )
    _spotter_sighting(session, keep, user, days_ago=1, photo="uploads/a-blurry-one.jpg")

    user.catalog_layout = json.dumps(
        {
            "order": [dupe.id, keep.id],
            "covers": {str(dupe.id): favourite.photo_path},
            "frames": {str(dupe.id): "gold"},
            "adjusts": {str(dupe.id): {"scale": 1.4, "x": 3.0, "y": -2.0}},
        }
    )
    session.commit()

    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id
    _approve(client, admin, rid, keep.id)

    card = client.get("/cats/mine", headers=_auth(user)).json()[0]
    assert card["id"] == keep.id
    # Still their chosen polaroid, not the blurry recent one.
    assert card["last_photo_path"].endswith("the-good-one.jpg")

    session.refresh(user)
    layout = json.loads(user.catalog_layout)
    assert layout["order"] == [keep.id]
    assert layout["frames"] == {str(keep.id): "gold"}
    assert layout["adjusts"][str(keep.id)]["scale"] == 1.4


def test_reseeding_the_demo_neighbourhood_survives_requests_filed_on_it(strict_client):
    """The demo purge runs at every startup, so this one takes the app down.

    A trait suggestion or duplicate report filed on a seeded cat outlives that
    cat: _purge_seeded_content deletes the whole neighbourhood and lays it down
    again. trait_change_requests.cat_id is NOT NULL, so with foreign keys
    enforced the purge raises and the service never finishes booting.
    """
    from app.services.demo_seed import _purge_seeded_content, demo_features

    client = strict_client
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    real = _make_cat(session, "Mittens")
    demo = _make_cat(session, "Seeded", features_json=demo_features("cat:willow"))
    session.add(
        TraitChangeRequest(
            cat_id=demo.id,
            user_id=admin.id,
            status="pending",
            proposed_json=json.dumps({"pattern": "tabby"}),
        )
    )
    session.commit()
    assert _file(client, admin, real.id, demo.id).status_code == 201

    _purge_seeded_content(session)
    session.commit()

    assert session.query(Cat).filter(Cat.id == demo.id).first() is None
    assert session.query(TraitChangeRequest).count() == 0
    report = session.query(CatMergeRequest).first()
    session.refresh(report)
    assert report.status == "superseded"
    assert demo.id not in (report.cat_a_id, report.cat_b_id)
    # The real cat is untouched by the neighbourhood being rebuilt.
    assert session.query(Cat).filter(Cat.id == real.id).first() is not None


def test_a_merge_survives_enforced_foreign_keys(strict_client):
    """The bug, reproduced where it actually bites.

    Before the cleanup this raised IntegrityError and the merge failed outright,
    so any cat that had ever been suggested a correction was unmergeable.
    """
    client = strict_client
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    keep, dupe = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _add_sighting(session, dupe, days_ago=2)
    session.add(
        TraitChangeRequest(
            cat_id=dupe.id,
            user_id=user.id,
            status="pending",
            proposed_json=json.dumps({"primary_color": "ginger"}),
        )
    )
    session.add(
        Notification(
            user_id=user.id, type="sighting", title="t", body="b", cat_id=dupe.id
        )
    )
    session.commit()
    _file(client, user, keep.id, dupe.id)
    rid = session.query(CatMergeRequest).first().id

    assert _approve(client, admin, rid, keep.id).status_code == 200
    assert session.query(Cat).filter(Cat.id == dupe.id).first() is None


def test_deleting_a_cat_survives_enforced_foreign_keys(strict_client):
    from app.services.content_deletion import delete_cat

    client = strict_client
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    session.add(
        TraitChangeRequest(
            cat_id=a.id,
            user_id=user.id,
            status="pending",
            proposed_json=json.dumps({"pattern": "tabby"}),
        )
    )
    session.commit()
    _file(client, user, a.id, b.id)

    delete_cat(session, session.query(Cat).filter(Cat.id == a.id).first())
    session.commit()

    assert session.query(Cat).filter(Cat.id == a.id).first() is None


def test_a_departing_moderators_decisions_stand(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a, b = _make_cat(session, "Mittens"), _make_cat(session, "Socks")
    _file(client, user, a.id, b.id)
    rid = session.query(CatMergeRequest).first().id
    client.post(f"/moderation/merges/{rid}/reject", json={"reason": "No."}, headers=_auth(admin))

    assert client.delete("/auth/me", headers=_auth(admin)).status_code == 204

    row = session.query(CatMergeRequest).filter(CatMergeRequest.id == rid).first()
    session.refresh(row)
    assert row.status == "rejected"
    assert row.reviewed_by_id is None
