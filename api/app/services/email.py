import logging

import httpx

from app.config import settings

log = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def send_email(to: str, subject: str, html: str, text: str | None = None) -> bool:
    """Send a transactional email via Resend. Returns True on success.

    When resend_api_key is unset (local dev / not configured) this no-ops and
    returns False, so callers can fall back to logging.
    """
    if not settings.resend_api_key:
        log.warning("RESEND_API_KEY not set — email to %s not sent (%s)", to, subject)
        return False

    payload: dict = {"from": settings.email_from, "to": [to], "subject": subject, "html": html}
    if text:
        payload["text"] = text

    try:
        resp = httpx.post(
            RESEND_API_URL,
            headers={
                "Authorization": f"Bearer {settings.resend_api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=10.0,
        )
    except httpx.HTTPError:
        log.exception("Resend request failed for %s", to)
        return False

    if resp.status_code >= 400:
        log.error("Resend error %s for %s: %s", resp.status_code, to, resp.text)
        return False
    return True


def send_password_reset_code(to: str, code: str) -> bool:
    """Email a 6-digit password-reset code."""
    subject = "Your Cats password reset code"
    html = f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,sans-serif;background:#faf6f0;padding:32px;">
  <div style="max-width:480px;margin:0 auto;background:#ffffff;border-radius:16px;padding:32px;text-align:center;">
    <h1 style="font-size:20px;font-weight:800;color:#2d2420;margin:0 0 8px;">Reset your password</h1>
    <p style="font-size:15px;line-height:22px;color:#9a8a82;margin:0 0 24px;">
      Use this code to reset your Cats password. It expires in 15 minutes.
    </p>
    <div style="font-size:34px;font-weight:800;letter-spacing:8px;color:#b53920;background:#f4efe6;border-radius:12px;padding:18px 0;">
      {code}
    </div>
    <p style="font-size:13px;line-height:19px;color:#9a8a82;margin:24px 0 0;">
      If you didn't request this, you can safely ignore this email.
    </p>
  </div>
</div>"""
    text = (
        f"Your Cats password reset code is {code}. It expires in 15 minutes. "
        "If you didn't request this, you can ignore this email."
    )
    return send_email(to, subject, html, text)
