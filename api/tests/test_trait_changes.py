"""Trait corrections: a suggestion changes nothing until a moderator applies it.

The negative space matters as much as the happy path here. A pending request
must leave the cat untouched, a moderator must be able to overrule a half-right
suggestion rather than take it whole, and the values a requester may propose
must be the same closed vocabularies vision itself writes — with breed the
deliberate exception, because live cats carry breed values from two different
lists and strict validation would make them unsubmittable.
"""

import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registers every model on Base before create_all
from app.db.session import Base, get_db
from app.main import app
from app.models.cat import Cat
from app.models.claim import CatClaim
from app.models.notification import Notification
from app.models.trait_change import TraitChangeRequest
from app.models.user import User
from app.routers.trait_changes import MAX_TRAIT_REQUESTS_PER_DAY
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


# Deliberately wrong on colour and pattern — that's what there is to correct.
FEATURES = {
    "primary_color": "gray",
    "secondary_color": "white",
    "pattern": "solid",
    "fur_length": "short",
    "eye_color": "green",
    "body_size": "medium",
    # Not in the app's breed list. Vision writes these, and they must survive.
    "breed": "Orange Tabby",
}


def _make_cat(session, name="Mittens", **overrides):
    cat = Cat(name=name, last_photo_path="uploads/cat.jpg", **{**FEATURES, **overrides})
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def _file(client, user, cat_id, proposed, note=None):
    return client.post(
        f"/cats/{cat_id}/trait-change",
        json={"proposed": proposed, "note": note},
        headers=_auth(user),
    )


# ---------------------------------------------------------------------------
# Filing a request
# ---------------------------------------------------------------------------


