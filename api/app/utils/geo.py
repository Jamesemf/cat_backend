"""Coarsening of the coordinates attached to a public sighting.

Every coordinate a client sends for a sighting is snapped to a fixed grid before
it is stored, so the precise point is never persisted and never published. Cats
are overwhelmingly photographed where they live, and the claim flow explicitly
asks for photos taken inside the claimant's home — an exact coordinate on a
public map is therefore a home address, either the spotter's or the owner's.

Snapping, not random jitter. Jitter looks stronger and is weaker: the noise is
drawn independently per sighting, so averaging N sightings of the same cat on
the same doorstep converges on the true point, and a well-loved neighbourhood
cat has plenty of N. Snapping is deterministic — every coordinate inside a cell
yields the identical stored value, so the hundredth sighting of a cat reveals no
more about where it lives than the first did.

LOCATION_DECIMALS = 3 gives cells about 111 m tall and 111·cos(lat) m wide
(~69 m at UK latitudes): the right street, not the right doorstep, which is the
resolution the map actually needs.

Applied at the schema boundary (schemas/sighting.py) rather than in the router,
so no future write path can persist a precise coordinate by forgetting to call
this.
"""

from __future__ import annotations

# Decimal places kept on a stored/published coordinate. Changing this changes
# the privacy promise made in the published privacy policy — keep the two in
# step (cats-website/src/app/privacy/page.tsx, "Sighting location").
LOCATION_DECIMALS = 3


def coarsen(value: float) -> float:
    """Snap one coordinate to the public grid. Idempotent."""
    return round(value, LOCATION_DECIMALS)
