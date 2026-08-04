"""The moderator side of reporting: a review queue and the actions on it.

Every endpoint here is admin-only (is_admin, set directly in the DB — same gate
as catalog maintenance). Reports arrive from users via
POST /explorer/posts/{id}/report; enough of them auto-hide a post
(services/moderation.py). This router is where a human resolves what that
produced.

The three outcomes, and why they're distinct:

  * dismiss  — the reports were wrong. Unhides, marks them reviewed. The post
               needs AUTO_HIDE_REPORT_COUNT *new* reporters to hide again.
  * hide     — withhold without deleting. Reversible, keeps the evidence, and
               is the right call while something is being judged.
  * remove   — the reports were right. Deletes the post *and* its sighting, so
               the photo leaves the map and the cat's history too, and unlinks
               the file. Not reversible.

Resolving a post always marks its open reports reviewed, so the queue drains.
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.db.session import get_db
from app.models.explorer import ExplorerPost, PostReport
from app.models.user import User
from app.schemas.explorer import (
    ModerationActionResult,
    ReportedPostOut,
    ReportOut,
)
from app.routers.media import serve_upload
from app.services.auth_service import require_admin
from app.services.content_deletion import purge_post, safe_unlink
from app.services.moderation import open_report_count
from app.services.storage import UPLOADS_PREFIX

log = logging.getLogger(__name__)

router = APIRouter(prefix="/moderation", tags=["moderation"])


def _get_post_or_404(db: Session, post_id: int) -> ExplorerPost:
    post = db.query(ExplorerPost).filter(ExplorerPost.id == post_id).first()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")
    return post


def _resolve_reports(db: Session, post_id: int, moderator: User) -> int:
    """Mark every open report on a post as reviewed. Returns how many."""
    now = datetime.now(timezone.utc)
    return (
        db.query(PostReport)
        .filter(PostReport.post_id == post_id, PostReport.reviewed_at.is_(None))
        .update(
            {PostReport.reviewed_at: now, PostReport.reviewed_by_id: moderator.id},
            synchronize_session=False,
        )
    )


@router.get("/reports", response_model=list[ReportedPostOut])
def list_reported_posts(
    limit: int = 50,
    offset: int = 0,
    include_resolved: bool = False,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """The review queue: reported posts, most-reported first.

    Grouped by post rather than listed per report — five reports on one photo
    is one decision, not five. Defaults to open reports only; pass
    include_resolved=true to see history.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    counts = db.query(
        PostReport.post_id,
        func.count(PostReport.id).label("n"),
        func.max(PostReport.created_at).label("latest"),
    )
    if not include_resolved:
        counts = counts.filter(PostReport.reviewed_at.is_(None))
    rows = (
        counts.group_by(PostReport.post_id)
        # Worst first, then most recent — a post with five reports outranks a
        # newer one with a single report.
        .order_by(func.count(PostReport.id).desc(), func.max(PostReport.created_at).desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    if not rows:
        return []

    post_ids = [r[0] for r in rows]
    posts = {
        p.id: p
        for p in db.query(ExplorerPost)
        .options(joinedload(ExplorerPost.user))
        .filter(ExplorerPost.id.in_(post_ids))
        .all()
    }

    report_q = (
        db.query(PostReport)
        .options(joinedload(PostReport.reporter))
        .filter(PostReport.post_id.in_(post_ids))
    )
    if not include_resolved:
        report_q = report_q.filter(PostReport.reviewed_at.is_(None))
    reports_by_post: dict[int, list[PostReport]] = {}
    for r in report_q.order_by(PostReport.created_at.desc()).all():
        reports_by_post.setdefault(r.post_id, []).append(r)

    # Batched, because include_resolved=true means the grouped counts above
    # aren't the open ones.
    open_counts = dict(
        db.query(PostReport.post_id, func.count(PostReport.id))
        .filter(PostReport.post_id.in_(post_ids), PostReport.reviewed_at.is_(None))
        .group_by(PostReport.post_id)
        .all()
    )

    out: list[ReportedPostOut] = []
    for post_id, _n, _latest in rows:
        post = posts.get(post_id)
        if post is None:
            continue  # post deleted out from under its reports; nothing to review
        post_reports = reports_by_post.get(post_id, [])
        reason_counts: dict[str, int] = {}
        for r in post_reports:
            reason_counts[r.reason] = reason_counts.get(r.reason, 0) + 1
        out.append(
            ReportedPostOut(
                post_id=post.id,
                photo_path=post.photo_path,
                caption=post.caption,
                created_at=post.created_at,
                hidden=post.hidden_at is not None,
                hidden_reason=post.hidden_reason,
                author_id=post.user_id,
                author_name=post.user.display_name if post.user else None,
                author_strikes=post.user.content_strikes if post.user else 0,
                cat_id=post.cat_id,
                sighting_id=post.sighting_id,
                open_report_count=open_counts.get(post.id, 0),
                reasons=sorted(reason_counts, key=lambda k: -reason_counts[k]),
                reports=[
                    ReportOut(
                        id=r.id,
                        reason=r.reason,
                        detail=r.detail,
                        created_at=r.created_at,
                        reporter_id=r.reporter_id,
                        reporter_name=r.reporter.display_name if r.reporter else None,
                    )
                    for r in post_reports
                ],
            )
        )
    return out


@router.get("/photo")
def moderation_photo(
    key: str,
    _admin: User = Depends(require_admin),
):
    """Serve a reported photo to a moderator.

    The public /uploads/* route is deliberately unauthenticated — it serves the
    feed. But a review tool showing photos that have been *hidden* needs the
    admin check on the fetch itself, or hiding is undone by anyone who kept the
    URL. Reuses serve_upload for the traversal-safe path handling and the S3
    redirect rather than reimplementing either.
    """
    prefix = f"{UPLOADS_PREFIX}/"
    if not key.startswith(prefix):
        raise HTTPException(status_code=404, detail="Not found.")
    return serve_upload(key[len(prefix):])


@router.post("/posts/{post_id}/hide", response_model=ModerationActionResult)
def hide_post(
    post_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Withhold a post pending judgement, and close its open reports."""
    post = _get_post_or_404(db, post_id)
    if post.hidden_at is None:
        post.hidden_at = datetime.now(timezone.utc)
    # Overwrite any "auto_reports" — a human has now made this call.
    post.hidden_reason = "moderator"
    _resolve_reports(db, post.id, admin)
    db.commit()
    log.info("Post %s hidden by moderator %s", post.id, admin.id)
    return ModerationActionResult(
        post_id=post.id, hidden=True, open_report_count=open_report_count(db, post.id)
    )


@router.post("/posts/{post_id}/dismiss", response_model=ModerationActionResult)
def dismiss_reports(
    post_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Clear a post: unhide it and mark its reports reviewed.

    Deliberately also unhides posts a *moderator* hid, not just auto-hidden
    ones — this is the undo for both, and a second moderator overturning the
    first is a normal outcome.
    """
    post = _get_post_or_404(db, post_id)
    post.hidden_at = None
    post.hidden_reason = None
    resolved = _resolve_reports(db, post.id, admin)
    db.commit()
    log.info("Post %s cleared by moderator %s (%d reports dismissed)", post.id, admin.id, resolved)
    return ModerationActionResult(post_id=post.id, hidden=False, open_report_count=0)


@router.delete("/posts/{post_id}", status_code=204)
def remove_post(
    post_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Delete a reported post for real — the same teardown the author's own
    delete performs, so the sighting leaves the map, the cat's counters are
    repaired, and the photo file is unlinked once nothing references it.

    Removal does not strike or ban the author. Strikes come from vision's
    verdict on the image (services/moderation.py); acting on unverified reports
    would let a coordinated group inflict them. Ban by hand if it warrants it.
    """
    post = _get_post_or_404(db, post_id)
    author_id = post.user_id
    # purge_post deletes the reports along with the post, so there's nothing
    # left to mark reviewed — the queue row disappears with it.
    files_to_unlink = purge_post(db, post)
    db.commit()
    safe_unlink(db, files_to_unlink)
    log.warning("Post %s removed by moderator %s (author=%s)", post_id, admin.id, author_id)
