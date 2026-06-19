"""Expo push notification sender.

Runs in FastAPI BackgroundTasks (a worker thread), so it uses the sync httpx
client and opens its own DB session. Failures are logged, never raised — a
push must never break the request that triggered it.
"""

from __future__ import annotations

import logging

import httpx

from app.db.session import SessionLocal
from app.models.notification import PushToken

log = logging.getLogger(__name__)

EXPO_PUSH_URL = "https://exp.host/--/api/v2/push/send"
CHUNK_SIZE = 100


def send_expo_push(tokens: list[str], title: str, body: str, data: dict) -> None:
    """POST messages to Expo's push API and prune dead tokens."""
    if not tokens:
        return
    dead_tokens: list[str] = []
    try:
        with httpx.Client(timeout=10.0) as client:
            for i in range(0, len(tokens), CHUNK_SIZE):
                chunk = tokens[i : i + CHUNK_SIZE]
                messages = [
                    {
                        "to": t,
                        "title": title,
                        "body": body,
                        "data": data,
                        "sound": "default",
                        "channelId": "default",
                    }
                    for t in chunk
                ]
                resp = client.post(EXPO_PUSH_URL, json=messages)
                if resp.status_code != 200:
                    log.warning("Expo push returned %s: %s", resp.status_code, resp.text[:500])
                    continue
                tickets = resp.json().get("data", [])
                for token, ticket in zip(chunk, tickets):
                    details = ticket.get("details") or {}
                    if details.get("error") == "DeviceNotRegistered":
                        dead_tokens.append(token)
    except Exception:
        log.exception("Expo push send failed")
        return

    if dead_tokens:
        db = SessionLocal()
        try:
            db.query(PushToken).filter(PushToken.token.in_(dead_tokens)).delete(
                synchronize_session=False
            )
            db.commit()
            log.info("Pruned %d dead push tokens", len(dead_tokens))
        except Exception:
            log.exception("Failed pruning dead push tokens")
        finally:
            db.close()


def push_to_user(user_id: int, title: str, body: str, data: dict) -> None:
    """Send a push to all of a user's registered devices."""
    db = SessionLocal()
    try:
        tokens = [
            row.token
            for row in db.query(PushToken).filter(PushToken.user_id == user_id).all()
        ]
    except Exception:
        log.exception("Failed loading push tokens for user %d", user_id)
        return
    finally:
        db.close()
    send_expo_push(tokens, title, body, data)
