import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.cat import Cat
from app.models.claim import CatClaim
from app.models.exploration import ExploredTile
from app.models.explorer import ExplorerPost, PostComment, PostMeow, PostReport
from app.models.notification import Notification
from app.models.sighting import Sighting
from app.models.user import User
from app.services.auth_service import hash_password
from app.utils.rarity import compute_rarity_score

log = logging.getLogger(__name__)

DEMO_EMAIL = "demo@catapp.uk"
DEMO_PASSWORD = "CatDemo123!"
DEMO_MARKER = "apple_review_demo"


BASE_LAT = 51.3198
BASE_LNG = -0.2409
DEMO_TILE_Q_RANGE = range(-7476, -7466)
DEMO_TILE_R_RANGE = range(14836, 14846)

PHOTO_URLS = {
    "orange_tabby": "https://images.pexels.com/photos/25524459/pexels-photo-25524459.jpeg?auto=compress&cs=tinysrgb&w=1200",
    "ginger_white": "https://images.pexels.com/photos/20673054/pexels-photo-20673054.jpeg?auto=compress&cs=tinysrgb&w=1200",
    "grey_tabby": "https://images.pexels.com/photos/17127912/pexels-photo-17127912.jpeg?auto=compress&cs=tinysrgb&w=1200",
    "black_cat": "https://images.pexels.com/photos/18364269/pexels-photo-18364269.jpeg?auto=compress&cs=tinysrgb&w=1200",
    "calico": "https://images.pexels.com/photos/29020203/pexels-photo-29020203.jpeg?auto=compress&cs=tinysrgb&w=1200",
    "tuxedo": "https://images.pexels.com/photos/17218018/pexels-photo-17218018.jpeg?auto=compress&cs=tinysrgb&w=1200",
}

CATS = [
    {
        "key": "biscuit",
        "name": "Biscuit",
        "breed": "Orange Tabby",
        "photo": "orange_tabby",
        "dlat": 0.0012,
        "dlng": 0.0018,
        "vibes": "regal,fluffy,confident",
        "primary_color": "orange",
        "secondary_color": "white",
        "pattern": "tabby",
        "fur_length": "short",
        "eye_color": "amber",
        "body_size": "large",
        "first_days": 62,
        "last_days": 0.2,
        "frame": "gold",
        "caption": "Golden hour for a golden boy.",
    },
    {
        "key": "clementine",
        "name": "Clementine",
        "breed": "Ginger & White",
        "photo": "ginger_white",
        "dlat": -0.0021,
        "dlng": 0.0009,
        "vibes": "dapper,friendly,curious",
        "primary_color": "orange",
        "secondary_color": "white",
        "pattern": "bicolor",
        "fur_length": "short",
        "eye_color": "amber",
        "body_size": "medium",
        "first_days": 48,
        "last_days": 1.1,
        "frame": "rose",
        "caption": "Waiting by the bakery door again.",
    },
    {
        "key": "ash",
        "name": "Ash",
        "breed": "Grey Tabby & White",
        "photo": "grey_tabby",
        "dlat": 0.0032,
        "dlng": -0.0014,
        "vibes": "playful,bouncy,dramatic",
        "primary_color": "gray",
        "secondary_color": "white",
        "pattern": "tabby",
        "fur_length": "short",
        "eye_color": "yellow",
        "body_size": "small",
        "first_days": 35,
        "last_days": 2.3,
        "frame": "sky",
        "caption": "Caught mid-mischief.",
    },
    {
        "key": "wednesday",
        "name": "Wednesday",
        "breed": "Domestic Shorthair",
        "photo": "black_cat",
        "dlat": -0.0009,
        "dlng": -0.0026,
        "vibes": "mysterious,calm,watchful",
        "primary_color": "black",
        "secondary_color": None,
        "pattern": "solid",
        "fur_length": "short",
        "eye_color": "green",
        "body_size": "medium",
        "first_days": 55,
        "last_days": 0.9,
        "frame": "noir",
        "caption": "She sees everything from that garden.",
    },
    {
        "key": "juniper",
        "name": "Juniper",
        "breed": "Longhaired Calico",
        "photo": "calico",
        "dlat": 0.0024,
        "dlng": 0.0031,
        "vibes": "intense,elegant,nocturnal",
        "primary_color": "brown",
        "secondary_color": "white",
        "pattern": "calico",
        "fur_length": "long",
        "eye_color": "green",
        "body_size": "medium",
        "first_days": 41,
        "last_days": 3.4,
        "frame": "meadow",
        "caption": "Evening patrol.",
    },
    {
        "key": "mochi",
        "name": "Mochi",
        "breed": "Tuxedo",
        "photo": "tuxedo",
        "dlat": -0.0031,
        "dlng": 0.0022,
        "vibes": "gentle,sleepy,polite",
        "primary_color": "black",
        "secondary_color": "white",
        "pattern": "bicolor",
        "fur_length": "short",
        "eye_color": "yellow",
        "body_size": "medium",
        "first_days": 29,
        "last_days": 0.5,
        "frame": "mint",
        "caption": "Politely requested a second breakfast.",
    },
]


