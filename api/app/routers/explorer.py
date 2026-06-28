import logging
from datetime import date
from pathlib import Path

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
)
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.db.session import get_db
from app.models.cat import Cat
from app.models.explorer import ExplorerPost, PostComment, PostMeow, PostReport
from app.models.notification import Notification
from app.models.sighting import Sighting
from app.models.user import User
from app.schemas.explorer import (
    REPORT_REASONS,
    CommentCreate,
    CommentOut,
    ExplorerPostOut,
    MeowResult,
    ReportCreate,
)
from app.services.auth_service import get_current_user, get_optional_user
from app.services.content_deletion import (
    delete_post_dependents,
    recompute_cat_after_sighting_removal,
    safe_unlink,
)
from app.services.push import push_to_user
from app.services.storage import get_storage
from app.services.vision import VisionError, moderate_explorer_photo

log = logging.getLogger(__name__)

router = APIRouter(prefix="/explorer", tags=["explorer"])

# Same in-memory abuse protection as sightings: resets on restart, single-process only.
MAX_PHOTO_BYTES = 5 * 1024 * 1024
MAX_POSTS_PER_IP_PER_DAY = 50
_rate_limit_buckets: dict[str, tuple[str, int]] = {}

# Friendly fallbacks when the model rejects without a displayable sentence.
REJECTION_MESSAGES = {
    "not_a_cat": "We couldn't spot a cat in this photo. The Explorer is cats only!",
    "animal_harm": "This photo appears to show an animal in distress and can't be posted.",
    "violence_or_gore": "This photo contains violent content and can't be posted.",
    "nsfw": "This photo contains adult content and can't be posted.",
    "hate_or_harassment": "This photo contains hateful content and can't be posted.",
    "private_information": "This photo appears to contain private information and can't be posted.",
    "other_inappropriate": "This photo isn't suitable for the Explorer feed.",
}


def _enforce_rate_limit(client_ip: str) -> None:
    today = date.today().isoformat()
    bucket_date, count = _rate_limit_buckets.get(client_ip, (today, 0))
    if bucket_date != today:
        count = 0
    if count >= MAX_POSTS_PER_IP_PER_DAY:
        raise HTTPException(
            status_code=429,
            detail=f"Daily limit of {MAX_POSTS_PER_IP_PER_DAY} posts reached for this client.",
        )
    _rate_limit_buckets[client_ip] = (today, count + 1)


def _serialize_posts(
    db: Session, posts: list[ExplorerPost], current_user: User | None
) -> list[ExplorerPostOut]:
    """Build feed items with batched meow/comment counts (no N+1)."""
    post_ids = [p.id for p in posts]
    meow_counts: dict[int, int] = {}
    comment_counts: dict[int, int] = {}
    my_meows: set[int] = set()
    if post_ids:
        meow_counts = dict(
            db.query(PostMeow.post_id, func.count(PostMeow.id))
            .filter(PostMeow.post_id.in_(post_ids))
            .group_by(PostMeow.post_id)
            .all()
        )
        comment_counts = dict(
            db.query(PostComment.post_id, func.count(PostComment.id))
            .filter(PostComment.post_id.in_(post_ids))
            .group_by(PostComment.post_id)
            .all()
        )
        if current_user:
            my_meows = {
                row[0]
                for row in db.query(PostMeow.post_id)
                .filter(PostMeow.post_id.in_(post_ids), PostMeow.user_id == current_user.id)
                .all()
            }

    out: list[ExplorerPostOut] = []
    for p in posts:
        cat = p.cat or (p.sighting.cat if p.sighting else None)
        out.append(
            ExplorerPostOut(
                id=p.id,
                photo_path=p.photo_path,
                caption=p.caption,
                created_at=p.created_at,
                latitude=p.latitude,
                longitude=p.longitude,
                user_id=p.user_id,
                user_name=p.user.display_name if p.user else None,
                user_emoji=p.user.avatar_emoji if p.user else None,
                sighting_id=p.sighting_id,
                cat_id=cat.id if cat else None,
                cat_name=cat.name if cat else None,
                meow_count=meow_counts.get(p.id, 0),
                comment_count=comment_counts.get(p.id, 0),
                meowed_by_me=p.id in my_meows,
                is_mine=current_user is not None and p.user_id == current_user.id,
            )
        )
    return out


def _post_query(db: Session):
    return db.query(ExplorerPost).options(
        joinedload(ExplorerPost.user),
        joinedload(ExplorerPost.cat),
        joinedload(ExplorerPost.sighting).joinedload(Sighting.cat),
    )