def test_a_pending_request_changes_nothing_about_the_cat(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    cat = _make_cat(session)

    res = _file(client, user, cat.id, {"primary_color": "orange", "pattern": "tabby"})
    assert res.status_code == 201
    assert res.json()["status"] == "pending"

    fresh = session.query(Cat).filter(Cat.id == cat.id).first()
    assert fresh.primary_color == "gray"
    assert fresh.pattern == "solid"


def test_only_the_changed_fields_are_stored(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    cat = _make_cat(session)

    # fur_length already matches, so it isn't a change and shouldn't be recorded.
    _file(
        client,
        user,
        cat.id,
        {"primary_color": "orange", "fur_length": "short"},
    )

    row = session.query(TraitChangeRequest).first()
    assert json.loads(row.proposed_json) == {"primary_color": "orange"}


def test_an_explicit_null_is_kept_as_a_proposal_to_clear(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    cat = _make_cat(session)

    res = _file(client, user, cat.id, {"secondary_color": None})
    assert res.status_code == 201

    row = session.query(TraitChangeRequest).first()
    assert json.loads(row.proposed_json) == {"secondary_color": None}


def test_filing_notifies_the_requester_without_pushing(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    cat = _make_cat(session)

    _file(client, user, cat.id, {"primary_color": "orange"})

    note = session.query(Notification).filter(Notification.user_id == user.id).first()
    assert note.type == "trait_change_pending"
    assert note.cat_id == cat.id


def test_a_value_outside_its_vocabulary_is_refused(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    cat = _make_cat(session)

    res = _file(client, user, cat.id, {"primary_color": "chartreuse"})
    assert res.status_code == 400
    assert session.query(TraitChangeRequest).count() == 0


def test_an_unknown_field_is_refused(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    cat = _make_cat(session)

    res = _file(client, user, cat.id, {"rarity_score": "legendary"})
    assert res.status_code == 400
    assert "not a trait" in res.json()["detail"]


def test_breed_accepts_a_value_in_neither_list(client):
    """Cats carry breeds from both the app's list and vision's. Both must submit."""
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    cat = _make_cat(session)

    res = _file(client, user, cat.id, {"breed": "Domestic Shorthair"})
    assert res.status_code == 201


def test_a_no_op_proposal_is_refused(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    cat = _make_cat(session)

    res = _file(client, user, cat.id, {"primary_color": "gray"})
    assert res.status_code == 400
    assert "already" in res.json()["detail"].lower()


def test_an_empty_proposal_is_refused(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    cat = _make_cat(session)

    assert _file(client, user, cat.id, {}).status_code == 400


def test_a_missing_cat_is_a_404(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")

    assert _file(client, user, 999, {"primary_color": "orange"}).status_code == 404


def test_a_second_request_while_one_is_pending_is_refused(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    cat = _make_cat(session)

    assert _file(client, user, cat.id, {"primary_color": "orange"}).status_code == 201
    second = _file(client, user, cat.id, {"pattern": "tabby"})
    assert second.status_code == 409


def test_someone_else_may_still_file_on_the_same_cat(client):
    session = client.Session()
    one = _make_user(session, "one@example.com")
    two = _make_user(session, "two@example.com")
    cat = _make_cat(session)

    assert _file(client, one, cat.id, {"primary_color": "orange"}).status_code == 201
    assert _file(client, two, cat.id, {"primary_color": "orange"}).status_code == 201


def test_the_daily_cap_is_enforced(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")

    # A fresh cat each time, so the one-pending-per-cat guard isn't what bites.
    for i in range(MAX_TRAIT_REQUESTS_PER_DAY):
        cat = _make_cat(session, name=f"Cat {i}")
        assert _file(client, user, cat.id, {"primary_color": "orange"}).status_code == 201

    one_too_many = _make_cat(session, name="Straw")
    res = _file(client, user, one_too_many.id, {"primary_color": "orange"})
    assert res.status_code == 429


def test_filing_requires_a_signed_in_account(client):
    session = client.Session()
    cat = _make_cat(session)

    res = client.post(
        f"/cats/{cat.id}/trait-change", json={"proposed": {"primary_color": "orange"}}
    )
    assert res.status_code in (401, 403)


def test_mine_reports_the_callers_own_request(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    other = _make_user(session, "other@example.com")
    cat = _make_cat(session)

    _file(client, user, cat.id, {"primary_color": "orange"})

    mine = client.get(f"/cats/{cat.id}/trait-change/mine", headers=_auth(user))
    assert mine.json()["status"] == "pending"

    theirs = client.get(f"/cats/{cat.id}/trait-change/mine", headers=_auth(other))
    assert theirs.json() is None


# ---------------------------------------------------------------------------
# The moderator queue
# ---------------------------------------------------------------------------


def test_the_queue_is_admin_only(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    cat = _make_cat(session)
    _file(client, user, cat.id, {"primary_color": "orange"})
    request_id = session.query(TraitChangeRequest).first().id

    assert client.get("/moderation/trait-changes", headers=_auth(user)).status_code == 403
    assert (
        client.post(
            f"/moderation/trait-changes/{request_id}/apply",
            json={"values": {"primary_color": "orange"}},
            headers=_auth(user),
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/moderation/trait-changes/{request_id}/reject",
            json={"reason": "no"},
            headers=_auth(user),
        ).status_code
        == 403
    )


def test_the_queue_shows_current_beside_proposed(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    cat = _make_cat(session)
    _file(client, user, cat.id, {"primary_color": "orange"}, note="It's ginger.")

    row = client.get("/moderation/trait-changes", headers=_auth(admin)).json()[0]
    assert row["current"]["primary_color"] == "gray"
    assert row["proposed"] == {"primary_color": "orange"}
    assert row["note"] == "It's ginger."
    assert row["is_owner"] is False
    assert row["applied"] is None


def test_a_request_from_the_verified_owner_is_badged(client):
    session = client.Session()
    owner = _make_user(session, "owner@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    cat = _make_cat(session)
    session.add(CatClaim(cat_id=cat.id, user_id=owner.id, status="verified"))
    session.commit()

    _file(client, owner, cat.id, {"primary_color": "orange"})

    row = client.get("/moderation/trait-changes", headers=_auth(admin)).json()[0]
    assert row["is_owner"] is True


def test_resolved_requests_are_hidden_until_asked_for(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    cat = _make_cat(session)
    _file(client, user, cat.id, {"primary_color": "orange"})
    request_id = session.query(TraitChangeRequest).first().id

    client.post(
        f"/moderation/trait-changes/{request_id}/reject",
        json={"reason": "Looks grey to me."},
        headers=_auth(admin),
    )

    assert client.get("/moderation/trait-changes", headers=_auth(admin)).json() == []
    everything = client.get(
        "/moderation/trait-changes?include_resolved=true", headers=_auth(admin)
    ).json()
    assert len(everything) == 1
    assert everything[0]["status"] == "rejected"


# ---------------------------------------------------------------------------
# Deciding
# ---------------------------------------------------------------------------


def test_apply_writes_the_moderators_values_not_the_proposal(client):
    """The whole point of the editable form: a half-right suggestion is fixable."""
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    cat = _make_cat(session)
    _file(client, user, cat.id, {"primary_color": "orange", "pattern": "calico"})
    request_id = session.query(TraitChangeRequest).first().id

    # The moderator agrees about the colour but not about calico.
    res = client.post(
        f"/moderation/trait-changes/{request_id}/apply",
        json={"values": {"primary_color": "orange", "pattern": "tabby"}},
        headers=_auth(admin),
    )
    assert res.status_code == 200

    session.expire_all()
    fresh = session.query(Cat).filter(Cat.id == cat.id).first()
    assert fresh.primary_color == "orange"
    assert fresh.pattern == "tabby"

    row = session.query(TraitChangeRequest).filter_by(id=request_id).first()
    assert row.status == "applied"
    assert row.reviewed_by_id == admin.id
    assert json.loads(row.applied_json) == {
        "primary_color": "orange",
        "pattern": "tabby",
    }


def test_apply_leaves_untouched_fields_alone(client):
    """Opening the form must never silently rewrite a breed nobody edited."""
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    cat = _make_cat(session)
    _file(client, user, cat.id, {"pattern": "tabby"})
    request_id = session.query(TraitChangeRequest).first().id

    client.post(
        f"/moderation/trait-changes/{request_id}/apply",
        json={"values": {"pattern": "tabby"}},
        headers=_auth(admin),
    )

    session.expire_all()
    fresh = session.query(Cat).filter(Cat.id == cat.id).first()
    assert fresh.breed == "Orange Tabby"
    assert fresh.primary_color == "gray"


def test_apply_can_clear_a_trait(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    cat = _make_cat(session)
    _file(client, user, cat.id, {"secondary_color": None})
    request_id = session.query(TraitChangeRequest).first().id

    client.post(
        f"/moderation/trait-changes/{request_id}/apply",
        json={"values": {"secondary_color": None}},
        headers=_auth(admin),
    )

    session.expire_all()
    assert session.query(Cat).filter(Cat.id == cat.id).first().secondary_color is None


def test_apply_refuses_a_value_outside_the_vocabulary(client):
    """A moderator can't write something the app itself would have refused."""
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    cat = _make_cat(session)
    _file(client, user, cat.id, {"primary_color": "orange"})
    request_id = session.query(TraitChangeRequest).first().id

    res = client.post(
        f"/moderation/trait-changes/{request_id}/apply",
        json={"values": {"primary_color": "chartreuse"}},
        headers=_auth(admin),
    )
    assert res.status_code == 400

    session.expire_all()
    assert session.query(Cat).filter(Cat.id == cat.id).first().primary_color == "gray"


def test_apply_notifies_the_requester(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    cat = _make_cat(session)
    _file(client, user, cat.id, {"primary_color": "orange"})
    request_id = session.query(TraitChangeRequest).first().id

    client.post(
        f"/moderation/trait-changes/{request_id}/apply",
        json={"values": {"primary_color": "orange"}},
        headers=_auth(admin),
    )

    types = [
        n.type
        for n in session.query(Notification).filter(Notification.user_id == user.id).all()
    ]
    assert "trait_change_applied" in types


def test_deciding_twice_is_refused(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    cat = _make_cat(session)
    _file(client, user, cat.id, {"primary_color": "orange"})
    request_id = session.query(TraitChangeRequest).first().id

    first = client.post(
        f"/moderation/trait-changes/{request_id}/apply",
        json={"values": {"primary_color": "orange"}},
        headers=_auth(admin),
    )
    assert first.status_code == 200

    second = client.post(
        f"/moderation/trait-changes/{request_id}/apply",
        json={"values": {"pattern": "tabby"}},
        headers=_auth(admin),
    )
    assert second.status_code == 409

    rejected_after = client.post(
        f"/moderation/trait-changes/{request_id}/reject",
        json={"reason": "changed my mind"},
        headers=_auth(admin),
    )
    assert rejected_after.status_code == 409


def test_rejecting_leaves_the_cat_alone_and_explains_why(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    cat = _make_cat(session)
    _file(client, user, cat.id, {"primary_color": "orange"})
    request_id = session.query(TraitChangeRequest).first().id

    client.post(
        f"/moderation/trait-changes/{request_id}/reject",
        json={"reason": "The photos show a grey cat."},
        headers=_auth(admin),
    )

    session.expire_all()
    assert session.query(Cat).filter(Cat.id == cat.id).first().primary_color == "gray"

    note = (
        session.query(Notification)
        .filter(Notification.type == "trait_change_rejected")
        .first()
    )
    assert note.body == "The photos show a grey cat."
    assert note.user_id == user.id


def test_rejecting_without_a_reason_still_says_something(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    cat = _make_cat(session)
    _file(client, user, cat.id, {"primary_color": "orange"})
    request_id = session.query(TraitChangeRequest).first().id

    client.post(
        f"/moderation/trait-changes/{request_id}/reject",
        json={"reason": "   "},
        headers=_auth(admin),
    )

    note = (
        session.query(Notification)
        .filter(Notification.type == "trait_change_rejected")
        .first()
    )
    assert note.body.strip()


def test_a_decision_frees_the_user_to_file_again(client):
    session = client.Session()
    user = _make_user(session, "spotter@example.com")
    admin = _make_user(session, "mod@example.com", is_admin=True)
    cat = _make_cat(session)
    _file(client, user, cat.id, {"primary_color": "orange"})
    request_id = session.query(TraitChangeRequest).first().id

    client.post(
        f"/moderation/trait-changes/{request_id}/reject",
        json={"reason": "Not this time."},
        headers=_auth(admin),
    )

    assert _file(client, user, cat.id, {"pattern": "tabby"}).status_code == 201
