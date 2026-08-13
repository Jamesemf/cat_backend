"""What makes a claim or registration photo unreviewable.

The two flows diverge on one point: a claim may hold a second cat, a
registration may not. Claims ask for the owner at home with the cat, and a
two-cat household photographs both — that reaches the moderator, who has the
cat's own record to compare against. A registration's photos become the new
Cat's features on approval, and with two cats in frame there's no saying whose
features those are.
"""

from app.services.claim_verification import (
    invalid_photo_reason,
    invalid_registration_photo_reason,
)
from app.services.vision import CatFeatures


def _cat(count: int = 1, is_cat: bool = True) -> CatFeatures:
    return CatFeatures(is_cat=is_cat, cat_count=count, primary_color="orange")


def test_a_claim_photo_with_the_owners_other_cat_in_it_is_reviewable():
    assert invalid_photo_reason([_cat(), _cat(2), _cat()]) is None


def test_a_registration_photo_with_two_cats_is_rejected():
    reason = invalid_registration_photo_reason([_cat(), _cat(2)])
    assert reason is not None
    assert "Photo 2" in reason


def test_both_flows_reject_a_photo_with_no_cat_in_it():
    features = [_cat(), _cat(count=0, is_cat=False)]
    assert "Photo 2" in (invalid_photo_reason(features) or "")
    assert "Photo 2" in (invalid_registration_photo_reason(features) or "")


def test_the_missing_cat_is_reported_before_the_extra_one():
    """A photo of two dogs should be told it has no cat, not too many."""
    reason = invalid_registration_photo_reason([_cat(count=2, is_cat=False)])
    assert reason == "Photo 1 doesn't appear to contain a cat."


def test_ordinary_photos_pass_both():
    assert invalid_photo_reason([_cat(), _cat()]) is None
    assert invalid_registration_photo_reason([_cat(), _cat()]) is None
