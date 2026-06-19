"""Annotated string types that resolve a stored media *key* to a fetchable URL.

Apply these to display fields only (feed/profile output). On JSON serialization
the stored key ("uploads/<uuid>.jpg") is passed through ``storage.url()``:
LocalStorage returns it unchanged (frontend prepends API_BASE), S3 returns an
absolute URL. Handshake/input fields (the analyze->commit echo token) keep the
raw ``str`` type so the client round-trips the key, not the URL.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import PlainSerializer

from app.services.storage import get_storage


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
