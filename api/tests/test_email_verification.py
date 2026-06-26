"""Registration email-verification flow: register emails a code, /verify-email
confirms it, and the user's email_verified flag flips."""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401 — registers every model on Base before create_all
from app.db.session import Base, get_db
from app.main import app


@pytest.fixture
def client(monkeypatch):
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,  # share one in-memory DB across all connections
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

    # Capture the verification code instead of sending a real email.
    sent: dict[str, str] = {}

    def fake_send(to: str, code: str) -> bool:
        sent[to] = code
        return True

    monkeypatch.setattr("app.routers.auth.send_verification_code", fake_send)

    c = TestClient(app)
    c.sent = sent  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _register(client, email="new@example.com", password="hunter2pw"):
    r = client.post("/auth/register", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_register_emails_code_and_protected_routes_gated(client):
    token = _register(client)
    assert "new@example.com" in client.sent  # a code was issued
    # Hard gate: an unverified account can't touch protected endpoints.
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 403
    assert me.json()["detail"] == "email_not_verified"


def test_verify_email_unlocks_protected_routes(client):
    token = _register(client)
    code = client.sent["new@example.com"]

    # Wrong code is rejected.
    bad = client.post("/auth/verify-email", json={"email": "new@example.com", "code": "000000"})
    assert bad.status_code == 400

    # Still gated before verifying.
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).status_code == 403

    # Correct code verifies.
    ok = client.post("/auth/verify-email", json={"email": "new@example.com", "code": code})
    assert ok.status_code == 200
    assert ok.json()["email_verified"] is True

    # Now the same token works.
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email_verified"] is True

    # Code is single-use: a second attempt finds nothing pending.
    again = client.post("/auth/verify-email", json={"email": "new@example.com", "code": code})
    assert again.status_code == 400


def test_login_unverified_is_403_and_resends(client):
    _register(client)
    client.sent.clear()
    r = client.post("/auth/login", json={"email": "new@example.com", "password": "hunter2pw"})
    assert r.status_code == 403
    assert r.json()["detail"] == "email_not_verified"
    # A fresh code was re-sent so the user can complete verification.
    assert "new@example.com" in client.sent


def test_verify_email_unknown_email_400(client):
    r = client.post("/auth/verify-email", json={"email": "nobody@example.com", "code": "123456"})
    assert r.status_code == 400


def test_resend_verification_is_always_200(client):
    _register(client)
    # Re-send issues a fresh code (and stays 200).
    r = client.post("/auth/resend-verification", json={"email": "new@example.com"})
    assert r.status_code == 200
    # Unknown email also 200 (no enumeration).
    r2 = client.post("/auth/resend-verification", json={"email": "nobody@example.com"})
    assert r2.status_code == 200