def is_demo_user(user: User | None) -> bool:
    return bool(user and user.email.lower() == DEMO_EMAIL)


def can_see_demo_content(user: User | None) -> bool:
    return bool(user and (user.is_admin or is_demo_user(user)))


def demo_features(key: str) -> str:
    return json.dumps({"seed": DEMO_MARKER, "key": key})


def demo_feature_key(raw: str | None) -> str | None:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if data.get("seed") != DEMO_MARKER:
        return None
    key = data.get("key")
    return key if isinstance(key, str) else None


def is_demo_feature_expr(column):
    return column.like(f'%"seed": "{DEMO_MARKER}"%')


def visible_cats_query(query, current_user: User | None):
    if can_see_demo_content(current_user):
        return query
    return query.filter(or_(Cat.features_json.is_(None), ~is_demo_feature_expr(Cat.features_json)))


def visible_sightings_query(query, current_user: User | None):
    if can_see_demo_content(current_user):
        return query
    return query.filter(or_(Sighting.features_json.is_(None), ~is_demo_feature_expr(Sighting.features_json)))


def _ts(now: datetime, days_ago: float) -> datetime:
    return now - timedelta(days=days_ago)


def relocate_demo_content(db: Session, lat: float | None, lng: float | None) -> None:
    """Move demo-only cats around the reviewer's current location.

    App Review can run the app from anywhere. Keeping the demo pins near the
    current device position means the map/feed are populated without exposing
    those seeded cats to normal users.
    """
    if lat is None or lng is None:
        return
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return

    by_key = {str(item["key"]): item for item in CATS}
    changed = False

    cats = db.query(Cat).filter(is_demo_feature_expr(Cat.features_json)).all()
    for cat in cats:
        key = demo_feature_key(cat.features_json)
        if not key or not key.startswith("cat:"):
            continue
        item = by_key.get(key.removeprefix("cat:"))
        if not item:
            continue
        cat.last_lat = lat + item["dlat"]
        cat.last_lng = lng + item["dlng"]
        changed = True

    sightings = db.query(Sighting).filter(is_demo_feature_expr(Sighting.features_json)).all()
    for sighting in sightings:
        key = demo_feature_key(sighting.features_json)
        if not key or not key.startswith("sighting:"):
            continue
        _, cat_key, suffix = key.split(":", 2)
        item = by_key.get(cat_key)
        if not item:
            continue
        sighting.latitude = lat + item["dlat"] + (0.00012 if suffix == "latest" else -0.00008)
        sighting.longitude = lng + item["dlng"] + (-0.0001 if suffix == "latest" else 0.00009)
        changed = True

    posts = (
        db.query(ExplorerPost)
        .join(Sighting, ExplorerPost.sighting_id == Sighting.id)
        .filter(is_demo_feature_expr(Sighting.features_json))
        .all()
    )
    for post in posts:
        if post.sighting:
            post.latitude = post.sighting.latitude
            post.longitude = post.sighting.longitude
            changed = True

    if changed:
        db.commit()


