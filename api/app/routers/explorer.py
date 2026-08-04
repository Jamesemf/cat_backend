import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.db.session import get_db
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
from app.services.content_deletion import purge_post, safe_unlink
from app.services.moderation import apply_report_threshold
from app.services.push import push_to_user

log = logging.getLogger(__name__)

router = APIRouter(prefix="/explorer", tags=["explorer"])

# Direct photo uploads to the Explorer feed were removed — posts are only
# created by mirroring camera sightings (see routers/sightings.py). This
# router just serves the feed and its interactions (meows/comments/reports).


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
                hidden=p.hidden_at is not None,
            )
        )
    return out


def _post_query(db: Session):
    return db.query(ExplorerPost).options(
        joinedload(ExplorerPost.user),
        joinedload(ExplorerPost.cat),
        joinedload(ExplorerPost.sighting).joinedload(Sighting.cat),
    )


def _visible(query, current_user: User | None):
    """Drop moderated-away posts. Admins see everything so they can review in
    place; the author keeps seeing their own hidden post (flagged as hidden)
    rather than watching it vanish with no explanation."""
    if current_user is not None and current_user.is_admin:
        return query
    if current_user is not None:
        return query.filter(
            or_(ExplorerPost.hidden_at.is_(None), ExplorerPost.user_id == current_user.id)
        )
    return query.filter(ExplorerPost.hidden_at.is_(None))


def _get_post_or_404(db: Session, post_id: int) -> ExplorerPost:
    post = db.query(ExplorerPost).filter(ExplorerPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


def _can_see(post: ExplorerPost, user: User | None) -> bool:
    """Row-level counterpart to _visible, for single-post lookups."""
    if post.hidden_at is None:
        return True
    return user is not None and (user.is_admin or post.user_id == user.id)


def _require_interactable(post: ExplorerPost) -> None:
    """Block meows and comments on hidden posts — engagement shouldn't keep
    accruing on content that's been pulled pending review."""
    if post.hidden_at is not None:
        raise HTTPException(status_code=403, detail="This post is hidden pending review.")


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
    query = _visible(_post_query(db), current_user)
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
    post = _visible(_post_query(db), current_user).filter(ExplorerPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return _serialize_posts(db, [post], current_user)[0]


@router.delete("/posts/{post_id}", status_code=204)
def delete_post(
    post_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """Delete the current user's post, and everything hanging off it.

    Photo files are unlinked only after the commit, and only when no surviving
    row still references them (see content_deletion.purge_post).
    """
    post = _get_post_or_404(db, post_id)
    if post.user_id is None or post.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="You can only delete your own posts.")

    files_to_unlink = purge_post(db, post)
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
    return success without creating another row.

    Enough distinct reports hide the post pending moderator review; the
    response doesn't say so, since confirming the threshold was reached would
    tell a brigade exactly how many accounts they need.
    """
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

    apply_report_threshold(db, post)
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
    _require_interactable(post)

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
                    # The standalone post screen is gone — taps land on the cat.
                    cat_id=post.cat_id,
                )
            )
            db.commit()
            background_tasks.add_task(
                push_to_user, post.user_id, title, notif_body, {"cat_id": post.cat_id}
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
    if not _can_see(post, current_user):
        raise HTTPException(status_code=404, detail="Post not found")
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
    _require_interactable(post)

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
                # The standalone post screen is gone — taps land on the cat.
                cat_id=post.cat_id,
            )
        )
        db.commit()
        background_tasks.add_task(
            push_to_user, post.user_id, title, notif_body, {"cat_id": post.cat_id}
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
