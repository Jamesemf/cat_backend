"""Which photo a spotter's Cat-a-log card shows for each cat, and when it was taken.

``Cat.last_photo_path`` is a global column — whoever photographed that cat most
recently, which is very often somebody else. A Cat-a-log is a personal keepsake,
so a card must only ever show a photo its owner took: the highlight photo they
picked if it is still one of theirs, otherwise their own most recent photo of
that cat. Both the owner's tab (``GET /cats/mine``) and their public profile
resolve cards through here so the two always agree.

The photo's own ``spotted_at`` comes back with it because the card stamps a date
under the print. ``Cat.last_seen`` is the wrong date to stamp there twice over:
it moves whenever *anyone* logs the cat, and it ignores a highlight pointing at
an older photo — so the print and its caption drifted apart.
"""

import json
from datetime import datetime
from typing import NamedTuple

from sqlalchemy.orm import Session

from app.models.sighting import Sighting


class CoverPhoto(NamedTuple):
    """The photo a card shows, paired with when that photo was actually taken."""

    path: str
    spotted_at: datetime


def parse_covers(raw: str | None) -> dict[str, str]:
    """Pull just the per-cat highlight choices (catId -> raw storage key) out of a
    stored ``catalog_layout`` blob. Tolerates null/corrupt data with an empty map."""
    if not raw:
        return {}
    try:
        covers = json.loads(raw).get("covers", {})
    except (ValueError, TypeError, AttributeError):
        return {}
    if not isinstance(covers, dict):
        return {}
    return {str(k): str(v) for k, v in covers.items()}


def own_cover_photos(
    db: Session,
    user_id: int,
    cat_ids: list[int],
    covers: dict[str, str],
) -> dict[int, CoverPhoto | None]:
    """Map each of ``cat_ids`` to the photo this user's card should show.

    A chosen highlight is honoured only if it is still one of that user's own
    photos of that cat — which also stops a crafted ``catalog_layout`` pointing a
    card at an arbitrary storage key. Anything else falls back to the latest
    photo they took of the cat, and a cat they have no photo of maps to ``None``
    (the card draws its procedural-face placeholder) rather than to another
    spotter's photo.

    Each result carries the ``spotted_at`` of the sighting the photo came from,
    so the card can stamp the day the photo was taken. Where the same key appears
    on more than one of their sightings, the most recent one dates it.
    """
    if not cat_ids:
        return {}

    rows = (
        db.query(Sighting.cat_id, Sighting.photo_path, Sighting.spotted_at)
        .filter(
            Sighting.user_id == user_id,
            Sighting.cat_id.in_(cat_ids),
            Sighting.photo_path.isnot(None),
        )
        .order_by(Sighting.spotted_at.asc(), Sighting.id.asc())
        .all()
    )

    latest: dict[int, CoverPhoto] = {}
    mine: dict[int, dict[str, datetime]] = {}
    for cat_id, path, spotted_at in rows:
        # Ascending, so the last row wins — both for "their latest photo" and for
        # the date on a key they photographed more than once.
        latest[cat_id] = CoverPhoto(path, spotted_at)
        mine.setdefault(cat_id, {})[path] = spotted_at

    resolved: dict[int, CoverPhoto | None] = {}
    for cat_id in cat_ids:
        chosen = covers.get(str(cat_id))
        taken = mine.get(cat_id, {}).get(chosen) if chosen else None
        resolved[cat_id] = (
            CoverPhoto(chosen, taken) if taken is not None else latest.get(cat_id)
        )
    return resolved
