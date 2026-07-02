"""Tests for the security-hardening fixes from the audit:

  * SECRET_KEY fail-fast on a real (non-SQLite) database;
  * OAuth audience verification (Apple/Google token-substitution defence);
  * verification / reset code brute-force lockout;
  * admin gate on catalog-maintenance endpoints;
  * CORS no longer pairs a wildcard origin with credentials;
  * /uploads path traversal returns 404, not 500.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import app.models  # noqa: F401
from app.db.session import Base, get_db
from app.main import app, _require_secure_config
from app.models.user import User
from app.services import auth_service
from app.services.auth_service import (
    _check_audience,
    create_access_token,
    hash_password,
)


# --- C1: SECRET_KEY fail-fast ----------------------------------------------

def test_secure_config_rejects_weak_secret_on_postgres(monkeypatch):
    monkeypatch.setattr("app.main.settings.database_url", "postgresql+psycopg2://x/y")
    monkeypatch.setattr("app.main.settings.secret_key", "change-me")
    with pytest.raises(RuntimeError):
        _require_secure_config()


def test_secure_config_allows_sqlite_with_default_secret(monkeypatch):
    monkeypatch.setattr("app.main.settings.database_url", "sqlite:///./cats.db")
    monkeypatch.setattr("app.main.settings.secret_key", "change-me")
    _require_secure_config()  # must not raise — dev/test stays usable


def test_secure_config_allows_strong_secret_on_postgres(monkeypatch):
    monkeypatch.setattr("app.main.settings.database_url", "postgresql+psycopg2://x/y")
    monkeypatch.setattr("app.main.settings.secret_key", "z" * 40)
    _require_secure_config()


# --- C2/H2: OAuth audience verification ------------------------------------

def test_check_audience_rejects_foreign_aud():
    with pytest.raises(ValueError):
        _check_audience({"aud": "someone-elses-app"}, "our-client-id", "Google")


def test_check_audience_accepts_matching_aud():
    _check_audience({"aud": "our-client-id"}, "our-client-id", "Google")


def test_check_audience_accepts_matching_azp():
    # Google puts the client id in azp for some token types.
    _check_audience({"azp": "our-client-id"}, "our-client-id", "Google")


def test_check_audience_skipped_when_unconfigured():
    # No client id configured (local dev): don't block, but it's logged.
    _check_audience({"aud": "anything"}, "", "Apple")


# --- shared client fixture --------------------------------------------------

@pytest.fixture
def client(monkeypatch):
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

    sent: dict[str, str] = {}
    monkeypatch.setattr(
        "app.routers.auth.send_verification_code",
        lambda to, code: sent.__setitem__(to, code) or True,
    )
    monkeypatch.setattr(
        "app.routers.auth.send_password_reset_code",
        lambda to, code: sent.__setitem__(to, code) or True,
    )

    c = TestClient(app)
    c.sent = sent  # type: ignore[attr-defined]
    c.Session = Session  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def _token(user_id):
    return create_access_token({"sub": str(user_id)})


# --- H3: code brute-force lockout ------------------------------------------

def test_verify_email_locks_out_after_max_attempts(client):
    client.post("/auth/register", json={"email": "v@example.com", "password": "hunter2pw"})
    assert "v@example.com" in client.sent
    real_code = client.sent["v@example.com"]

    # Five wrong guesses exhaust the code.
    for _ in range(5):
        r = client.post("/auth/verify-email", json={"email": "v@example.com", "code": "000000"})
        assert r.status_code == 400

    # The (correct) code is now dead — the row was invalidated.
    r = client.post("/auth/verify-email", json={"email": "v@example.com", "code": real_code})
    assert r.status_code == 400
    assert "request a new" in r.json()["detail"].lower()


def test_reset_code_locks_out_after_max_attempts(client):
    session = client.Session()
    session.add(User(email="r@example.com", hashed_password=hash_password("hunter2pw"), email_verified=True))
    session.commit()
    session.close()

    client.post("/auth/forgot-password", json={"email": "r@example.com"})
    real_code = client.sent["r@example.com"]

    for _ in range(5):
        r = client.post("/auth/verify-code", json={"email": "r@example.com", "code": "000000"})
        assert r.status_code == 400

    r = client.post("/auth/verify-code", json={"email": "r@example.com", "code": real_code})
    assert r.status_code == 400  # code was invalidated, no reset_token issued
    assert "reset_token" not in r.json()


# --- M2: admin gate ---------------------------------------------------------

def _make_user(client, email, is_admin=False):
    session = client.Session()
    u = User(
        email=email,
        hashed_password=hash_password("hunter2pw"),
        email_verified=True,
        is_admin=is_admin,
    )
    session.add(u)
    session.commit()
    uid = u.id
    session.close()
    return uid


def test_recompute_rarity_requires_auth(client):
    assert client.post("/cats/recompute-rarity").status_code in (401, 403)


def test_recompute_rarity_forbidden_for_non_admin(client):
    uid = _make_user(client, "user@example.com", is_admin=False)
    r = client.post("/cats/recompute-rarity", headers={"Authorization": f"Bearer {_token(uid)}"})
    assert r.status_code == 403


def test_recompute_rarity_allowed_for_admin(client):
    uid = _make_user(client, "admin@example.com", is_admin=True)
    r = client.post("/cats/recompute-rarity", headers={"Authorization": f"Bearer {_token(uid)}"})
    assert r.status_code == 200


def test_create_cat_forbidden_for_non_admin(client):
    uid = _make_user(client, "user2@example.com", is_admin=False)
    r = client.post(
        "/cats",
        json={"name": "Sneaky", "breed": None},
        headers={"Authorization": f"Bearer {_token(uid)}"},
    )
    assert r.status_code == 403


# --- M1: CORS ---------------------------------------------------------------

def test_cors_does_not_allow_credentials_with_wildcard():
    from starlette.middleware.cors import CORSMiddleware

    cors = next(
        (m for m in app.user_middleware if m.cls is CORSMiddleware), None
    )
    assert cors is not None
    assert cors.kwargs.get("allow_credentials") is False


# --- L2: path traversal -> 404 ---------------------------------------------

def test_uploads_path_traversal_returns_404(client, tmp_path):
    from app.services.storage import LocalStorage, set_storage

    set_storage(LocalStorage(tmp_path))
    try:
        r = client.get("/uploads/..%2f..%2f..%2fetc%2fpasswd")
        assert r.status_code == 404
    finally:
        set_storage(None)
