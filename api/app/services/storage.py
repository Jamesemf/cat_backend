"""Pluggable media storage: local disk for dev, S3 for prod.

The backend is chosen by ``settings.s3_bucket``:
  * empty  -> :class:`LocalStorage` (writes under ./uploads, served by the API's
    ``/uploads`` route)
  * set    -> :class:`S3Storage` (objects in the bucket, served via a CDN base
    URL or presigned GET URLs)

DB rows only ever store a *storage key* like ``uploads/<uuid>.jpg`` — stable and
backend-independent. ``url()`` turns a key into something a client can GET:
LocalStorage returns the key unchanged (the frontend prepends API_BASE), S3
returns an absolute URL. So the database never knows S3 exists.
"""

from __future__ import annotations

import logging
import mimetypes
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.config import settings

log = logging.getLogger(__name__)

# Every media key lives under this prefix. Kept in the stored key so existing
# rows ("uploads/<uuid>.jpg") and the on-disk layout stay valid unchanged.
UPLOADS_PREFIX = "uploads"


@dataclass(frozen=True)
class StoredObject:
    """One object in a storage backend, as seen by reconciliation."""

    key: str
    size: int
    last_modified: datetime  # tz-aware UTC


def _guess_content_type(key: str) -> str:
    ctype, _ = mimetypes.guess_type(key)
    return ctype or "application/octet-stream"


def new_key(ext: str, *, prefix: str = UPLOADS_PREFIX) -> str:
    """Mint a fresh, collision-free storage key under ``prefix``."""
    if not ext:
        ext = ".jpg"
    elif not ext.startswith("."):
        ext = f".{ext}"
    return f"{prefix}/{uuid.uuid4()}{ext}"


class Storage(ABC):
    """Backend-agnostic media store. Keys are POSIX-style relative paths."""

    @abstractmethod
    def save(self, key: str, data: bytes) -> None: ...

    @abstractmethod
    def delete(self, key: str) -> None: ...

    @abstractmethod
    def exists(self, key: str) -> bool: ...

    @abstractmethod
    def url(self, key: str) -> str:
        """Return a value the client can use to fetch this object."""

    @abstractmethod
    def list_objects(self, prefix: str = UPLOADS_PREFIX) -> Iterator[StoredObject]:
        """Yield every stored object under ``prefix`` (for reconciliation)."""

    def put(self, data: bytes, *, ext: str, prefix: str = UPLOADS_PREFIX) -> str:
        """Store ``data`` under a freshly minted key and return that key."""
        key = new_key(ext, prefix=prefix)
        self.save(key, data)
        return key


class LocalStorage(Storage):
    """Disk-backed store rooted at ``root`` (default: process CWD).

    A key ``uploads/<uuid>.jpg`` maps to ``<root>/uploads/<uuid>.jpg`` — exactly
    the layout the app used before storage was abstracted, so dev data and the
    existing 28 files keep working with no migration.
    """

    def __init__(self, root: Path | str = ".") -> None:
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        # Resolve and confine to root — a malicious key like "../../etc" can't
        # escape the upload tree.
        p = (self.root / key).resolve()
        if self.root not in p.parents and p != self.root:
            raise ValueError(f"Key escapes storage root: {key!r}")
        return p

    def save(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def url(self, key: str) -> str:
        # Relative — the frontend prepends API_BASE and the /uploads route serves
        # the bytes. Unchanged from the pre-S3 behaviour.
        return key

    def list_objects(self, prefix: str = UPLOADS_PREFIX) -> Iterator[StoredObject]:
        base = self.root / prefix
        if not base.exists():
            return
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            st = p.stat()
            yield StoredObject(
                key=p.relative_to(self.root).as_posix(),
                size=st.st_size,
                last_modified=datetime.fromtimestamp(st.st_mtime, tz=timezone.utc),
            )


class S3Storage(Storage):
    """S3-backed store. Credentials come from the standard boto3 chain (IAM role
    on ECS, env vars / profile locally)."""

    def __init__(self, bucket: str, region: str, media_base_url: str = "") -> None:
        import boto3

        self.bucket = bucket
        self.media_base_url = media_base_url.rstrip("/")
        self._client = boto3.client("s3", region_name=region)

    def save(self, key: str, data: bytes) -> None:
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=data,
            ContentType=_guess_content_type(key),
        )

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def url(self, key: str) -> str:
        if self.media_base_url:
            # CloudFront / public bucket domain — clients fetch directly,
            # bypassing the API entirely.
            return f"{self.media_base_url}/{key}"
        # No CDN configured: hand out a short-lived presigned GET URL.
        return self._client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=3600,
        )

    def list_objects(self, prefix: str = UPLOADS_PREFIX) -> Iterator[StoredObject]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield StoredObject(
                    key=obj["Key"],
                    size=obj["Size"],
                    last_modified=obj["LastModified"],  # boto3 returns tz-aware UTC
                )


_storage: Storage | None = None


def get_storage() -> Storage:
    """Return the process-wide storage backend (built once from settings)."""
    global _storage
    if _storage is None:
        if settings.s3_bucket:
            _storage = S3Storage(
                settings.s3_bucket, settings.s3_region, settings.media_base_url
            )
            log.info("Media storage: S3 bucket %s", settings.s3_bucket)
        else:
            _storage = LocalStorage(".")
            log.info("Media storage: local disk (./uploads)")
    return _storage


def set_storage(storage: Storage | None) -> None:
    """Override the backend (tests). Pass None to reset to settings-derived."""
    global _storage
    _storage = storage
