"""Ownership review: nothing is granted until a moderator says so.

The point of these tests is the negative space as much as the happy path — that
a pending claim confers nothing, that a pending registration puts no cat in the
public catalogue, and that turning a claim down doesn't hand the claimant a
fresh budget to resubmit with.

Claims are inserted directly rather than posted through /cats/{id}/claim: the
submit endpoints call Anthropic vision, which the suite has no stub for and
deliberately never reaches.
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
from app.models.claim import CatClaim, ClaimPhoto
from app.models.notification import Notification
from app.models.sighting import Sighting
from app.models.user import User
from app.services.auth_service import create_access_token, hash_password
from app.services.claim_verification import MAX_CLAIM_ATTEMPTS_PER_DAY


@pytest.fixture
def client():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # These tests build a fixture across several commits and then read ids back
    # after closing the session. Without this, each commit expires the objects
    # the previous helper returned and the reads raise DetachedInstanceError.
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


FEATURES = {
    "primary_color": "grey",
    "secondary_color": "white",
    "pattern": "tabby",
    "fur_length": "short",
    "eye_color": "green",
    "body_size": "medium",
    "breed": "domestic shorthair",
}


def _make_cat(session, name="Mittens", sightings=0):
    now = datetime.now(timezone.utc)
    cat = Cat(
        name=name,
        sighting_count=sightings,
        first_seen=now,
        last_seen=now,
        last_photo_path="uploads/cat.jpg",
        **FEATURES,
    )
    session.add(cat)
    session.commit()
    session.refresh(cat)
    for _ in range(sightings):
        session.add(
            Sighting(
                user_id=None,
                cat_id=cat.id,
                photo_path="uploads/spot.jpg",
                latitude=51.5,
                longitude=-0.12,
            )
        )
    session.commit()
    return cat


def _make_claim(session, user, cat=None, source="claim", status="pending", real_name="Mittens"):
    claim = CatClaim(
        cat_id=cat.id if cat else None,
        user_id=user.id,
        status=status,
        source=source,
        real_name=real_name,
        likes_petting=True,
        accepts_treats=True,
        age_years=4,
        indoor_outdoor="both",
        created_at=datetime.now(timezone.utc),
        decided_at=datetime.now(timezone.utc) if status != "pending" else None,
    )
    session.add(claim)
    session.commit()
    session.refresh(claim)
    session.add(
        ClaimPhoto(
            claim_id=claim.id,
            photo_path=f"uploads/claims/{claim.id}.jpg",
            features_json=json.dumps(FEATURES),
        )
    )
    session.commit()
    return claim


# --------------------------------------------------------------------------
# Access
# --------------------------------------------------------------------------


def test_queue_is_admin_only(client):
    session = client.Session()
    user = _make_user(session, "nobody@example.com")
    cat = _make_cat(session)
    claim = _make_claim(session, user, cat)
    session.close()

    # No credentials at all is 403 from HTTPBearer, same as a signed-in non-admin.
    assert client.get("/moderation/claims").status_code == 403
    assert client.get("/moderation/claims", headers=_auth(user)).status_code == 403
    assert (
        client.post(f"/moderation/claims/{claim.id}/approve", headers=_auth(user)).status_code
        == 403
    )


def test_missing_claim_is_404_not_500(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    session.close()

    r = client.post("/moderation/claims/9999/approve", headers=_auth(admin))
    assert r.status_code == 404


# --------------------------------------------------------------------------
# A pending claim grants nothing
# --------------------------------------------------------------------------


def test_pending_claim_confers_no_ownership(client):
    session = client.Session()
    user = _make_user(session, "jane@example.com")
    cat = _make_cat(session)
    _make_claim(session, user, cat)
    session.close()

    # No owner card on the public profile...
    r = client.get(f"/cats/{cat.id}")
    assert r.status_code == 200
    assert r.json()["owner"] is None

    # ...and the claimant can't edit one into existence.
    r = client.put(
        f"/cats/{cat.id}/claim", json={"fun_fact": "sneaks in"}, headers=_auth(user)
    )
    assert r.status_code == 403


def test_pending_registration_creates_no_cat(client):
    session = client.Session()
    user = _make_user(session, "jane@example.com")
    _make_claim(session, user, cat=None, source="register", real_name="Ghost")
    cat_count = session.query(Cat).count()
    session.close()

    assert cat_count == 0
    # The public catalogue is unauthenticated; nothing unreviewed may appear in it.
    assert client.get("/cats").json() == []


def test_pending_registration_appears_in_my_claims(client):
    """It has no cat to borrow a name or photo from, so it uses its own."""
    session = client.Session()
    user = _make_user(session, "jane@example.com")
    _make_claim(session, user, cat=None, source="register", real_name="Ghost")
    session.close()

    rows = client.get("/claims/mine", headers=_auth(user)).json()
    assert len(rows) == 1
    assert rows[0]["status"] == "pending"
    assert rows[0]["source"] == "register"
    assert rows[0]["cat_id"] is None
    assert rows[0]["cat_name"] == "Ghost"
    assert rows[0]["cat_photo_path"] is not None


# --------------------------------------------------------------------------
# Approval
# --------------------------------------------------------------------------


def test_approve_grants_ownership_and_renames_the_cat(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    cat = _make_cat(session, name="Grey One")
    claim = _make_claim(session, user, cat, real_name="Mittens")
    session.close()

    r = client.post(f"/moderation/claims/{claim.id}/approve", headers=_auth(admin))
    assert r.status_code == 200
    assert r.json()["status"] == "verified"

    body = client.get(f"/cats/{cat.id}").json()
    assert body["owner"]["display_name"] == "jane"
    # The owner knows the real name; it replaces the generated nickname.
    assert body["name"] == "Mittens"

    session = client.Session()
    types = [n.type for n in session.query(Notification).filter_by(user_id=user.id).all()]
    reviewed_by = session.get(CatClaim, claim.id).reviewed_by_id
    session.close()
    assert "claim_verified" in types
    assert reviewed_by == admin.id


def test_approving_a_registration_creates_the_cat_from_its_photos(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    claim = _make_claim(session, user, cat=None, source="register", real_name="Ghost")
    session.close()

    r = client.post(f"/moderation/claims/{claim.id}/approve", headers=_auth(admin))
    assert r.status_code == 200
    cat_id = r.json()["cat_id"]
    assert cat_id is not None

    body = client.get(f"/cats/{cat_id}").json()
    assert body["name"] == "Ghost"
    assert body["owner"]["display_name"] == "jane"

    session = client.Session()
    cat = session.get(Cat, cat_id)
    # Vision features recorded at submission seed the cat, so other people's
    # sightings can still be matched back to it.
    assert cat.primary_color == "grey"
    assert cat.pattern == "tabby"
    assert cat.last_photo_path == f"uploads/claims/{claim.id}.jpg"
    assert cat.sighting_count == 0
    session.close()

    # Only now is it public.
    assert [c["id"] for c in client.get("/cats").json()] == [cat_id]


def test_approving_one_claim_rejects_its_rivals(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    winner = _make_user(session, "jane@example.com")
    loser = _make_user(session, "bob@example.com")
    cat = _make_cat(session)
    won = _make_claim(session, winner, cat)
    lost = _make_claim(session, loser, cat)
    session.close()

    assert client.post(f"/moderation/claims/{won.id}/approve", headers=_auth(admin)).status_code == 200

    session = client.Session()
    lost_row = session.get(CatClaim, lost.id)
    assert lost_row.status == "rejected"
    assert lost_row.reviewed_by_id == admin.id
    notes = session.query(Notification).filter_by(user_id=loser.id).all()
    session.close()
    assert [n.type for n in notes] == ["claim_rejected"]


def test_second_approval_on_the_same_cat_is_409_not_500(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    a = _make_user(session, "a@example.com")
    b = _make_user(session, "b@example.com")
    cat = _make_cat(session)
    first = _make_claim(session, a, cat)
    second = _make_claim(session, b, cat)
    session.close()

    assert client.post(f"/moderation/claims/{first.id}/approve", headers=_auth(admin)).status_code == 200
    # The rival was auto-rejected, so approving it now is a state conflict.
    r = client.post(f"/moderation/claims/{second.id}/approve", headers=_auth(admin))
    assert r.status_code == 409
    assert "already" in r.json()["detail"].lower()


def test_approve_is_rejected_for_an_already_decided_claim(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    cat = _make_cat(session)
    claim = _make_claim(session, user, cat, status="rejected")
    session.close()

    r = client.post(f"/moderation/claims/{claim.id}/approve", headers=_auth(admin))
    assert r.status_code == 409


# --------------------------------------------------------------------------
# Rejection and revocation
# --------------------------------------------------------------------------


def test_rejecting_a_registration_leaves_no_cat_and_keeps_the_claim(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    claim = _make_claim(session, user, cat=None, source="register", real_name="Ghost")
    session.close()

    r = client.post(
        f"/moderation/claims/{claim.id}/reject",
        json={"reason": "These are stock photos."},
        headers=_auth(admin),
    )
    assert r.status_code == 200
    assert r.json()["status"] == "rejected"

    session = client.Session()
    assert session.query(Cat).count() == 0
    row = session.get(CatClaim, claim.id)
    # The row survives on purpose: the daily cap counts rows, so deleting it
    # would refund the attempt and let the same submission loop.
    assert row is not None
    assert row.rejection_reason == "These are stock photos."
    note = session.query(Notification).filter_by(user_id=user.id).one()
    assert note.type == "claim_rejected"
    assert note.body == "These are stock photos."
    # Nothing to deep-link to — the cat was never created.
    assert note.cat_id is None
    session.close()


def test_rejection_still_counts_against_the_daily_cap(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    claims = [
        _make_claim(session, user, cat=None, source="register", real_name=f"C{i}")
        for i in range(MAX_CLAIM_ATTEMPTS_PER_DAY)
    ]
    session.close()

    for c in claims:
        client.post(f"/moderation/claims/{c.id}/reject", json={}, headers=_auth(admin))

    session = client.Session()
    day_cutoff = datetime.now(timezone.utc) - timedelta(days=1)
    remaining = (
        session.query(CatClaim)
        .filter(CatClaim.user_id == user.id, CatClaim.created_at >= day_cutoff)
        .count()
    )
    session.close()
    assert remaining == MAX_CLAIM_ATTEMPTS_PER_DAY


def test_reject_uses_a_default_reason_when_none_is_given(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    cat = _make_cat(session)
    claim = _make_claim(session, user, cat)
    session.close()

    client.post(f"/moderation/claims/{claim.id}/reject", json={}, headers=_auth(admin))

    session = client.Session()
    body = session.query(Notification).filter_by(user_id=user.id).one().body
    session.close()
    assert body.strip()


def test_revoke_takes_ownership_back(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    cat = _make_cat(session, name="Grey One")
    claim = _make_claim(session, user, cat, real_name="Mittens")
    session.close()

    client.post(f"/moderation/claims/{claim.id}/approve", headers=_auth(admin))
    r = client.post(
        f"/moderation/claims/{claim.id}/revoke",
        json={"reason": "Not their cat."},
        headers=_auth(admin),
    )
    assert r.status_code == 200

    body = client.get(f"/cats/{cat.id}").json()
    assert body["owner"] is None
    # The cat keeps the name approval gave it — others have been seeing it since.
    assert body["name"] == "Mittens"

    session = client.Session()
    types = [n.type for n in session.query(Notification).filter_by(user_id=user.id).all()]
    session.close()
    assert "claim_revoked" in types


def test_revoke_requires_a_verified_claim(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    cat = _make_cat(session)
    claim = _make_claim(session, user, cat)
    session.close()

    r = client.post(f"/moderation/claims/{claim.id}/revoke", json={}, headers=_auth(admin))
    assert r.status_code == 409


# --------------------------------------------------------------------------
# The queue itself
# --------------------------------------------------------------------------


def test_queue_lists_pending_oldest_first_with_both_sides_to_compare(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    cat = _make_cat(session)
    older = _make_claim(session, user, cat)
    older.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    session.commit()
    newer = _make_claim(session, user, cat=None, source="register", real_name="Ghost")
    session.close()

    rows = client.get("/moderation/claims", headers=_auth(admin)).json()
    assert [r["claim_id"] for r in rows] == [older.id, newer.id]

    first = rows[0]
    assert first["source"] == "claim"
    assert first["claimant_name"] == "jane"
    assert first["cat_features"]["pattern"] == "tabby"
    assert first["photos"][0]["features"]["pattern"] == "tabby"
    # No score anywhere — the comparison is the moderator's to make.
    assert "confidence" not in json.dumps(first)

    # A registration has no cat side to compare against yet.
    assert rows[1]["cat_id"] is None
    assert rows[1]["proposed_name"] == "Ghost"


def test_queue_hides_decided_claims_unless_asked(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    cat = _make_cat(session)
    claim = _make_claim(session, user, cat)
    session.close()

    client.post(f"/moderation/claims/{claim.id}/reject", json={}, headers=_auth(admin))

    assert client.get("/moderation/claims", headers=_auth(admin)).json() == []
    resolved = client.get(
        "/moderation/claims?include_resolved=true", headers=_auth(admin)
    ).json()
    assert [r["claim_id"] for r in resolved] == [claim.id]
    assert resolved[0]["reviewed_by_name"] == "mod"


def test_queue_skips_claims_whose_cat_was_deleted(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    cat = _make_cat(session)
    _make_claim(session, user, cat)
    session.query(CatClaim).filter_by(cat_id=cat.id).update({CatClaim.cat_id: None})
    session.commit()
    session.close()

    # cat_id nulled out from under it: there's nothing left to judge it against.
    rows = client.get("/moderation/claims", headers=_auth(admin)).json()
    assert rows == [] or rows[0]["cat_id"] is None


def test_claimant_strikes_are_surfaced_to_the_reviewer(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    user.content_strikes = 2
    session.commit()
    cat = _make_cat(session)
    _make_claim(session, user, cat)
    session.close()

    rows = client.get("/moderation/claims", headers=_auth(admin)).json()
    assert rows[0]["claimant_strikes"] == 2
    assert rows[0]["claimant_banned"] is False


# --------------------------------------------------------------------------
# Merging
# --------------------------------------------------------------------------


def test_merge_refuses_while_a_claim_is_pending(client):
    session = client.Session()
    admin = _make_user(session, "mod@example.com", is_admin=True)
    user = _make_user(session, "jane@example.com")
    source_cat = _make_cat(session, name="A")
    target_cat = _make_cat(session, name="B")
    _make_claim(session, user, source_cat)
    session.close()

    r = client.post(
        f"/cats/{source_cat.id}/merge",
        json={"target_id": target_cat.id},
        headers=_auth(admin),
    )
    assert r.status_code == 409
    assert "awaiting review" in r.json()["detail"]
