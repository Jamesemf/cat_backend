"""Contract tests for the storage backend (exercised via LocalStorage)."""

import pytest

from app.services.storage import UPLOADS_PREFIX, new_key


def test_put_returns_key_under_prefix(storage):
    key = storage.put(b"\xff\xd8data", ext=".jpg")
    assert key.startswith(f"{UPLOADS_PREFIX}/")
    assert key.endswith(".jpg")
    assert storage.exists(key)


def test_put_normalizes_extension_without_dot(storage):
    key = storage.put(b"x", ext="png")
    assert key.endswith(".png")


def test_put_under_custom_prefix(storage):
    key = storage.put(b"x", ext=".jpg", prefix=f"{UPLOADS_PREFIX}/claims")
    assert key.startswith(f"{UPLOADS_PREFIX}/claims/")
    assert storage.exists(key)


def test_roundtrip_then_delete(storage):
    key = storage.put(b"bytes", ext=".jpg")
    assert storage.exists(key)
    storage.delete(key)
    assert not storage.exists(key)


def test_delete_missing_is_noop(storage):
    storage.delete("uploads/never-existed.jpg")  # must not raise


def test_local_url_returns_key_unchanged(storage):
    # Local backend hands back the relative key; the frontend prepends API_BASE.
    key = storage.put(b"x", ext=".png")
    assert storage.url(key) == key


def test_list_objects_reports_size_and_tzaware_mtime(storage):
    k1 = storage.put(b"a", ext=".jpg")
    k2 = storage.put(b"bb", ext=".jpg", prefix=f"{UPLOADS_PREFIX}/claims")
    objs = {o.key: o for o in storage.list_objects()}
    assert {k1, k2} <= set(objs)
    assert objs[k2].size == 2
    assert objs[k1].last_modified.tzinfo is not None


def test_keys_are_unique(storage):
    assert new_key(".jpg") != new_key(".jpg")


def test_path_traversal_is_rejected(storage):
    with pytest.raises(ValueError):
        storage.save("../escape.jpg", b"x")
