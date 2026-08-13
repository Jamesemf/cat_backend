"""Security response headers.

Applied to every response, including the ones CORS generates for preflight.

HSTS is conditional, and deliberately so. App Runner terminates TLS and forwards
plaintext to the container, so ``request.url.scheme`` is the scheme of the
*internal* hop and says nothing about what the client used — ``X-Forwarded-Proto``
is the header that does. Emitting the header only when that says https keeps us
on the right side of RFC 6797 (a server should not send HSTS over a non-secure
transport, and clients must ignore it if received that way), and avoids a
genuinely nasty local footgun: a stray HSTS header served from
``http://localhost:8000`` pins *all* of localhost to HTTPS in the developer's
browser, breaking every other project on the machine until they clear it by hand.

No ``preload`` directive. Preloading is a one-way commitment that ships inside
browser binaries and is slow and awkward to reverse; it should be a deliberate
decision, not a side effect of adding this module.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.types import ASGIApp

# One year, the value the preload list requires and the shortest that browsers
# treat as a serious commitment. api.catapp.uk has no subdomains today, so
# includeSubDomains costs nothing and covers any added later.
HSTS_VALUE = "max-age=31536000; includeSubDomains"

# Sent on every response regardless of scheme.
STATIC_HEADERS = {
    # The API serves JSON and user-uploaded images. Sniffing is how an upload
    # that survived sanitisation gets reinterpreted as something executable.
    "X-Content-Type-Options": "nosniff",
    # Nothing here is meant to be framed. Modern browsers take this from CSP's
    # frame-ancestors instead, but this still covers older ones.
    "X-Frame-Options": "DENY",
    # Don't leak path or query (password-reset and verification codes travel in
    # some of them) to third-party origins.
    "Referrer-Policy": "strict-origin-when-cross-origin",
}


def _is_https(request: Request) -> bool:
    """Whether the *client* reached us over TLS, not the proxy hop."""
    forwarded = request.headers.get("x-forwarded-proto")
    if forwarded:
        # A proxy chain sends a comma-separated list; the client's is first.
        return forwarded.split(",")[0].strip().lower() == "https"
    return request.url.scheme == "https"


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        for name, value in STATIC_HEADERS.items():
            # setdefault, so a route that has already made a deliberate choice
            # (e.g. media.py pinning nosniff itself) keeps it.
            response.headers.setdefault(name, value)
        if _is_https(request):
            response.headers.setdefault("Strict-Transport-Security", HSTS_VALUE)
        return response


def add_security_headers(app: ASGIApp) -> None:
    app.add_middleware(SecurityHeadersMiddleware)
