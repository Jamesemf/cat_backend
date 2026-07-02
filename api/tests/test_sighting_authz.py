"""Authorization + counting behaviour of PATCH /sightings/{id} (assign_cat).

Regression cover for the audit findings:
  * the endpoint used to have no auth dependency at all (anyone could reassign
    any sighting to any cat) — now it requires a token and sighting ownership;
  * cat.sighting_count was computed as `count() + 1`, double-counting the row
    just assigned — now it reflects the true number of linked sightings.
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
from app.models.sighting import Sighting
from app.models.user import User
from app.services.auth_service import create_access_token, hash_password


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


def _make_user(session, email):
    user = User(
        email=email,
        hashed_password=hash_password("hunter2pw"),
        email_verified=True,
        is_active=True,
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _token(user):
    return create_access_token({"sub": str(user.id)})


def _make_cat(session, name="Whiskers"):
    cat = Cat(name=name)
    session.add(cat)
    session.commit()
    session.refresh(cat)
    return cat


def _make_sighting(session, user_id, cat_id=None):
    s = Sighting(
        user_id=user_id,
        cat_id=cat_id,
        photo_path="uploads/x.jpg",
        latitude=51.5,
        longitude=-0.12,
    )
    session.add(s)
    session.commit()
    session.refresh(s)
    return s


def test_assign_cat_requires_auth(client):
    """No Authorization header → 401/403, never an anonymous mutation."""
    session = client.Session()
    owner = _make_user(session, "owner@example.com")
    cat = _make_cat(session)
    sighting = _make_sighting(session, owner.id)
    cat_id, sighting_id = cat.id, sighting.id
    session.close()

    r = client.patch(f"/sightings/{sighting_id}", json={"cat_id": cat_id})
    assert r.status_code in (401, 403), r.text


def test_assign_cat_rejects_non_owner(client):
    """A logged-in user cannot reassign someone else's sighting."""
    session = client.Session()
    owner = _make_user(session, "owner@example.com")
    attacker = _make_user(session, "attacker@example.com")
    cat = _make_cat(session)
    sighting = _make_sighting(session, owner.id)
    cat_id, sighting_id = cat.id, sighting.id
    attacker_token = _token(attacker)
    session.close()

    r = client.patch(
        f"/sightings/{sighting_id}",
        json={"cat_id": cat_id},
        headers={"Authorization": f"Bearer {attacker_token}"},
    )
    assert r.status_code == 403, r.text


def test_assign_cat_owner_succeeds_and_counts_once(client):
    """The owner may reassign; sighting_count reflects reality (no off-by-one)."""
    session = client.Session()
    owner = _make_user(session, "owner@example.com")
    cat = _make_cat(session)
    sighting = _make_sighting(session, owner.id)
    cat_id, sighting_id = cat.id, sighting.id
    owner_token = _token(owner)
    session.close()

    r = client.patch(
        f"/sightings/{sighting_id}",
        json={"cat_id": cat_id},
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    assert r.status_code == 200, r.text

    verify = client.Session()
    refreshed = verify.get(Cat, cat_id)
    # Exactly one sighting is linked to this cat — not two.
    assert refreshed.sighting_count == 1
    verify.close()


def test_commit_ignores_client_spotter_name_and_uses_display_name(client, tmp_path):
    """The public spotter label comes from the auth token's display name, never
    the client-supplied value (spoofable) or the email (privacy leak)."""
    from app.services.storage import LocalStorage, UPLOADS_PREFIX, set_storage

    backend = LocalStorage(tmp_path)
    set_storage(backend)
    try:
        session = client.Session()
        user = User(
            email="spotter@example.com",
            hashed_password=hash_password("hunter2pw"),
            email_verified=True,
            display_name="Cool Spotter",
        )
        session.add(user)
        cat = Cat(name="Existing")  # link to an existing cat so no AI nickname call
        session.add(cat)
        session.commit()
        uid, cat_id = user.id, cat.id
        session.close()

        key = f"{UPLOADS_PREFIX}/test.jpg"
        backend.save(key, b"fake-jpeg-bytes")

        r = client.post(
            "/sightings",
            json={
                "cat_id": cat_id,
                "photo_path": key,
                "latitude": 51.5,
                "longitude": -0.12,
                # Attacker tries to spoof someone else's name / leak an email.
                "spotter_name": "spoofed@victim.com",
                "is_cat": True,
            },
            headers={"Authorization": f"Bearer {create_access_token({'sub': str(uid)})}"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["spotter_name"] == "Cool Spotter"
    finally:
        set_storage(None)
