"""DB <-> bucket consistency: the reconciliation guarantees.

These tests assert the invariant the user asked for — that the database and the
storage bucket stay in sync — by driving them out of sync deliberately and
checking reconcile() detects and (where safe) repairs the drift.
"""

from datetime import datetime, timedelta, timezone

from app.models.cat import Cat
from app.models.claim import CatClaim, ClaimPhoto
from app.models.explorer import ExplorerPost
from app.models.sighting import Sighting
from app.services.reconcile import gather_referenced_keys, reconcile


def _commit(db, *objs):
    for o in objs:
        db.add(o)
    db.commit()


def test_gather_referenced_keys_spans_every_media_table(db, storage):
    cat = Cat(last_photo_path="uploads/cat.jpg")
    _commit(db, cat)
    claim = CatClaim(cat_id=cat.id, user_id=1, status="pending")
    _commit(
        db,
        Sighting(photo_path="uploads/s.jpg", latitude=0.0, longitude=0.0),
        ExplorerPost(photo_path="uploads/p.jpg"),
        claim,
    )
    _commit(db, ClaimPhoto(claim_id=claim.id, photo_path="uploads/claims/c.jpg"))

    assert gather_referenced_keys(db) == {
        "uploads/cat.jpg",
        "uploads/s.jpg",
        "uploads/p.jpg",
        "uploads/claims/c.jpg",
    }


def test_in_sync_when_every_object_is_referenced(db, storage):
    key = storage.put(b"img", ext=".jpg")
    _commit(db, Sighting(photo_path=key, latitude=0.0, longitude=0.0))

    report = reconcile(db, storage)

    assert report.in_sync
    assert report.orphan_keys == []
    assert report.dangling_keys == []
    assert storage.exists(key)  # a referenced object is never touched


def test_old_orphan_is_swept(db, storage):
    # Uploaded (e.g. by /analyze) but never committed to any row.
    key = storage.put(b"junk", ext=".jpg")
    later = datetime.now(timezone.utc) + timedelta(hours=48)

    report = reconcile(db, storage, now=later, grace_hours=24)

    assert key in report.orphan_keys
    assert key in report.deleted_keys
    assert not storage.exists(key)


def test_fresh_orphan_is_protected_by_grace_window(db, storage):
    # A just-uploaded photo a user hasn't committed yet must survive.
    key = storage.put(b"fresh", ext=".jpg")

    report = reconcile(db, storage, grace_hours=24)

    assert key not in report.orphan_keys
    assert storage.exists(key)


def test_orphan_only_reported_when_deletion_disabled(db, storage):
    key = storage.put(b"junk", ext=".jpg")
    later = datetime.now(timezone.utc) + timedelta(hours=48)

    report = reconcile(db, storage, now=later, grace_hours=24, delete_orphans=False)

    assert key in report.orphan_keys
    assert report.deleted_keys == []
    assert storage.exists(key)


def test_referenced_object_survives_even_when_ancient(db, storage):
    key = storage.put(b"img", ext=".jpg")
    _commit(db, ExplorerPost(photo_path=key))
    far_future = datetime.now(timezone.utc) + timedelta(days=365)

    report = reconcile(db, storage, now=far_future, grace_hours=24)

    assert key not in report.orphan_keys
    assert storage.exists(key)


def test_dangling_reference_is_detected_but_not_auto_repaired(db, storage):
    # Row points at a key with no backing object (manual deletion / failed upload).
    _commit(db, Cat(last_photo_path="uploads/missing.jpg"))

    report = reconcile(db, storage)

    assert "uploads/missing.jpg" in report.dangling_keys
    assert not report.in_sync
    assert report.deleted_keys == []  # dangling refs are never silently dropped
