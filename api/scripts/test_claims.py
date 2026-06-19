"""Smoke test for the claim decision logic. No API key or DB needed.

Run from api/:  python scripts/test_claims.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models.cat import Cat
from app.services.claim_verification import (
    CLAIM_PHOTO_FLOOR,
    CLAIM_VERIFY_THRESHOLD,
    decide_claim,
)
from app.services.vision import CatFeatures


def make_cat(**overrides) -> Cat:
    fields = dict(
        name="Biscuit",
        breed="Orange Tabby",
        primary_color="orange",
        secondary_color="white",
        pattern="tabby",
        fur_length="short",
        eye_color="green",
        body_size="medium",
    )
    fields.update(overrides)
    cat = Cat()
    for k, v in fields.items():
        setattr(cat, k, v)
    return cat


def make_features(**overrides) -> CatFeatures:
    fields = dict(
        is_cat=True,
        cat_count=1,
        primary_color="orange",
        secondary_color="white",
        pattern="tabby",
        fur_length="short",
        eye_color="green",
        body_size="medium",
        breed="Orange Tabby",
    )
    fields.update(overrides)
    return CatFeatures(**fields)


def check(label: str, condition: bool, detail: str = "") -> bool:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {label}" + (f"  ({detail})" if detail else ""))
    return condition


def main() -> int:
    cat = make_cat()
    ok = True

    # 1. Perfect match across both photos -> verified
    d = decide_claim([make_features(), make_features()], cat)
    ok &= check("perfect match verifies", d.verified, f"avg={d.avg_confidence}")

    # 2. One photo of a clearly different cat -> rejected via per-photo floor
    wrong = make_features(
        primary_color="black", secondary_color=None, pattern="solid",
        breed="Black Cat", eye_color="yellow", fur_length="long",
    )
    d = decide_claim([make_features(), wrong], cat)
    ok &= check(
        "one mismatched photo rejects",
        not d.verified and d.reason is not None,
        f"avg={d.avg_confidence} reason={d.reason!r}",
    )

    # 3. Sparse cat record -> rejected as inconclusive
    sparse_cat = make_cat(
        breed=None, secondary_color=None, pattern=None,
        fur_length=None, eye_color=None, body_size=None,
    )
    d = decide_claim([make_features(), make_features()], sparse_cat)
    ok &= check(
        "sparse cat record rejects with guidance",
        not d.verified and "another sighting" in (d.reason or ""),
        f"reason={d.reason!r}",
    )

    # 4. Non-cat photo -> rejected
    d = decide_claim([make_features(is_cat=False, cat_count=0)], cat)
    ok &= check("non-cat photo rejects", not d.verified)

    # 5. Multi-cat photo -> rejected
    d = decide_claim([make_features(cat_count=2)], cat)
    ok &= check("multi-cat photo rejects", not d.verified)

    # 6. Borderline: photos above floor but below verify threshold -> rejected
    # Mismatches: pattern (.20) + breed (.20) + eye_color (.05) lost -> score 0.55
    near = make_features(pattern="solid", eye_color="yellow", breed="Orange and White")
    d = decide_claim([near, near], cat)
    ok &= check(
        "below-threshold average rejects",
        not d.verified and CLAIM_PHOTO_FLOOR <= d.avg_confidence < CLAIM_VERIFY_THRESHOLD,
        f"avg={d.avg_confidence}",
    )

    print()
    print("ALL PASS" if ok else "FAILURES PRESENT")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
