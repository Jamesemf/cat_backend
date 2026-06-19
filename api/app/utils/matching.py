import math
from datetime import datetime, timedelta
from sqlalchemy.orm import Session

from app.models.cat import Cat

# Weighted contribution of each feature toward a match score
FEATURE_WEIGHTS: dict[str, float] = {
    "primary_color": 0.25,
    "breed":         0.20,
    "pattern":       0.20,
    "fur_length":    0.15,
    "secondary_color": 0.10,
    "eye_color":     0.05,
    "body_size":     0.05,
}

# Reference point on the scoring scale, kept for documentation (and referenced
# by claim verification). Auto-linking is deliberately disabled: a cat is only
# ever linked to an existing record when the user confirms it, no matter how
# confident the match.
AUTO_LINK_THRESHOLD = 0.85
# Best-match confidence >= this → surface the cat as a confirmation candidate
CONFIRM_THRESHOLD = 0.55
# A score is only trustworthy if enough feature weight is actually comparable on
# both sides. Without this floor, a single shared attribute (e.g. just
# primary_color) scores a perfect 1.0 and would auto-link two unrelated cats.
# Mirrors the comparable-weight gate used in claim verification. 0.5 requires
# roughly three overlapping features before any match is reported.
MIN_COMPARABLE_WEIGHT = 0.5


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def score_candidate(features: dict, cat: Cat) -> float:
    """Weighted feature similarity between an incoming sighting and an existing cat.

    Only features present on both sides contribute. Returns 0–1. When too little
    feature weight is comparable to be meaningful (< MIN_COMPARABLE_WEIGHT) the
    score is forced to 0 so a sparse coincidental match can't auto-link.
    """
    total_weight = 0.0
    matched_weight = 0.0
    for field, weight in FEATURE_WEIGHTS.items():
        new_val = features.get(field)
        cat_val = getattr(cat, field, None)
        if new_val is None or cat_val is None:
            continue
        total_weight += weight
        if new_val == cat_val:
            matched_weight += weight
    if total_weight < MIN_COMPARABLE_WEIGHT:
        return 0.0
    return round(matched_weight / total_weight, 4)


def find_match_candidates(
    db: Session,
    lat: float,
    lng: float,
    features: dict,
    radius_km: float = 0.5,
    time_days: int = 90,
    max_results: int = 3,
) -> list[tuple[Cat, float]]:
    """Return (cat, confidence) pairs for cats near the sighting that share features.

    Uses a lat/lng bounding box to prefilter in SQL (SQLite has no PostGIS), then
    applies the precise Haversine check and feature scoring in Python.
    Only results above CONFIRM_THRESHOLD are returned, sorted by confidence desc.
    """
    cutoff = datetime.utcnow() - timedelta(days=time_days)

    # Approximate degree deltas for the radius (sufficient for prefilter)
    lat_delta = radius_km / 111.0
    lng_delta = radius_km / max(111.0 * math.cos(math.radians(lat)), 0.001)

    prefiltered = (
        db.query(Cat)
        .filter(
            Cat.last_lat.isnot(None),
            Cat.last_lng.isnot(None),
            Cat.last_lat.between(lat - lat_delta, lat + lat_delta),
            Cat.last_lng.between(lng - lng_delta, lng + lng_delta),
            Cat.last_seen >= cutoff,
        )
        .all()
    )

    results: list[tuple[Cat, float]] = []
    for cat in prefiltered:
        dist = haversine_km(lat, lng, cat.last_lat, cat.last_lng)
        if dist > radius_km:
            continue
        score = score_candidate(features, cat)
        if score >= CONFIRM_THRESHOLD:
            results.append((cat, score))

    return sorted(results, key=lambda x: x[1], reverse=True)[:max_results]
