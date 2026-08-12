import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.db.session import Base, get_db
from app.main import app
from app.services.demo_seed import DEMO_EMAIL, DEMO_PASSWORD, seed_apple_review_demo


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

    with Session() as db:
        seed_apple_review_demo(db)

    app.dependency_overrides[get_db] = override_get_db
    c = TestClient(app)
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _demo_auth(client):
    resp = client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    assert resp.status_code == 200
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_demo_account_can_log_in(client):
    resp = client.post("/auth/login", json={"email": DEMO_EMAIL, "password": DEMO_PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["access_token"]


def test_demo_cats_are_hidden_from_public_lists(client):
    assert client.get("/cats").json() == []
    assert client.get("/sightings/feed").json() == []
    assert client.get("/explorer/feed").json() == []

    stats = client.get("/cats/stats").json()
    assert stats["total_cats"] == 0
    assert stats["total_sightings"] == 0


def test_demo_account_sees_seeded_content(client):
    headers = _demo_auth(client)

    cats = client.get("/cats", headers=headers).json()
    assert len(cats) >= 6
    assert {c["name"] for c in cats} >= {"Biscuit", "Clementine", "Mochi"}

    feed = client.get("/sightings/feed", headers=headers).json()
    assert len(feed) >= 6
    assert all(not item["hidden"] for item in feed)

    mine = client.get("/cats/mine", headers=headers).json()
    assert len(mine) >= 6

    profile = client.get("/auth/me", headers=headers).json()
    assert profile["email"] == DEMO_EMAIL


def test_demo_cats_can_be_relocated_to_reviewer_position(client):
    headers = _demo_auth(client)
    lat = 40.7128
    lng = -74.0060

    cats = client.get(f"/cats?lat={lat}&lng={lng}", headers=headers).json()

    assert cats
    assert all(abs(c["last_lat"] - lat) < 0.01 for c in cats)
    assert all(abs(c["last_lng"] - lng) < 0.01 for c in cats)


def test_public_cannot_open_demo_cat_detail(client):
    headers = _demo_auth(client)
    demo_cat_id = client.get("/cats", headers=headers).json()[0]["id"]

    assert client.get(f"/cats/{demo_cat_id}").status_code == 404
    assert client.get(f"/cats/{demo_cat_id}", headers=headers).status_code == 200
