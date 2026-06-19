"""Ownership-claim verification.

Owner-submitted photos are run through the existing vision pipeline and their
features are scored against the claimed cat's stored features with the same
weighted similarity used for sighting Re-ID. The decision logic is a pure
function so it can be smoke-tested without an Anthropic key.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.models.cat import Cat
from app.services.vision import CatFeatures, VisionError, analyze_cat_photo
from app.utils.matching import FEATURE_WEIGHTS, score_candidate

log = logging.getLogger(__name__)

MIN_PHOTOS = 2
MAX_PHOTOS = 3
# Average score across photos required to verify. Sits between the Re-ID
# CONFIRM (0.55) and AUTO_LINK (0.85) thresholds: owner close-ups should match
# well, but categorical labels flip on borderline angles.
CLAIM_VERIFY_THRESHOLD = 0.70
# Any single photo below this floor rejects the claim outright (guards against
# padding a claim with photos of a different cat).
CLAIM_PHOTO_FLOOR = 0.40
# Per photo, the sum of FEATURE_WEIGHTS where both sides are non-null must reach
# this, otherwise the comparison is statistically meaningless (sparse cat record).
MIN_COMPARABLE_WEIGHT = 0.50
# After a rejection, the same user must wait this long before retrying the same cat.
CLAIM_COOLDOWN_HOURS = 24
# Max claim submissions per user per rolling day (DB-backed).
MAX_CLAIM_ATTEMPTS_PER_DAY = 5


@dataclass
class ClaimDecision:
    verified: bool
    avg_confidence: float
    per_photo: list[float]
    reason: str | None = None


def features_dict(f: CatFeatures) -> dict:
    """The 7 controlled-vocabulary keys used by score_candidate."""
    return {
        "primary_color": f.primary_color,
        "secondary_color": f.secondary_color,
        "pattern": f.pattern,
        "fur_length": f.fur_length,
        "eye_color": f.eye_color,
        "body_size": f.body_size,
        "breed": f.breed,
    }


def comparable_weight(features: dict, cat: Cat) -> float:
    """Sum of feature weights where both the photo and the cat have a value.

    score_candidate normalises by this, so a cat with only one recorded feature
    can score 1.0 on almost nothing. This guard requires enough overlap for the
    score to mean something.
    """
    total = 0.0
    for field, weight in FEATURE_WEIGHTS.items():
        if features.get(field) is not None and getattr(cat, field, None) is not None:
            total += weight
    return total


def decide_claim(photo_features: list[CatFeatures], cat: Cat) -> ClaimDecision:
    """Pure decision: score each photo against the cat and apply thresholds."""
    scores: list[float] = []
    for i, f in enumerate(photo_features, start=1):
        if not f.is_cat:
            return ClaimDecision(
                verified=False, avg_confidence=0.0, per_photo=scores,
                reason=f"Photo {i} doesn't appear to contain a cat.",
            )
        if f.cat_count > 1:
            return ClaimDecision(
                verified=False, avg_confidence=0.0, per_photo=scores,
                reason=f"Photo {i} contains more than one cat. Please photograph your cat alone.",
            )
        feats = features_dict(f)
        if comparable_weight(feats, cat) < MIN_COMPARABLE_WEIGHT:
            return ClaimDecision(
                verified=False, avg_confidence=0.0, per_photo=scores,
                reason=(
                    "There isn't enough detail on this cat's record to verify a match yet. "
                    "Log another sighting of them first, then try again."
                ),
            )
        scores.append(score_candidate(feats, cat))

    avg = round(sum(scores) / len(scores), 4) if scores else 0.0
    cat_label = cat.name or "this cat"

    floor_breach = min(scores) < CLAIM_PHOTO_FLOOR if scores else True
    if floor_breach:
        return ClaimDecision(
            verified=False, avg_confidence=avg, per_photo=scores,
            reason=(
                f"One of your photos doesn't look like {cat_label} "
                f"(match {min(scores) * 100:.0f}%, every photo needs at least {CLAIM_PHOTO_FLOOR * 100:.0f}%)."
            ),
        )
    if avg < CLAIM_VERIFY_THRESHOLD:
        return ClaimDecision(
            verified=False, avg_confidence=avg, per_photo=scores,
            reason=(
                f"These photos don't match {cat_label}'s recorded appearance "
                f"(match {avg * 100:.0f}%, needs {CLAIM_VERIFY_THRESHOLD * 100:.0f}%)."
            ),
        )
    return ClaimDecision(verified=True, avg_confidence=avg, per_photo=scores)


async def analyze_claim_photos(photos_bytes: list[bytes]) -> list[CatFeatures]:
    """Run vision on each photo. Raises VisionError if the service is unavailable."""
    results: list[CatFeatures] = []
    for contents in photos_bytes:
        results.append(await analyze_cat_photo(contents))
    return results