def _get_post_or_404(db: Session, post_id: int) -> ExplorerPost:
    post = db.query(ExplorerPost).filter(ExplorerPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


@router.get("/feed", response_model=list[ExplorerPostOut])
def get_explorer_feed(
    limit: int = 10,
    before_id: int | None = None,
    cat_id: int | None = None,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    """Newest-first Explorer feed with cursor pagination.

    Pass the id of the last post you received as before_id to get the next
    page. A cursor (rather than offset) keeps pages stable while new posts
    are being created above.

    cat_id restricts to one cat's posts (its profile grid). A post belongs
    to a cat either via its direct tag or through its originating sighting.
    """
    limit = max(1, min(limit, 30))
    query = _post_query(db)
    if before_id is not None:
        query = query.filter(ExplorerPost.id < before_id)

    if cat_id is not None:
        query = query.outerjoin(Sighting, ExplorerPost.sighting_id == Sighting.id).filter(
            or_(ExplorerPost.cat_id == cat_id, Sighting.cat_id == cat_id)
        )

    posts = query.order_by(ExplorerPost.id.desc()).limit(limit).all()
    return _serialize_posts(db, posts, current_user)


@router.get("/posts/{post_id}", response_model=ExplorerPostOut)
def get_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    post = _post_query(db).filter(ExplorerPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return _serialize_posts(db, [post], current_user)[0]


@router.post("/posts", response_model=ExplorerPostOut, status_code=201)
async def create_post(
    request: Request,
    photo: UploadFile = File(...),
    caption: str | None = Form(None),
    latitude: float | None = Form(None),
    longitude: float | None = Form(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Upload a photo directly to the Explorer feed.

    Location is optional — posts without coordinates simply never appear on
    the map. Every upload is moderated by Claude vision before it is saved:
    the photo must contain a cat and must not contain harmful content. Posts
    are not tagged to a cat profile; this flow is just for sharing photos.
    """
    client_ip = request.client.host if request.client else "unknown"
    _enforce_rate_limit(client_ip)

    contents = await photo.read()
    if len(contents) > MAX_PHOTO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Photo exceeds {MAX_PHOTO_BYTES // 1024 // 1024}MB limit.",
        )

    if caption is not None:
        caption = caption.strip() or None
    if caption and len(caption) > 280:
        raise HTTPException(status_code=400, detail="Caption must be 280 characters or fewer.")

    try:
        verdict = await moderate_explorer_photo(contents)
    except VisionError as exc:
        log.warning("Explorer moderation failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail="Photo checks are temporarily unavailable. Please try again.",
        )

    if not verdict.accepted:
        reason = verdict.rejection_reason or ("not_a_cat" if not verdict.is_cat else "other_inappropriate")
        detail = verdict.reason_detail or REJECTION_MESSAGES.get(
            reason, REJECTION_MESSAGES["other_inappropriate"]
        )
        raise HTTPException(status_code=400, detail=detail)

    ext = Path(photo.filename).suffix if photo.filename else ".jpg"
    photo_path = get_storage().put(contents, ext=ext)

    post = ExplorerPost(
        user_id=current_user.id,
        photo_path=photo_path,
        caption=caption,
        latitude=latitude,
        longitude=longitude,
    )
    db.add(post)
    db.commit()
    db.refresh(post)
    return _serialize_posts(db, [post], current_user)[0]


@router.delete("/posts/{post_id}", status_code=204)
def delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete the current user's post, and everything hanging off it.

    Sighting-originated posts also delete the underlying sighting (otherwise
    the startup backfill would resurrect the post) and repair the cat's
    counters. Photo files are unlinked only after the commit, and only when
    no surviving row still references them.
    """
    post = _get_post_or_404(db, post_id)
    if post.user_id is None or post.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own posts.")

    files_to_unlink: list[str] = []
    delete_post_dependents(db, [post.id])

    if post.sighting_id is not None:
        sighting = db.query(Sighting).filter(Sighting.id == post.sighting_id).first()
        db.delete(post)
        if sighting:
            db.query(Notification).filter(Notification.sighting_id == sighting.id).delete(
                synchronize_session=False
            )
            cat = sighting.cat
            files_to_unlink.append(sighting.photo_path)
            db.delete(sighting)
            db.flush()
            if cat:
                files_to_unlink.extend(
                    recompute_cat_after_sighting_removal(db, cat, sighting.photo_path)
                )
    else:
        # Direct upload: the post owns its photo. The "my new cat" flow may
        # have set it as the cat's profile photo — clear that reference.
        db.query(Cat).filter(Cat.last_photo_path == post.photo_path).update(
            {Cat.last_photo_path: None}, synchronize_session=False
        )
        files_to_unlink.append(post.photo_path)
        db.delete(post)

    db.commit()
    safe_unlink(db, files_to_unlink)


@router.post("/posts/{post_id}/report", status_code=201)
def report_post(
    post_id: int,
    body: ReportCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """File a report against a post. Idempotent per user — repeat reports
    return success without creating another row."""
    post = _get_post_or_404(db, post_id)
    if body.reason not in REPORT_REASONS:
        raise HTTPException(status_code=400, detail="Unknown report reason.")
    if post.user_id == current_user.id:
        raise HTTPException(status_code=400, detail="You can't report your own post.")

    db.add(
        PostReport(
            post_id=post.id,
            reporter_id=current_user.id,
            reason=body.reason,
            detail=body.detail,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()  # already reported by this user — treat as success
    return {"status": "reported"}


@router.post("/posts/{post_id}/meow", response_model=MeowResult)
def toggle_meow(
    post_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Toggle the current user's meow on a post."""
    post = _get_post_or_404(db, post_id)

    existing = (
        db.query(PostMeow)
        .filter(PostMeow.post_id == post_id, PostMeow.user_id == current_user.id)
        .first()
    )
    if existing:
        db.delete(existing)
        db.commit()
        meowed = False
    else:
        db.add(PostMeow(post_id=post_id, user_id=current_user.id))
        try:
            db.commit()
        except IntegrityError:
            # Double-tap race: another request already inserted the meow.
            db.rollback()
        meowed = True

        if post.user_id and post.user_id != current_user.id:
            who = current_user.display_name or "Someone"
            title = "Meow!"
            notif_body = f"{who} meowed at your cat post."
            db.add(
                Notification(
                    user_id=post.user_id,
                    type="meow",
                    title=title,
                    body=notif_body,
                    post_id=post.id,
                )
            )
            db.commit()
            background_tasks.add_task(
                push_to_user, post.user_id, title, notif_body, {"post_id": post.id}
            )

    meow_count = db.query(PostMeow).filter(PostMeow.post_id == post_id).count()
    return MeowResult(meowed=meowed, meow_count=meow_count)


@router.get("/posts/{post_id}/comments", response_model=list[CommentOut])
def list_comments(
    post_id: int,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User | None = Depends(get_optional_user),
):
    post = _get_post_or_404(db, post_id)
    rows = (
        db.query(PostComment)
        .options(joinedload(PostComment.user))
        .filter(PostComment.post_id == post_id)
        .order_by(PostComment.created_at.asc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        CommentOut(
            id=c.id,
            post_id=c.post_id,
            body=c.body,
            created_at=c.created_at,
            user_id=c.user_id,
            user_name=c.user.display_name if c.user else None,
            user_emoji=c.user.avatar_emoji if c.user else None,
            can_delete=bool(
                current_user
                and (c.user_id == current_user.id or post.user_id == current_user.id)
            ),
        )
        for c in rows
    ]


@router.delete("/posts/{post_id}/comments/{comment_id}", status_code=204)
def delete_comment(
    post_id: int,
    comment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete a comment — allowed for its author or the post's owner."""
    post = _get_post_or_404(db, post_id)
    comment = (
        db.query(PostComment)
        .filter(PostComment.id == comment_id, PostComment.post_id == post_id)
        .first()
    )
    if not comment:
        raise HTTPException(status_code=404, detail="Comment not found")
    if comment.user_id != current_user.id and post.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can't delete this comment.")
    db.delete(comment)
    db.commit()


@router.post("/posts/{post_id}/comments", response_model=CommentOut, status_code=201)
def create_comment(
    post_id: int,
    body: CommentCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    post = _get_post_or_404(db, post_id)

    comment = PostComment(post_id=post_id, user_id=current_user.id, body=body.body.strip())
    db.add(comment)
    db.commit()
    db.refresh(comment)

    if post.user_id and post.user_id != current_user.id:
        who = current_user.display_name or "Someone"
        title = "New comment"
        snippet = comment.body if len(comment.body) <= 80 else comment.body[:77] + "..."
        notif_body = f'{who} commented on your cat post: "{snippet}"'
        db.add(
            Notification(
                user_id=post.user_id,
                type="comment",
                title=title,
                body=notif_body,
                post_id=post.id,
            )
        )
        db.commit()
        background_tasks.add_task(
            push_to_user, post.user_id, title, notif_body, {"post_id": post.id}
        )

    return CommentOut(
        id=comment.id,
        post_id=comment.post_id,
        body=comment.body,
        created_at=comment.created_at,
        user_id=current_user.id,
        user_name=current_user.display_name,
        user_emoji=current_user.avatar_emoji,
        can_delete=True,
    )
