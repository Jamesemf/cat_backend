"""Audience fan-out when a sighting is committed: the spotter's friends, then
nearby explorers.

Runs in FastAPI BackgroundTasks (a worker thread) after the sighting's own
transaction is done, so it opens its own DB session — mirroring services/push.py.
Failures are logged, never raised: fan-out must never break the sighting request.

The verified owner (the only direct per-cat subscription — claiming a cat is
how you follow it) is notified inline in the sightings router and excluded
here. This task covers two audiences, each gated by its own users.notify_*
column and deduplicated against the other:

  * the spotter's friends, wherever they are ("friend_new_cat" /
    "friend_sighting") — friendship is the one subscription that ignores
    distance, which is the whole point of it;
  * users who have explored tiles near the sighting ("new_cat" /
    "nearby_sighting").

Friends are notified first, deliberately: a friend who also happens to live
nearby should hear that *their friend* found something, not that a stranger did.
"""

from __future__ import annotations

import logging

from app.db.session import SessionLocal
from app.models.exploration import ExploredTile
from app.models.notification import Notification
from app.models.user import User
from app.services.friends import friend_ids
from app.services.push import push_to_user
from app.utils.hexgrid import disk_keys

log = logging.getLogger(__name__)

# Explored tiles are ~200 m across on the ground; 3 rings around the sighting's
# tile makes "nearby" roughly a kilometre of ground the user has actually
# uncovered (home-seed tiles included — your home neighbourhood counts).
NEARBY_TILE_RADIUS = 3


def notify_sighting_audiences(
    sighting_id: int,
    cat_id: int,
    cat_name: str | None,
    latitude: float,
    longitude: float,
    is_new_cat: bool,
    exclude_user_ids: set[int],
    spotter_id: int | None = None,
    spotter_name: str | None = None,
) -> None:
    name = cat_name or "A cat"
    db = SessionLocal()
    pushes: list[tuple[int, str, str]] = []
    try:
        notified = set(exclude_user_ids)

        # The spotter's friends, at any distance. First, so a friend who is also
        # a nearby explorer is told it was their friend who found it.
        if spotter_id is not None:
            who = spotter_name or "A friend"
            if is_new_cat:
                friend_type = "friend_new_cat"
                friend_title = f"{who} found a new cat!"
                friend_body = f"{name} had never been logged before. Tap to meet them."
                friend_pref = User.notify_friend_new_cats
            else:
                friend_type = "friend_sighting"
                friend_title = f"{who} spotted {name}"
                friend_body = "Tap to see where."
                friend_pref = User.notify_friend_sightings

            ids = friend_ids(db, spotter_id)
            if ids:
                friend_rows = (
                    db.query(User.id)
                    .filter(User.id.in_(ids), friend_pref.is_(True))
                    .all()
                )
                for (uid,) in friend_rows:
                    if uid in notified:
                        continue
                    notified.add(uid)
                    db.add(
                        Notification(
                            user_id=uid,
                            type=friend_type,
                            title=friend_title,
                            body=friend_body,
                            cat_id=cat_id,
                            sighting_id=sighting_id,
                        )
                    )
                    pushes.append((uid, friend_title, friend_body))

        # Nearby explorers, gated by their per-type preference.
        if is_new_cat:
            notif_type = "new_cat"
            title = "A new cat appeared in your area!"
            body = f"{name} was spotted for the first time near ground you've explored. Tap to meet them."
            pref = User.notify_new_cat_in_area
        else:
            notif_type = "nearby_sighting"
            title = f"{name} was spotted nearby"
            body = "A cat was just seen in an area you've explored. Tap to see where."
            pref = User.notify_nearby_sightings

        keys = disk_keys(longitude, latitude, NEARBY_TILE_RADIUS)
        nearby_ids = (
            db.query(ExploredTile.user_id)
            .join(User, User.id == ExploredTile.user_id)
            .filter(ExploredTile.tile_key.in_(keys), pref.is_(True))
            .distinct()
            .all()
        )
        for (uid,) in nearby_ids:
            if uid in notified:
                continue
            notified.add(uid)
            db.add(
                Notification(
                    user_id=uid,
                    type=notif_type,
                    title=title,
                    body=body,
                    cat_id=cat_id,
                    sighting_id=sighting_id,
                )
            )
            pushes.append((uid, title, body))

        db.commit()
    except Exception:
        log.exception("Sighting notification fan-out failed for sighting %d", sighting_id)
        return
    finally:
        db.close()

    data = {"cat_id": cat_id, "sighting_id": sighting_id}
    for uid, title, body in pushes:
        push_to_user(uid, title, body, data)
