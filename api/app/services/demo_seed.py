"""The Apple App Review account and the procedural neighbourhood it lives in.

App Review signs in as DEMO_EMAIL and is taken through the ordinary new-user
flow: the intro carousel, profile setup, and the "do you own a cat?" step, then
the map asks them to set a home neighbourhood where they actually are. So the
account is reset to a pristine, un-onboarded state on *every* sign-in — a second
reviewer (or a resubmission) gets the same first-run experience without a deploy.

The neighbourhood they arrive in is seeded: six cats spotted by three procedural
neighbour accounts, one of which the review account already owns so the Verified
Owner badge is visible without them having to file a claim. A few of the others
carry an older sighting of the review account's own, so the Cat-a-log opens with
cats already in it — and two are left unspotted, so there's still something out
there to photograph. Every seeded row is tagged with DEMO_MARKER and filtered out
of public reads, so none of it reaches real users or the global stats.
"""

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models.cat import Cat
from app.models.claim import CatClaim, ClaimPhoto
from app.models.exploration import ExploredTile
from app.models.explorer import ExplorerPost, PostComment, PostMeow, PostReport
from app.models.notification import Notification, PushToken
from app.models.sighting import Sighting
from app.models.trait_change import TraitChangeRequest
from app.models.user import User
from app.services.auth_service import hash_password
from app.services.cat_merge import detach_cat_from_merge_requests
from app.services.content_deletion import purge_post, safe_unlink
from app.utils.matching import haversine_km
from app.utils.rarity import compute_rarity_score

log = logging.getLogger(__name__)

DEMO_EMAIL = "demo@catapp.uk"
DEMO_PASSWORD = "CatDemo123!"
DEMO_MARKER = "apple_review_demo"

# The procedural spotters who own the seeded cats, so the review account's own
# Cat-a-log starts empty and the cats are theirs to discover. They have no
# password and no social sub, so /auth/login can never authenticate as one.
NEIGHBOURS = [
    {"key": "nadia", "email": "nadia@neighbours.catapp.uk", "display_name": "Nadia", "joined_days": 240},
    {"key": "tom", "email": "tom@neighbours.catapp.uk", "display_name": "Tom", "joined_days": 180},
    {"key": "priya", "email": "priya@neighbours.catapp.uk", "display_name": "Priya", "joined_days": 130},
]
NEIGHBOUR_EMAILS = frozenset(str(n["email"]) for n in NEIGHBOURS)
# Every account belonging to this seed. Excluded from the public leaderboard so
# procedural spotters never outrank real ones.
DEMO_ACCOUNT_EMAILS = frozenset({DEMO_EMAIL}) | NEIGHBOUR_EMAILS

# Where the seed initially places the cats. Only ever seen if the reviewer's
# device reports no location at all — relocate_demo_content moves them to
# whichever neighbourhood the reviewer actually opens the app in.
BASE_LAT = 51.3198
BASE_LNG = -0.2409

# How far the reviewer has to be from the seeded pins before they're moved.
# Once the cats are in their neighbourhood they must stay put: re-anchoring on
# every /cats poll would drag the whole neighbourhood along behind a reviewer
# who walks down the street, and cats that follow you aren't a map. Comfortably
# larger than the home neighbourhood (2 rings of ~190 m hexes).
RELOCATE_TRIGGER_KM = 2.0

PHOTO_URLS = {
    "orange_tabby": "https://images.pexels.com/photos/25524459/pexels-photo-25524459.jpeg?auto=compress&cs=tinysrgb&w=1200",
    "ginger_white": "https://images.pexels.com/photos/20673054/pexels-photo-20673054.jpeg?auto=compress&cs=tinysrgb&w=1200",
    "grey_tabby": "https://images.pexels.com/photos/17127912/pexels-photo-17127912.jpeg?auto=compress&cs=tinysrgb&w=1200",
    "black_cat": "https://images.pexels.com/photos/18364269/pexels-photo-18364269.jpeg?auto=compress&cs=tinysrgb&w=1200",
    "calico": "https://images.pexels.com/photos/29020203/pexels-photo-29020203.jpeg?auto=compress&cs=tinysrgb&w=1200",
    "tuxedo": "https://images.pexels.com/photos/17218018/pexels-photo-17218018.jpeg?auto=compress&cs=tinysrgb&w=1200",
}

