import logging
from datetime import datetime, timezone

import httpx

from app.config import settings

log = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"


def render_layout(inner_html: str) -> str:
    """Wrap an email's body in the shared branded shell.

    Every transactional email passes its content card's inner HTML here so the
    header (logo) and footer (company / copyright / links) stay identical across
    all of them. Branding values come from settings so they're configurable per
    environment.
    """
    year = datetime.now(timezone.utc).year
    return f"""\
<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:#faf6f0;padding:32px 16px;">
  <div style="max-width:480px;margin:0 auto;">
    <div style="text-align:center;margin:0 0 20px;">
      <img src="{settings.email_logo_url}" alt="{settings.email_brand_name}" width="60"
           style="display:inline-block;width:60px;height:auto;border:0;outline:none;" />
    </div>
    <div style="background:#ffffff;border-radius:16px;padding:32px;text-align:center;">
      {inner_html}
    </div>
    <div style="text-align:center;padding:24px 8px 0;">
      <p style="font-size:12px;line-height:18px;color:#b3a59c;margin:0 0 6px;">
        <a href="{settings.email_website}" style="color:#b53920;text-decoration:none;">{settings.email_website.replace("https://", "").replace("http://", "")}</a>
        &nbsp;·&nbsp;
        <a href="mailto:{settings.email_support}" style="color:#b53920;text-decoration:none;">{settings.email_support}</a>
      </p>
      <p style="font-size:12px;line-height:18px;color:#b3a59c;margin:0;">
        © {year} {settings.email_company}. All rights reserved.
      </p>
    </div>
  </div>
</div>"""


def _layout_text(body: str) -> str:
    """Plain-text counterpart of render_layout's footer."""
    return (
        f"{body}\n\n"
        f"—\n"
        f"{settings.email_website}  ·  {settings.email_support}\n"
        f"© {datetime.now(timezone.utc).year} {settings.email_company}. All rights reserved."
    )


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
    inner = f"""\
      <h1 style="font-size:20px;font-weight:800;color:#2d2420;margin:0 0 8px;">Reset your password</h1>
      <p style="font-size:15px;line-height:22px;color:#9a8a82;margin:0 0 24px;">
        Use this code to reset your {settings.email_brand_name} password. It expires in 15 minutes.
      </p>
      <div style="font-size:34px;font-weight:800;letter-spacing:8px;color:#b53920;background:#f4efe6;border-radius:12px;padding:18px 0;">
        {code}
      </div>
      <p style="font-size:13px;line-height:19px;color:#9a8a82;margin:24px 0 0;">
        If you didn't request this, you can safely ignore this email.
      </p>"""
    text = _layout_text(
        f"Your {settings.email_brand_name} password reset code is {code}. It expires in 15 minutes. "
        "If you didn't request this, you can ignore this email."
    )
    return send_email(to, subject, render_layout(inner), text)


def send_verification_code(to: str, code: str) -> bool:
    """Email a 6-digit address-verification code for a new registration."""
    subject = f"Verify your {settings.email_brand_name} email"
    inner = f"""\
      <h1 style="font-size:20px;font-weight:800;color:#2d2420;margin:0 0 8px;">Confirm your email</h1>
      <p style="font-size:15px;line-height:22px;color:#9a8a82;margin:0 0 24px;">
        Welcome to {settings.email_brand_name}! Enter this code in the app to verify your
        email address. It expires in 15 minutes.
      </p>
      <div style="font-size:34px;font-weight:800;letter-spacing:8px;color:#b53920;background:#f4efe6;border-radius:12px;padding:18px 0;">
        {code}
      </div>
      <p style="font-size:13px;line-height:19px;color:#9a8a82;margin:24px 0 0;">
        If you didn't create an account, you can safely ignore this email.
      </p>"""
    text = _layout_text(
        f"Welcome to {settings.email_brand_name}! Your email verification code is {code}. "
        "It expires in 15 minutes. If you didn't create an account, you can ignore this email."
    )
    return send_email(to, subject, render_layout(inner), text)
