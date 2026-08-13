"""Security headers on every response, and HSTS only where it belongs.

The conditional HSTS is the part worth testing. Sending it unconditionally would
work fine in production and quietly pin localhost to HTTPS in every developer's
browser, so the scheme detection — which has to read X-Forwarded-Proto, because
App Runner terminates TLS before the app sees the request — is the behaviour
these tests pin down.
"""

import pytest
from fastapi.testclient import TestClient

from app.main import app

HSTS = "Strict-Transport-Security"


@pytest.fixture
def client():
    return TestClient(app)


# --- Always present ---------------------------------------------------------

def test_static_headers_are_set(client):
    r = client.get("/health")
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_headers_are_set_on_error_responses_too(client):
    """A 404 is still a response an attacker can elicit."""
    r = client.get("/definitely-not-a-route")
    assert r.status_code == 404
    assert r.headers["X-Content-Type-Options"] == "nosniff"


def test_headers_reach_cors_preflight_responses(client):
    """CORS answers preflight itself and never calls the router, so this only
    holds while the security middleware stays outermost — which is what the
    ordering in main.py is for."""
    r = client.options(
        "/health",
        headers={
            "Origin": "https://catapp.uk",
            "Access-Control-Request-Method": "GET",
            "X-Forwarded-Proto": "https",
        },
    )
    assert r.headers["X-Frame-Options"] == "DENY"
    assert HSTS in r.headers


# --- HSTS, only over TLS ----------------------------------------------------

def test_hsts_sent_when_the_proxy_reports_https(client):
    r = client.get("/health", headers={"X-Forwarded-Proto": "https"})
    assert r.headers[HSTS] == "max-age=31536000; includeSubDomains"


def test_hsts_honours_the_first_hop_in_a_proxy_chain(client):
    r = client.get("/health", headers={"X-Forwarded-Proto": "https, http"})
    assert HSTS in r.headers


def test_no_hsts_over_plain_http(client):
    """Local dev speaks http. An HSTS header here pins the developer's whole
    localhost to HTTPS and breaks their other projects."""
    r = client.get("/health", headers={"X-Forwarded-Proto": "http"})
    assert HSTS not in r.headers


def test_no_hsts_when_the_proxy_says_nothing(client):
    r = client.get("/health")
    assert HSTS not in r.headers


def test_hsts_does_not_advertise_preload(client):
    """Preload is a one-way commitment shipped in browser binaries — it should
    never arrive as a side effect of this middleware."""
    r = client.get("/health", headers={"X-Forwarded-Proto": "https"})
    assert "preload" not in r.headers[HSTS]
