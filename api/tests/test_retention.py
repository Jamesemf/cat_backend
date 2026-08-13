"""Claim photos are deleted once their claim has been closed long enough.

The privacy policy publishes a 90-day period for the photos attached to a
rejected or revoked claim. Those photos show an identifiable person inside their
home, so the period is a promise rather than housekeeping — these tests are what
keeps it honest.
"""

from datetime import datetime, timedelta, timezone

from app.models.cat import Cat
from app.models.claim import CatClaim, ClaimPhoto
from app.models.rate_limit import DailyUsage
from app.services.retention import (
    CLAIM_PHOTO_RETENTION_DAYS,
    RATE_LIMIT_RETENTION_DAYS,
    sweep_claim_photos,
    sweep_rate_limit_counters,
)


def _naive_days_ago(days: int) -> datetime:
    """Match the naive-UTC convention of the DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)


def _claim(db, storage, *, status: str, decided_days_ago: int | None, cat_id=None):
    """A claim in `status`, with one stored photo. Returns (claim, key)."""
    key = storage.put(b"evidence", ext=".jpg")
    claim = CatClaim(
        cat_id=cat_id,
        user_id=1,
        status=status,
        decided_at=None if decided_days_ago is None else _naive_days_ago(decided_days_ago),
    )
    db.add(claim)
    db.commit()
    db.add(ClaimPhoto(claim_id=claim.id, photo_path=key))
    db.commit()
    return claim, key


def _photo_count(db, claim) -> int:
    return db.query(ClaimPhoto).filter(ClaimPhoto.claim_id == claim.id).count()


# --- What gets swept --------------------------------------------------------

def test_rejected_claim_photos_go_after_the_window(db, storage):
    claim, key = _claim(db, storage, status="rejected", decided_days_ago=91)

    assert sweep_claim_photos(db) == 1
    assert _photo_count(db, claim) == 0
    assert not storage.exists(key)


def test_revoked_claim_photos_go_after_the_window(db, storage):
    claim, key = _claim(db, storage, status="revoked", decided_days_ago=91)

    assert sweep_claim_photos(db) == 1
    assert not storage.exists(key)


def test_the_claim_record_itself_survives(db, storage):
    """Only the photographs go — the decision, and the re-claim cooldown it
    drives, are not personal data we need to destroy."""
    claim, _ = _claim(db, storage, status="rejected", decided_days_ago=91)

    sweep_claim_photos(db)

    assert db.query(CatClaim).filter(CatClaim.id == claim.id).one().status == "rejected"


# --- What is deliberately kept ---------------------------------------------

def test_photos_survive_inside_the_window(db, storage):
    claim, key = _claim(db, storage, status="rejected", decided_days_ago=30)

    assert sweep_claim_photos(db) == 0
    assert _photo_count(db, claim) == 1
    assert storage.exists(key)


def test_pending_claims_are_untouched(db, storage):
    """A moderator still has to read them, however old the claim is."""
    claim, key = _claim(db, storage, status="pending", decided_days_ago=None)

    assert sweep_claim_photos(db) == 0
    assert storage.exists(key)


def test_verified_claims_are_untouched(db, storage):
    """Evidence for a grant that is still in force."""
    claim, key = _claim(db, storage, status="verified", decided_days_ago=400)

    assert sweep_claim_photos(db) == 0
    assert storage.exists(key)


# --- Safety -----------------------------------------------------------------

def test_a_photo_still_used_as_a_cat_cover_is_not_unlinked(db, storage):
    """Approving a `register` claim promotes its first photo to the cat's cover.

    Revoking that claim later must clear the evidence row without blanking the
    cat's picture, so the object stays while the Cat still points at it.
    """
    key = storage.put(b"evidence", ext=".jpg")
    cat = Cat(last_photo_path=key)
    db.add(cat)
    db.commit()
    claim = CatClaim(
        cat_id=cat.id,
        user_id=1,
        status="revoked",
        source="register",
        decided_at=_naive_days_ago(91),
    )
    db.add(claim)
    db.commit()
    db.add(ClaimPhoto(claim_id=claim.id, photo_path=key))
    db.commit()

    assert sweep_claim_photos(db) == 1
    assert _photo_count(db, claim) == 0
    # The row is gone; the object is not, because the cat still shows it.
    assert storage.exists(key)


def test_sweep_is_idempotent(db, storage):
    _claim(db, storage, status="rejected", decided_days_ago=91)

    assert sweep_claim_photos(db) == 1
    assert sweep_claim_photos(db) == 0


def test_sweep_is_a_noop_on_an_empty_database(db, storage):
    assert sweep_claim_photos(db) == 0


def test_retention_window_is_the_published_one(db, storage):
    """Guard against the period drifting away from the policy silently."""
    assert CLAIM_PHOTO_RETENTION_DAYS == 90


# --- Rate-limit counters ----------------------------------------------------

def _usage(db, key: str, days_ago: int):
    day = (
        datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days_ago)
    ).date().isoformat()
    db.add(DailyUsage(key=key, day=day, count=1))
    db.commit()


def test_stale_ip_counters_are_dropped(db, storage):
    """Half these keys embed an IP address, and yesterday's is never read."""
    _usage(db, "explorer:ip:1.2.3.4", days_ago=30)

    assert sweep_rate_limit_counters(db) == 1
    assert db.query(DailyUsage).count() == 0


def test_recent_counters_survive(db, storage):
    _usage(db, "sightings:user:42", days_ago=1)

    assert sweep_rate_limit_counters(db) == 0
    assert db.query(DailyUsage).count() == 1


def test_counter_sweep_is_idempotent(db, storage):
    _usage(db, "explorer:ip:1.2.3.4", days_ago=30)

    assert sweep_rate_limit_counters(db) == 1
    assert sweep_rate_limit_counters(db) == 0


def test_counter_window_is_the_published_one(db, storage):
    assert RATE_LIMIT_RETENTION_DAYS == 14