# `owner` names the NEIGHBOURS key whose account spotted the cat, or None for the
# review account's own cat. The offsets keep every cat inside the two rings of
# hexes the map clears around a new home (~800 m), so they all read as real cats
# rather than fogged mystery pins the moment the neighbourhood is set.
CATS = [
    {
        "key": "biscuit",
        "name": "Biscuit",
        "breed": "Orange Tabby",
        "photo": "orange_tabby",
        "owner": "nadia",
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
        # The review account's own cat: they arrive already owning her, so the
        # Verified Owner badge and owner card are visible without filing a claim.
        "owner": None,
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
        "owner": "tom",
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
        "owner": "priya",
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
        "owner": "nadia",
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
        "owner": "tom",
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

# Cats the review account spotted itself, a while back: its own (older) sighting
# of a neighbour's cat, so the Cat-a-log opens with a few cats already collected
# instead of an empty shelf. Each cat's two sightings share one photo, so this
# needs no extra imagery — it just hands the older one to the reviewer, which is
# also the more believable story (they saw it first, a neighbour saw it last).
# The cats left out are deliberate: they're on the map, not in the Cat-a-log, so
# there's still something to go and photograph.
REVIEWER_SPOTTED = frozenset({"biscuit", "wednesday", "mochi"})

# The cat the review account owns, and the owner-card copy shown on her profile.
OWNED_CAT_KEY = "clementine"
OWNED_CAT_CLAIM = {
    "real_name": "Clementine",
    "likes_petting": True,
    "accepts_treats": True,
    "age_years": 3,
    "fun_fact": "Holds court on the front steps and accepts pastry crumbs as tribute.",
    "indoor_outdoor": "both",
}


def is_demo_user(user: User | None) -> bool:
    return bool(user and user.email.lower() == DEMO_EMAIL)


def is_demo_account(user: User | None) -> bool:
    """The review account or one of its procedural neighbours."""
    return bool(user and user.email.lower() in DEMO_ACCOUNT_EMAILS)


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


def _sighting_offsets(item: dict, suffix: str) -> tuple[float, float]:
    """A seeded sighting's offset from its cat's pin — the two sightings of a cat
    sit a few metres either side of it so the cat's territory isn't a single point."""
    if suffix == "latest":
        return 0.00012, -0.0001
    return -0.00008, 0.00009


def relocate_demo_content(db: Session, lat: float | None, lng: float | None) -> None:
    """Move the seeded cats into the reviewer's neighbourhood.

    App Review can run the app from anywhere, so the pins follow them there once
    — and then stay: a request from within RELOCATE_TRIGGER_KM of where the cats
    already are is left alone, which is what stops them trailing the reviewer
    around after they've set a home neighbourhood.
    """
    if lat is None or lng is None:
        return
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return

    by_key = {str(item["key"]): item for item in CATS}
    cats = db.query(Cat).filter(is_demo_feature_expr(Cat.features_json)).all()
    if not cats:
        return

    # Where the seed currently sits, recovered from any one cat's known offset.
    # Already near the reviewer means the neighbourhood is set — leave it be.
    for cat in cats:
        key = demo_feature_key(cat.features_json)
        item = by_key.get(key.removeprefix("cat:")) if key and key.startswith("cat:") else None
        if not item or cat.last_lat is None or cat.last_lng is None:
            continue
        anchor_lat = cat.last_lat - float(item["dlat"])
        anchor_lng = cat.last_lng - float(item["dlng"])
        if haversine_km(lat, lng, anchor_lat, anchor_lng) <= RELOCATE_TRIGGER_KM:
            return
        break

    changed = False
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
        slat, slng = _sighting_offsets(item, suffix)
        sighting.latitude = lat + item["dlat"] + slat
        sighting.longitude = lng + item["dlng"] + slng
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


def _purge_seeded_content(db: Session) -> None:
    """Delete every row this seed created, so it can be laid down again.

    Keyed entirely on DEMO_MARKER, so content the reviewer produced themselves is
    left for _purge_review_account_content to handle.
    """
    demo_cat_ids = [
        row[0]
        for row in db.query(Cat.id).filter(is_demo_feature_expr(Cat.features_json)).all()
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
        claim_ids = [
            row[0] for row in db.query(CatClaim.id).filter(CatClaim.cat_id.in_(demo_cat_ids)).all()
        ]
        if claim_ids:
            db.query(ClaimPhoto).filter(ClaimPhoto.claim_id.in_(claim_ids)).delete(synchronize_session=False)
            db.query(CatClaim).filter(CatClaim.id.in_(claim_ids)).delete(synchronize_session=False)
        # Moderation requests a person filed *about* a seeded cat. Not seed rows,
        # but they point at one, and cat_id on a trait request is NOT NULL — left
        # behind, they don't merely dangle, they make the delete below fail and
        # take startup down with it on Postgres. Handled exactly as delete_cat
        # does: suggestions go, duplicate reports are let go of.
        db.query(TraitChangeRequest).filter(
            TraitChangeRequest.cat_id.in_(demo_cat_ids)
        ).delete(synchronize_session=False)
        for cat_id in demo_cat_ids:
            detach_cat_from_merge_requests(db, cat_id)
        db.query(Cat).filter(Cat.id.in_(demo_cat_ids)).delete(synchronize_session=False)
    db.flush()


def _purge_review_account_content(db: Session, demo: User) -> list[str]:
    """Delete everything the reviewer themselves produced.

    Photos they took, cats they registered, claims they filed, tiles they walked.
    Without this the next reviewer inherits the last one's session, and — because
    a reviewer's own uploads carry no DEMO_MARKER — their test cats would show up
    on real users' maps. Returns upload keys to unlink after the commit.
    """
    files: list[str] = []

    # purge_post also removes the underlying sighting and repairs (or deletes)
    # its cat, which is what keeps a reviewer's test cat from outliving them.
    for post in db.query(ExplorerPost).filter(ExplorerPost.user_id == demo.id).all():
        files.extend(purge_post(db, post))
    db.flush()

    # Any sighting with no mirror post (only possible if the backfill hasn't run
    # for it yet) would otherwise survive the sweep above.
    for sighting in db.query(Sighting).filter(Sighting.user_id == demo.id).all():
        db.query(Notification).filter(Notification.sighting_id == sighting.id).delete(
            synchronize_session=False
        )
        files.append(sighting.photo_path)
        db.delete(sighting)
    db.flush()

    claim_ids = [row[0] for row in db.query(CatClaim.id).filter(CatClaim.user_id == demo.id).all()]
    if claim_ids:
        files.extend(
            row[0]
            for row in db.query(ClaimPhoto.photo_path)
            .filter(ClaimPhoto.claim_id.in_(claim_ids))
            .all()
        )
        db.query(ClaimPhoto).filter(ClaimPhoto.claim_id.in_(claim_ids)).delete(synchronize_session=False)
        db.query(CatClaim).filter(CatClaim.id.in_(claim_ids)).delete(synchronize_session=False)

    db.query(ExploredTile).filter(ExploredTile.user_id == demo.id).delete(synchronize_session=False)
    db.query(Notification).filter(Notification.user_id == demo.id).delete(synchronize_session=False)
    db.query(PostMeow).filter(PostMeow.user_id == demo.id).delete(synchronize_session=False)
    db.query(PostComment).filter(PostComment.user_id == demo.id).delete(synchronize_session=False)
    db.query(PostReport).filter(PostReport.reporter_id == demo.id).delete(synchronize_session=False)
    db.query(PushToken).filter(PushToken.user_id == demo.id).delete(synchronize_session=False)
    db.flush()
    return files


def _upsert_review_account(db: Session, now: datetime) -> User:
    """The review account itself, in its pristine un-onboarded state.

    No display name, no avatar, no Cat-a-log arrangement and onboarded_at null,
    so the app routes the next sign-in through the intro carousel and profile
    setup exactly as it would a brand-new user.
    """
    demo = db.query(User).filter(User.email == DEMO_EMAIL).first()
    if demo is None:
        demo = User(
            email=DEMO_EMAIL,
            hashed_password=hash_password(DEMO_PASSWORD),
            email_verified=True,
            is_active=True,
            notify_nearby_sightings=True,
            notify_new_cat_in_area=True,
            created_at=_ts(now, 90),
        )
        db.add(demo)
        db.flush()
        return demo

    demo.hashed_password = hash_password(DEMO_PASSWORD)
    demo.email_verified = True
    demo.is_active = True
    demo.banned_at = None
    demo.content_strikes = 0
    demo.display_name = None
    demo.avatar_emoji = None
    demo.catalog_layout = None
    demo.onboarded_at = None
    # PUT /auth/me refuses a second name change within 30 days, which would 429
    # the reviewer's profile-setup step on every reset but the first.
    demo.display_name_updated_at = None
    return demo


def _upsert_neighbours(db: Session, now: datetime) -> dict[str, User]:
    """The procedural spotters who own the seeded cats, keyed by NEIGHBOURS key."""
    out: dict[str, User] = {}
    for item in NEIGHBOURS:
        email = str(item["email"])
        user = db.query(User).filter(User.email == email).first()
        if user is None:
            user = User(
                email=email,
                # No password and no social sub: unauthenticatable by design.
                hashed_password=None,
                email_verified=True,
                is_active=True,
                created_at=_ts(now, float(item["joined_days"])),
            )
            db.add(user)
        user.display_name = str(item["display_name"])
        user.avatar_emoji = f"face:{DEMO_MARKER}-{item['key']}"
        user.banned_at = None
        # They never onboard through the app, but a null here would make them
        # look un-onboarded to anything that checks.
        user.onboarded_at = user.onboarded_at or _ts(now, float(item["joined_days"]))
        db.flush()
        out[str(item["key"])] = user
    return out


def _seed_neighbourhood(db: Session, demo: User, now: datetime) -> None:
    """Lay down the six cats, their two sightings each, and their Explorer posts.

    Who spotted what decides what the reviewer sees where: the newest sighting of
    a cat is always its owner's (so it drives the feed and the cat's last-seen),
    while the older one belongs to the review account for the REVIEWER_SPOTTED
    cats — that's what fills their Cat-a-log.
    """
    neighbours = _upsert_neighbours(db, now)

    for item in CATS:
        owner = demo if item["owner"] is None else neighbours[str(item["owner"])]
        last_seen = _ts(now, float(item["last_days"]))
        cat = Cat(
            name=item["name"],
            breed=item["breed"],
            sighting_count=2,
            first_seen=_ts(now, float(item["first_days"])),
            last_seen=last_seen,
            last_lat=BASE_LAT + float(item["dlat"]),
            last_lng=BASE_LNG + float(item["dlng"]),
            last_photo_path=PHOTO_URLS[str(item["photo"])],
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

        for offset, suffix in ((0.0, "latest"), (float(item["first_days"]) - float(item["last_days"]), "first")):
            spotted_at = last_seen - timedelta(days=offset)
            slat, slng = _sighting_offsets(item, suffix)
            # The older sighting of a REVIEWER_SPOTTED cat is the review
            # account's own, which is what puts that cat in their Cat-a-log —
            # /cats/mine is built from the sightings you took, not the cats you
            # own. The newest one stays the neighbour's, so the feed and the
            # cat's "last seen by" still belong to somebody else.
            spotter = demo if suffix == "first" and item["key"] in REVIEWER_SPOTTED else owner
            sighting = Sighting(
                cat_id=cat.id,
                user_id=spotter.id,
                photo_path=PHOTO_URLS[str(item["photo"])],
                latitude=cat.last_lat + slat,
                longitude=cat.last_lng + slng,
                spotted_at=spotted_at,
                # Resolved live from the spotter's profile on read, so the review
                # account's own sightings pick up whatever name they choose.
                spotter_name=spotter.display_name,
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
                        user_id=owner.id,
                        sighting_id=sighting.id,
                        cat_id=cat.id,
                        photo_path=sighting.photo_path,
                        caption=item["caption"],
                        latitude=sighting.latitude,
                        longitude=sighting.longitude,
                        created_at=spotted_at,
                    )
                )
        if item["key"] == OWNED_CAT_KEY:
            db.add(
                CatClaim(
                    cat_id=cat.id,
                    user_id=demo.id,
                    status="verified",
                    source="claim",
                    created_at=_ts(now, 45),
                    decided_at=_ts(now, 44),
                    **OWNED_CAT_CLAIM,
                )
            )


def seed_apple_review_demo(db: Session) -> None:
    """Ensure the review account and its seeded neighbourhood exist.

    Runs at startup, so editing the copy or the cats above and redeploying is
    enough to refresh what App Review sees. Idempotent: everything this seed
    owns is keyed by DEMO_MARKER and replaced wholesale.
    """
    now = datetime.now(timezone.utc)
    demo = _upsert_review_account(db, now)
    _purge_seeded_content(db)
    _seed_neighbourhood(db, demo, now)
    db.commit()
    log.info("Apple review demo account/content seeded for %s", DEMO_EMAIL)


def reset_apple_review_demo(db: Session, demo: User) -> None:
    """Return the review account to its first-run state. Called on every sign-in.

    Wipes what the last reviewer did — their profile, their photos, the ground
    they walked — and lays the seeded neighbourhood down again, so each sign-in
    starts at the intro carousel with a fresh map. Best-effort: a failure here
    must not cost App Review their login, so the caller swallows it.
    """
    now = datetime.now(timezone.utc)
    # Seeded rows first, so the sweep below only ever sees the reviewer's own
    # leftovers — and never tries to unlink a seeded stock photo.
    _purge_seeded_content(db)
    files = _purge_review_account_content(db, demo)
    demo = _upsert_review_account(db, now)
    _seed_neighbourhood(db, demo, now)
    db.commit()
    # Only after the commit, and only keys nothing else still points at.
    safe_unlink(db, [f for f in files if f])
    log.info("Apple review demo account reset for %s", DEMO_EMAIL)