def seed_apple_review_demo(db: Session) -> None:
    """Ensure the Apple review account and its private demo content exist.

    The seed is idempotent and scoped by DEMO_MARKER. Demo cats/sightings are
    filtered from normal public reads; only the demo account and admins see them.
    """
    now = datetime.now(timezone.utc)
    demo = db.query(User).filter(User.email == DEMO_EMAIL).first()
    if demo is None:
        demo = User(
            email=DEMO_EMAIL,
            hashed_password=hash_password(DEMO_PASSWORD),
            display_name="Millie",
            avatar_emoji="face:apple-review-demo",
            email_verified=True,
            is_active=True,
            notify_nearby_sightings=True,
            notify_new_cat_in_area=True,
            created_at=_ts(now, 90),
        )
        db.add(demo)
        db.flush()
    else:
        demo.hashed_password = hash_password(DEMO_PASSWORD)
        demo.display_name = demo.display_name or "Millie"
        demo.avatar_emoji = demo.avatar_emoji or "face:apple-review-demo"
        demo.email_verified = True
        demo.is_active = True
        demo.banned_at = None

    # Remove this seed's previous rows before recreating them. This keeps changes
    # to demo copy/data reflected after the next CI/CD deployment.
    demo_cat_ids = [
        row[0]
        for row in db.query(Cat.id)
        .filter(is_demo_feature_expr(Cat.features_json))
        .all()
    ]
    demo_sighting_ids = [
        row[0]
        for row in db.query(Sighting.id)
        .filter(is_demo_feature_expr(Sighting.features_json))
        .all()
    ]
    demo_post_ids = [
        row[0]
        for row in db.query(ExplorerPost.id)
        .filter(
            or_(
                ExplorerPost.sighting_id.in_(demo_sighting_ids) if demo_sighting_ids else False,
                ExplorerPost.cat_id.in_(demo_cat_ids) if demo_cat_ids else False,
            )
        )
        .all()
    ]
    if demo_post_ids:
        db.query(PostComment).filter(PostComment.post_id.in_(demo_post_ids)).delete(synchronize_session=False)
        db.query(PostMeow).filter(PostMeow.post_id.in_(demo_post_ids)).delete(synchronize_session=False)
        db.query(PostReport).filter(PostReport.post_id.in_(demo_post_ids)).delete(synchronize_session=False)
        db.query(Notification).filter(Notification.post_id.in_(demo_post_ids)).delete(synchronize_session=False)
        db.query(ExplorerPost).filter(ExplorerPost.id.in_(demo_post_ids)).delete(synchronize_session=False)
    if demo_sighting_ids:
        db.query(Notification).filter(Notification.sighting_id.in_(demo_sighting_ids)).delete(synchronize_session=False)
        db.query(Sighting).filter(Sighting.id.in_(demo_sighting_ids)).delete(synchronize_session=False)
    if demo_cat_ids:
        db.query(Notification).filter(Notification.cat_id.in_(demo_cat_ids)).delete(synchronize_session=False)
        db.query(CatClaim).filter(CatClaim.cat_id.in_(demo_cat_ids)).delete(synchronize_session=False)
        db.query(Cat).filter(Cat.id.in_(demo_cat_ids)).delete(synchronize_session=False)
    demo_tile_keys = {
        f"{q},{r}"
        for q in DEMO_TILE_Q_RANGE
        for r in DEMO_TILE_R_RANGE
    }
    db.query(ExploredTile).filter(
        ExploredTile.user_id == demo.id,
        ExploredTile.tile_key.in_(demo_tile_keys),
    ).delete(synchronize_session=False)
    db.query(Notification).filter(
        Notification.user_id == demo.id,
        Notification.type.like("demo_%"),
    ).delete(synchronize_session=False)
    db.flush()

    created_cats: list[Cat] = []
    for item in CATS:
        last_seen = _ts(now, item["last_days"])
        cat = Cat(
            name=item["name"],
            breed=item["breed"],
            sighting_count=2,
            first_seen=_ts(now, item["first_days"]),
            last_seen=last_seen,
            last_lat=BASE_LAT + item["dlat"],
            last_lng=BASE_LNG + item["dlng"],
            last_photo_path=PHOTO_URLS[item["photo"]],
            vibes=item["vibes"],
            is_cat=True,
            primary_color=item["primary_color"],
            secondary_color=item["secondary_color"],
            pattern=item["pattern"],
            fur_length=item["fur_length"],
            eye_color=item["eye_color"],
            body_size=item["body_size"],
            features_json=demo_features(f"cat:{item['key']}"),
        )
        cat.rarity_score = compute_rarity_score(cat.sighting_count, last_seen)
        db.add(cat)
        db.flush()
        created_cats.append(cat)

        for offset, suffix in ((0.0, "latest"), (item["first_days"] - item["last_days"], "first")):
            spotted_at = last_seen - timedelta(days=offset)
            sighting = Sighting(
                cat_id=cat.id,
                user_id=demo.id,
                photo_path=PHOTO_URLS[item["photo"]],
                latitude=cat.last_lat + (0.00012 if suffix == "latest" else -0.00008),
                longitude=cat.last_lng + (-0.0001 if suffix == "latest" else 0.00009),
                spotted_at=spotted_at,
                spotter_name=demo.display_name,
                breed_description=item["breed"],
                vibes=item["vibes"],
                is_cat=True,
                primary_color=item["primary_color"],
                secondary_color=item["secondary_color"],
                pattern=item["pattern"],
                fur_length=item["fur_length"],
                eye_color=item["eye_color"],
                body_size=item["body_size"],
                features_json=demo_features(f"sighting:{item['key']}:{suffix}"),
                frame_id=item["frame"] if suffix == "latest" else "classic",
                caption=item["caption"] if suffix == "latest" else None,
            )
            db.add(sighting)
            db.flush()
            if suffix == "latest":
                db.add(
                    ExplorerPost(
                        user_id=demo.id,
                        sighting_id=sighting.id,
                        cat_id=cat.id,
                        photo_path=sighting.photo_path,
                        caption=item["caption"],
                        latitude=sighting.latitude,
                        longitude=sighting.longitude,
                        created_at=spotted_at,
                    )
                )

    if created_cats:
        owned = created_cats[1]
        db.add(
            CatClaim(
                cat_id=owned.id,
                user_id=demo.id,
                status="verified",
                source="claim",
                real_name="Clementine",
                likes_petting=True,
                accepts_treats=True,
                age_years=3,
                fun_fact="Holds court on the front steps and accepts pastry crumbs as tribute.",
                indoor_outdoor="both",
                created_at=_ts(now, 45),
                decided_at=_ts(now, 44),
            )
        )
        demo.catalog_layout = json.dumps(
            {
                "order": [cat.id for cat in created_cats],
                "frames": {
                    str(cat.id): CATS[idx]["frame"]
                    for idx, cat in enumerate(created_cats)
                },
                "covers": {},
                "adjusts": {},
            }
        )

    for q in DEMO_TILE_Q_RANGE:
        for r in DEMO_TILE_R_RANGE:
            if (q + r) % 3 == 0:
                continue
            db.add(
                ExploredTile(
                    user_id=demo.id,
                    tile_key=f"{q},{r}",
                    is_home=False,
                    created_at=_ts(now, (q + r) % 60),
                )
            )

    db.commit()
    log.info("Apple review demo account/content seeded for %s", DEMO_EMAIL)
