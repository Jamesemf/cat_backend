"""Annotated string types that resolve a stored media *key* to a fetchable URL.

Apply these to display fields only (feed/profile output). On JSON serialization
the stored key ("uploads/<uuid>.jpg") is passed through ``storage.url()``:
LocalStorage returns it unchanged (frontend prepends API_BASE), S3 returns an
absolute URL. Handshake/input fields (the analyze->commit echo token) keep the
raw ``str`` type so the client round-trips the key, not the URL.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated

from pydantic import PlainSerializer

from app.services.storage import get_storage


def _iso_utc(value: datetime | None) -> str | None:
    """Serialize a datetime as ISO-8601 that always carries a UTC offset.

    Our datetime columns store naive UTC, so FastAPI would otherwise emit
    ``"2026-07-03T09:00:00"`` with no zone — which JS ``new Date()`` parses as
    *local* time, throwing off every relative-time/date display on the client.
    Stamp naive values as UTC so the offset is explicit.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# Apply to every datetime field that goes out in a response so the client always
# receives a zoned timestamp. when_used="json" keeps python-mode model_dump()
# returning real datetimes for internal use.
UtcDatetime = Annotated[datetime, PlainSerializer(_iso_utc, when_used="json")]
UtcDatetimeOpt = Annotated[datetime | None, PlainSerializer(_iso_utc, when_used="json")]


def _resolve(value: str | None) -> str | None:
    return get_storage().url(value) if value else value


def _resolve_list(values: list[str] | None) -> list[str]:
    storage = get_storage()
    return [storage.url(v) if v else v for v in (values or [])]


# when_used="json": resolve only when FastAPI serializes a response, never on
# internal model_dump() in python mode (which compares against raw keys).
# return_type is left to inference so the optional variant keeps str | None.
MediaUrl = Annotated[str, PlainSerializer(_resolve, when_used="json")]
MediaUrlOpt = Annotated[str | None, PlainSerializer(_resolve, when_used="json")]
MediaUrlList = Annotated[list[str], PlainSerializer(_resolve_list, when_used="json")]
