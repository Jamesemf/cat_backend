"""Which photo a spotter's Cat-a-log card shows for each cat.

``Cat.last_photo_path`` is a global column — whoever photographed that cat most
recently, which is very often somebody else. A Cat-a-log is a personal keepsake,
so a card must only ever show a photo its owner took: the highlight photo they
picked if it is still one of theirs, otherwise their own most recent photo of
that cat. Both the owner's tab (``GET /cats/mine``) and their public profile
resolve cards through here so the two always agree.
"""

import json

from sqlalchemy.orm import Session

from app.models.sighting import Sighting


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
) -> dict[int, str | None]:
    """Map each of ``cat_ids`` to the photo key this user's card should show.

    A chosen highlight is honoured only if it is still one of that user's own
    photos of that cat — which also stops a crafted ``catalog_layout`` pointing a
    card at an arbitrary storage key. Anything else falls back to the latest
    photo they took of the cat, and a cat they have no photo of maps to ``None``
    (the card draws its procedural-face placeholder) rather than to another
    spotter's photo.
    """
    if not cat_ids:
        return {}

    rows = (
        db.query(Sighting.cat_id, Sighting.photo_path)
        .filter(
            Sighting.user_id == user_id,
            Sighting.cat_id.in_(cat_ids),
            Sighting.photo_path.isnot(None),
        )
        .order_by(Sighting.spotted_at.asc(), Sighting.id.asc())
        .all()
    )

    latest: dict[int, str] = {}
    mine: dict[int, set[str]] = {}
    for cat_id, path in rows:
        latest[cat_id] = path  # ascending, so the last row wins
        mine.setdefault(cat_id, set()).add(path)

    resolved: dict[int, str | None] = {}
    for cat_id in cat_ids:
        chosen = covers.get(str(cat_id))
        resolved[cat_id] = (
            chosen if chosen in mine.get(cat_id, set()) else latest.get(cat_id)
        )
    return resolved
