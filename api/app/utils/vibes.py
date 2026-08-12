"""Counting what spotters have said about a cat.

`cats.vibes` is not a running record of a cat's character — each new sighting
overwrites it wholesale (see routers/sightings.py), so it only ever holds the
most recent spotter's words. A cat's actual character has to be counted from the
sightings themselves, which is what this does.

The profile sizes each vibe by its count, so a vibe five spotters agreed on reads
louder than a one-off.
"""

from typing import Iterable


def tally_vibes(sightings: Iterable, fallback: str | None = None) -> list[dict]:
    """Count each vibe across `sightings`, most-agreed first.

    Vibes are stored as free-text comma-separated strings, so:

      * matching is case-insensitive — "Playful" and "playful" are one vibe. The
        label takes its casing from the first sighting in the order given; callers
        pass them newest-first, so the most recent spelling is the one shown;
      * a spotter repeating a vibe within one sighting counts once, so a single
        spotter can't inflate it;
      * ties break alphabetically, to keep the order stable between requests.

    `fallback` (a cat's own `vibes`) is used only when no sighting carries any —
    cats logged before sightings recorded vibes would otherwise show nothing.
    Those come back at a count of 1 each, so they render at a flat size.
    """
    counts: dict[str, dict] = {}

    for sighting in sightings:
        raw = getattr(sighting, "vibes", None)
        if not raw:
            continue
        seen: set[str] = set()
        for part in raw.split(","):
            label = part.strip()
            if not label:
                continue
            key = label.casefold()
            if key in seen:
                continue
            seen.add(key)
            entry = counts.get(key)
            if entry is not None:
                entry["count"] += 1
            else:
                counts[key] = {"label": label, "count": 1}

    if not counts and fallback:
        for part in fallback.split(","):
            label = part.strip()
            if label:
                counts.setdefault(label.casefold(), {"label": label, "count": 1})

    return sorted(counts.values(), key=lambda v: (-v["count"], v["label"].casefold()))
