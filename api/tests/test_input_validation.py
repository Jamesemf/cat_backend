"""Input-validation hardening from the security audit:

  * password strength — register/reset reject too-short passwords (422);
  * coordinate bounds — a sighting commit rejects out-of-range lat/lng (422);
  * list-endpoint limit clamping — an absurd `limit` can't force the API to
    materialise an unbounded result set.
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


# --- Password strength (M3) -------------------------------------------------

def test_register_rejects_short_password(client):
    r = client.post("/auth/register", json={"email": "a@example.com", "password": "short"})
    assert r.status_code == 422, r.text


def test_register_accepts_valid_password(client):
    r = client.post("/auth/register", json={"email": "b@example.com", "password": "hunter2pw"})
    assert r.status_code == 200, r.text


def test_reset_password_rejects_short_password(client):
    # An invalid reset_token would fail later, but validation runs first, so a
    # too-short new_password is a 422 regardless of the token.
    r = client.post(
        "/auth/reset-password",
        json={"reset_token": "whatever", "new_password": "abc"},
    )
    assert r.status_code == 422, r.text


# --- Coordinate bounds (L3) -------------------------------------------------

def test_commit_rejects_out_of_range_coordinates(client):
    body = {"photo_path": "uploads/x.jpg", "latitude": 999.0, "longitude": 0.0}
    r = client.post("/sightings", json=body)
    # Rejected at validation (422) — never reaches auth or persistence.
    assert r.status_code == 422, r.text


# --- List limit clamping (M4) ----------------------------------------------

def test_list_cats_clamps_absurd_limit(client):
    session = client.Session()
    for i in range(5):
        session.add(Cat(name=f"cat-{i}"))
    session.commit()
    session.close()

    r = client.get("/cats", params={"limit": 10_000_000})
    assert r.status_code == 200, r.text
    # Clamp caps the response; with 5 rows we simply get all 5 back, and the
    # request does not error trying to honour an unbounded limit.
    assert len(r.json()) == 5


def test_list_sightings_clamps_absurd_limit(client):
    r = client.get("/sightings", params={"limit": 10_000_000, "offset": -5})
    assert r.status_code == 200, r.text
    assert isinstance(r.json(), list)
