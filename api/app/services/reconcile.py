"""Keep the DB and the storage bucket consistent.

Two kinds of drift can appear between the catalog rows and the stored objects:

* **Orphan objects** — a file was uploaded (e.g. POST /sightings/analyze saved
  it) but never committed to a row, or its row was deleted without cleanup.
  These waste space and are swept once older than a grace window (so we never
  race a just-uploaded, not-yet-committed photo).
* **Dangling references** — a row points at a key that no longer exists in
  storage (manual deletion, failed upload, bad migration). These can't be
  auto-repaired, so they're reported/logged for a human.

The functions take ``db`` and ``storage`` explicitly so they're trivially
testable against a LocalStorage rooted at a tmp dir — which *is* the "bucket" in
the test.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models.cat import Cat
from app.models.claim import ClaimPhoto
from app.models.explorer import ExplorerPost
from app.models.sighting import Sighting
from app.services.storage import UPLOADS_PREFIX, Storage, get_storage

log = logging.getLogger(__name__)


@dataclass
class ReconcileReport:
    referenced: int = 0          # keys referenced by some DB row
    stored: int = 0             # objects present in storage
    orphan_keys: list[str] = field(default_factory=list)   # stored, unreferenced, past grace
    deleted_keys: list[str] = field(default_factory=list)  # orphans actually removed
    dangling_keys: list[str] = field(default_factory=list)  # referenced, missing from storage

    @property
    def in_sync(self) -> bool:
        return not self.orphan_keys and not self.dangling_keys


def gather_referenced_keys(db: Session) -> set[str]:
    """Every storage key referenced by a surviving row, across all media tables."""
    keys: set[str] = set()
    for model, column in (
        (Sighting, Sighting.photo_path),
        (ExplorerPost, ExplorerPost.photo_path),
        (Cat, Cat.last_photo_path),
        (ClaimPhoto, ClaimPhoto.photo_path),
    ):
        for (value,) in db.query(column).filter(column.isnot(None)).distinct():
            if value:
                keys.add(value)
    return keys


def reconcile(
    db: Session,
    storage: Storage | None = None,
    *,
    now: datetime | None = None,
    grace_hours: int = 24,
    delete_orphans: bool = True,
    prefix: str = UPLOADS_PREFIX,
) -> ReconcileReport:
    """Compare DB references against stored objects and optionally sweep orphans.

    An object is an orphan only if it is unreferenced AND older than
    ``grace_hours`` — the grace window protects photos saved by /analyze that a
    user hasn't committed yet. Dangling references are detected and reported but
    never auto-deleted.
    """
    storage = storage or get_storage()
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=grace_hours)

    referenced = gather_referenced_keys(db)
    stored = {obj.key: obj for obj in storage.list_objects(prefix)}

    report = ReconcileReport(referenced=len(referenced), stored=len(stored))

    for key, obj in stored.items():
        if key in referenced:
            continue
        if obj.last_modified >= cutoff:
            continue  # still within grace — a pending commit may claim it
        report.orphan_keys.append(key)
        if delete_orphans:
            storage.delete(key)
            report.deleted_keys.append(key)

    report.dangling_keys = sorted(referenced - stored.keys())

    if report.deleted_keys:
        log.info("Storage reconcile: swept %d orphan object(s).", len(report.deleted_keys))
    if report.dangling_keys:
        log.warning(
            "Storage reconcile: %d DB reference(s) point at missing objects: %s",
            len(report.dangling_keys),
            report.dangling_keys[:10],
        )
    return report
