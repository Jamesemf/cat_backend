import math
from datetime import datetime, timezone


def compute_rarity_score(sighting_count: int, last_seen: datetime) -> float:
    """
    Exponential decay on sighting count, with a small elusive bonus for cats
    not seen recently.

    Approximate tier boundaries:
      1 sight  → ~1.00  Legendary
      2 sights → ~0.74  Ultra-Rare
      3 sights → ~0.55  Rare
      4 sights → ~0.41  Uncommon
      5+       → <0.30  Common (faster if seen recently)
    """
    # Owner-created cats with no sightings have no rarity data — score 0 keeps
    # the nightly recompute from marking them Legendary via exp(0).
    if sighting_count == 0:
        return 0.0

    freq_score = math.exp(-0.3 * max(0, sighting_count - 1))

    last_seen_utc = (
        last_seen.replace(tzinfo=timezone.utc)
        if last_seen.tzinfo is None
        else last_seen
    )
    days_gone = (datetime.now(timezone.utc) - last_seen_utc).days
    # Up to +0.15 for a cat unseen for 30+ days
    elusive_bonus = min(0.15, days_gone / 200)

    return round(min(1.0, freq_score + elusive_bonus), 4)
