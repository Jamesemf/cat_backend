"""The moderator side of the app: two review queues and the actions on them.

Every endpoint here is admin-only (is_admin, set directly in the DB — same gate
as catalog maintenance).

**Reported posts.** Reports arrive from users via POST /explorer/posts/{id}/report;
enough of them auto-hide a post (services/moderation.py). The three outcomes,
and why they're distinct:

  * dismiss  — the reports were wrong. Unhides, marks them reviewed. The post
               needs AUTO_HIDE_REPORT_COUNT *new* reporters to hide again.
  * hide     — withhold without deleting. Reversible, keeps the evidence, and
               is the right call while something is being judged.
  * remove   — the reports were right. Deletes the post *and* its sighting, so
               the photo leaves the map and the cat's history too, and unlinks
               the file. Not reversible.

Resolving a post always marks its open reports reviewed, so the queue drains.

**Ownership claims.** Nothing grants ownership of a cat except approve() below.
Claims and registrations both land as pending (routers/claims.py,
routers/cats.py) and wait here. The asymmetry with reports is deliberate: a
report queue exists to *release* content the machine withheld, whereas this
queue exists to *withhold* a grant until a person makes it. Ownership carries a
standing feed of where a real animal is being seen, so it is not something a
similarity score should hand out.
"""

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.db.session import get_db
from app.models.cat import Cat
from app.models.claim import CatClaim, ClaimPhoto
from app.models.explorer import ExplorerPost, PostReport
from app.models.notification import Notification
from app.models.trait_change import TraitChangeRequest
from app.models.user import User
from app.schemas.claim import (
    ClaimPhotoOut,
    ClaimQueueItem,
    ClaimReviewResult,
    RejectClaimIn,
)
from app.schemas.explorer import (
    ModerationActionResult,
    ReportedPostOut,
    ReportOut,
)
from app.schemas.trait_change import (
    TRAIT_FIELDS,
    RejectTraitChangeIn,
    TraitChangeApplyIn,
    TraitChangeQueueItem,
    TraitChangeResult,
    validate_trait_values,
)
from app.routers.media import serve_upload
from app.services.auth_service import require_admin
from app.services.content_deletion import purge_post, safe_unlink
from app.services.moderation import open_report_count
from app.services.push import push_to_user
from app.services.storage import UPLOADS_PREFIX

log = logging.getLogger(__name__)

# The seven controlled-vocabulary fields the reviewer compares by eye. Same set
# utils.matching weights for sighting Re-ID — but here they are shown, not scored.
# Also the set a trait change request may touch, which is where it now lives.
COMPARED_FEATURES = TRAIT_FIELDS

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


# --------------------------------------------------------------------------
# Ownership claims
# --------------------------------------------------------------------------


def _get_claim_or_404(db: Session, claim_id: int) -> CatClaim:
    """Load a claim for review.

    404 rather than 500 when it's gone: deleting an account removes its claims,
    and deleting a cat removes the claims against it, so a queue page held open
    can easily name a row that no longer exists.
    """
    claim = (
        db.query(CatClaim)
        .options(joinedload(CatClaim.cat), joinedload(CatClaim.user))
        .filter(CatClaim.id == claim_id)
        .first()
    )
    if not claim:
        raise HTTPException(status_code=404, detail="Claim not found")
    return claim


def _parsed_features(raw: str | None) -> dict:
    """A stored features_json blob as a dict, tolerating junk."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _features_of(raw: str | None) -> dict:
    """The compared subset of a stored features_json blob."""
    parsed = _parsed_features(raw)
    return {k: parsed.get(k) for k in COMPARED_FEATURES}


def _cat_count_of(raw: str | None) -> int | None:
    """How many cats vision saw in one photo, or None if it didn't say.

    Kept out of _features_of because that subset is also read as a cat's own
    attributes, where a per-photo count means nothing. It travels with claim
    photos because a claim may hold more than one cat now — the claimant's other
    cat, in shot at home — and when it does, the single row of features beside
    it could be describing either animal. Without the count the reviewer reads
    that row as a statement about the cat being claimed.
    """
    count = _parsed_features(raw).get("cat_count")
    return count if isinstance(count, int) else None


def _queue_item(db: Session, claim: CatClaim) -> ClaimQueueItem:
    photos = (
        db.query(ClaimPhoto)
        .filter(ClaimPhoto.claim_id == claim.id)
        .order_by(ClaimPhoto.id.asc())
        .all()
    )
    cat = claim.cat
    return ClaimQueueItem(
        claim_id=claim.id,
        source=claim.source,
        status=claim.status,
        created_at=claim.created_at,
        decided_at=claim.decided_at,
        claimant_id=claim.user_id,
        claimant_name=claim.user.display_name if claim.user else None,
        # Surfaced for the same reason ReportedPostOut carries author_strikes:
        # a history of harmful uploads is context for whether to trust a claim.
        claimant_strikes=claim.user.content_strikes if claim.user else 0,
        claimant_banned=bool(claim.user and claim.user.banned_at is not None),
        cat_id=claim.cat_id,
        cat_name=cat.name if cat else None,
        cat_photo_path=cat.last_photo_path if cat else None,
        cat_sighting_count=cat.sighting_count if cat else None,
        cat_features={k: getattr(cat, k, None) for k in COMPARED_FEATURES} if cat else {},
        proposed_name=claim.real_name,
        likes_petting=claim.likes_petting,
        accepts_treats=claim.accepts_treats,
        age_years=claim.age_years,
        fun_fact=claim.fun_fact,
        indoor_outdoor=claim.indoor_outdoor,
        photos=[
            ClaimPhotoOut(
                id=p.id,
                photo_path=p.photo_path,
                features=_features_of(p.features_json),
                cat_count=_cat_count_of(p.features_json),
            )
            for p in photos
        ],
        rejection_reason=claim.rejection_reason,
        reviewed_by_name=claim.reviewed_by.display_name if claim.reviewed_by else None,
    )


@router.get("/claims", response_model=list[ClaimQueueItem])
def list_claims(
    limit: int = 50,
    offset: int = 0,
    include_resolved: bool = False,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """The claim review queue, oldest first.

    Oldest-first rather than the report queue's worst-first: there is no
    severity here, only someone waiting. A claim that has been pending two days
    should be decided before one filed this morning, because nothing about their
    cat works until it is.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    q = db.query(CatClaim).options(
        joinedload(CatClaim.cat),
        joinedload(CatClaim.user),
        joinedload(CatClaim.reviewed_by),
    )
    if include_resolved:
        # "Everything a moderator has or could have touched" — self-revoked and
        # never-reviewed rows are noise in a history view.
        q = q.filter(CatClaim.status.in_(("pending", "verified", "rejected", "revoked")))
    else:
        q = q.filter(CatClaim.status == "pending")

    rows = q.order_by(CatClaim.created_at.asc()).offset(offset).limit(limit).all()

    out: list[ClaimQueueItem] = []
    for claim in rows:
        # A claim on a cat that has since been deleted has nothing to judge —
        # mirrors the reports queue skipping posts deleted out from under them.
        if claim.source == "claim" and claim.cat is None:
            continue
        out.append(_queue_item(db, claim))
    return out


@router.post("/claims/{claim_id}/approve", response_model=ClaimReviewResult)
def approve_claim(
    claim_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Grant ownership. The only path in the codebase that does.

    For a registration this is also where the Cat is finally created, from the
    photos and vision features held against the claim.
    """
    claim = _get_claim_or_404(db, claim_id)
    if claim.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"This claim was already {claim.status}."
        )

    now = datetime.now(timezone.utc)

    if claim.source == "register":
        photos = (
            db.query(ClaimPhoto)
            .filter(ClaimPhoto.claim_id == claim.id)
            .order_by(ClaimPhoto.id.asc())
            .all()
        )
        if not photos:
            raise HTTPException(
                status_code=409,
                detail="This registration has no photos left; reject it instead.",
            )
        cover = photos[0]
        feats = _features_of(cover.features_json)
        cat = Cat(
            name=claim.real_name,
            breed=feats.get("breed"),
            last_photo_path=cover.photo_path,
            sighting_count=0,
            rarity_score=0.0,
            first_seen=now,
            last_seen=now,
            is_cat=True,
            primary_color=feats.get("primary_color"),
            secondary_color=feats.get("secondary_color"),
            pattern=feats.get("pattern"),
            fur_length=feats.get("fur_length"),
            eye_color=feats.get("eye_color"),
            body_size=feats.get("body_size"),
            features_json=cover.features_json,
        )
        db.add(cat)
        db.flush()
        claim.cat_id = cat.id
    else:
        cat = claim.cat
        if cat is None:
            raise HTTPException(status_code=404, detail="That cat no longer exists.")
        # Re-check rather than trusting the partial unique index: a database
        # created before that index existed won't have it (create_all skips
        # pre-existing tables), so this is the only guaranteed guard.
        rival = (
            db.query(CatClaim)
            .filter(
                CatClaim.cat_id == cat.id,
                CatClaim.status == "verified",
                CatClaim.id != claim.id,
            )
            .first()
        )
        if rival:
            raise HTTPException(
                status_code=409, detail="This cat already has a verified owner."
            )
        # The owner knows the cat's actual name: it replaces the generated nickname.
        if claim.real_name:
            cat.name = claim.real_name

    claim.status = "verified"
    claim.reviewed_by_id = admin.id
    claim.decided_at = now

    # Rival claims on the same cat can't all be right, and leaving them pending
    # would show the next moderator a decision that is already made.
    losers = (
        db.query(CatClaim)
        .filter(
            CatClaim.cat_id == claim.cat_id,
            CatClaim.status == "pending",
            CatClaim.id != claim.id,
        )
        .all()
    )
    cat_label = cat.name or "this cat"
    pushes: list[tuple[int, str, str]] = []
    for loser in losers:
        loser.status = "rejected"
        loser.reviewed_by_id = admin.id
        loser.decided_at = now
        loser.rejection_reason = "Someone else was confirmed as this cat's owner."
        title = f"Your claim on {cat_label} wasn't approved"
        body = "Someone else was confirmed as this cat's owner."
        db.add(
            Notification(
                user_id=loser.user_id,
                type="claim_rejected",
                title=title,
                body=body,
                cat_id=claim.cat_id,
            )
        )
        pushes.append((loser.user_id, title, body))

    win_title = f"You're now {cat_label}'s verified owner"
    win_body = "Your claim was approved. You'll be notified whenever they're spotted."
    db.add(
        Notification(
            user_id=claim.user_id,
            type="claim_verified",
            title=win_title,
            body=win_body,
            cat_id=claim.cat_id,
        )
    )
    pushes.append((claim.user_id, win_title, win_body))

    try:
        db.commit()
    except IntegrityError:
        # Lost a race with a simultaneous approval on the same cat.
        db.rollback()
        raise HTTPException(status_code=409, detail="This cat already has a verified owner.")

    # Only once the decision is durable. Pushing first would tell a claimant
    # they own a cat that a rolled-back commit never gave them.
    cat_id = claim.cat_id
    for uid, title, body in pushes:
        background_tasks.add_task(push_to_user, uid, title, body, {"cat_id": cat_id})

    log.info(
        "Claim %s (%s) approved by moderator %s — cat %s, %d rival(s) rejected",
        claim.id, claim.source, admin.id, cat_id, len(losers),
    )
    return ClaimReviewResult(claim_id=claim.id, status=claim.status, cat_id=cat_id)


@router.post("/claims/{claim_id}/reject", response_model=ClaimReviewResult)
def reject_claim(
    claim_id: int,
    body: RejectClaimIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Turn a claim down. Nothing is deleted.

    A rejected registration leaves no cat behind because approval is what would
    have created one. The claim row itself is kept deliberately — the retry
    cooldown and the daily attempt cap are both computed by counting rows, so
    deleting it would hand the claimant a fresh budget and let them resubmit in
    a loop.
    """
    claim = _get_claim_or_404(db, claim_id)
    if claim.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"This claim was already {claim.status}."
        )

    reason = (body.reason or "").strip() or "Your photos weren't enough to confirm this is your cat."
    cat_label = claim.cat.name if claim.cat else (claim.real_name or "this cat")

    claim.status = "rejected"
    claim.rejection_reason = reason
    claim.reviewed_by_id = admin.id
    claim.decided_at = datetime.now(timezone.utc)

    title = f"Your claim on {cat_label} wasn't approved"
    db.add(
        Notification(
            user_id=claim.user_id,
            type="claim_rejected",
            title=title,
            body=reason,
            # Null for a registration: no cat was ever created, so there is
            # nowhere for the notification to deep-link to.
            cat_id=claim.cat_id,
        )
    )
    db.commit()

    background_tasks.add_task(
        push_to_user, claim.user_id, title, reason, {"cat_id": claim.cat_id}
    )
    log.info("Claim %s (%s) rejected by moderator %s", claim.id, claim.source, admin.id)
    return ClaimReviewResult(claim_id=claim.id, status=claim.status, cat_id=claim.cat_id)


@router.post("/claims/{claim_id}/revoke", response_model=ClaimReviewResult)
def revoke_claim(
    claim_id: int,
    body: RejectClaimIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Take ownership back off someone who already has it.

    The cat keeps whatever name the approval gave it — the nickname it had
    before was generated, and other people have been seeing this one since.
    Rename it by hand if the revocation was for a bogus name.
    """
    claim = _get_claim_or_404(db, claim_id)
    if claim.status != "verified":
        raise HTTPException(
            status_code=409, detail=f"This claim is {claim.status}, not verified."
        )

    reason = (body.reason or "").strip() or "Your ownership of this cat has been removed."
    cat_label = claim.cat.name if claim.cat else "this cat"

    claim.status = "revoked"
    claim.rejection_reason = reason
    claim.reviewed_by_id = admin.id
    claim.decided_at = datetime.now(timezone.utc)

    title = f"You're no longer {cat_label}'s verified owner"
    db.add(
        Notification(
            user_id=claim.user_id,
            type="claim_revoked",
            title=title,
            body=reason,
            cat_id=claim.cat_id,
        )
    )
    db.commit()

    background_tasks.add_task(
        push_to_user, claim.user_id, title, reason, {"cat_id": claim.cat_id}
    )
    log.warning(
        "Claim %s revoked by moderator %s (was owned by user %s)",
        claim.id, admin.id, claim.user_id,
    )
    return ClaimReviewResult(claim_id=claim.id, status=claim.status, cat_id=claim.cat_id)


# ---------------------------------------------------------------------------
# Trait change requests
#
# A cat's traits are written once by vision and never revisited, so this third
# queue is for people saying the record is wrong. Unlike the other two, the
# moderator doesn't merely accept or refuse: they submit the values themselves,
# seeded with the proposal. A well-meant but half-right suggestion is worth
# correcting rather than bouncing.
# ---------------------------------------------------------------------------


def _get_trait_request_or_404(db: Session, request_id: int) -> TraitChangeRequest:
    request = (
        db.query(TraitChangeRequest).filter(TraitChangeRequest.id == request_id).first()
    )
    if not request:
        raise HTTPException(status_code=404, detail="That request no longer exists.")
    return request


def _traits_of(raw: str | None) -> dict:
    """Parse a stored proposal, keeping only real trait keys."""
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {k: v for k, v in parsed.items() if k in TRAIT_FIELDS}


def _trait_item(
    request: TraitChangeRequest, owner_user_ids: set[int]
) -> TraitChangeQueueItem:
    cat = request.cat
    return TraitChangeQueueItem(
        request_id=request.id,
        status=request.status,
        created_at=request.created_at,
        decided_at=request.decided_at,
        requester_id=request.user_id,
        requester_name=request.user.display_name if request.user else None,
        # Surfaced for the same reason ReportedPostOut carries author_strikes:
        # a history of harmful uploads is context for whether to trust this.
        requester_strikes=request.user.content_strikes if request.user else 0,
        requester_banned=bool(request.user and request.user.banned_at is not None),
        is_owner=request.user_id in owner_user_ids,
        cat_id=request.cat_id,
        cat_name=cat.name if cat else None,
        cat_photo_path=cat.last_photo_path if cat else None,
        current={k: getattr(cat, k, None) for k in TRAIT_FIELDS} if cat else {},
        proposed=_traits_of(request.proposed_json),
        applied=_traits_of(request.applied_json) if request.applied_json else None,
        note=request.note,
        rejection_reason=request.rejection_reason,
        reviewed_by_name=request.reviewed_by.display_name if request.reviewed_by else None,
    )


@router.get("/trait-changes", response_model=list[TraitChangeQueueItem])
def list_trait_changes(
    limit: int = 50,
    offset: int = 0,
    include_resolved: bool = False,
    db: Session = Depends(get_db),
    _admin: User = Depends(require_admin),
):
    """The trait correction queue, oldest first.

    Oldest-first like the claims queue rather than worst-first like reports:
    there is no severity here, only people waiting on an answer.
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)

    q = db.query(TraitChangeRequest).options(
        joinedload(TraitChangeRequest.cat),
        joinedload(TraitChangeRequest.user),
        joinedload(TraitChangeRequest.reviewed_by),
    )
    if not include_resolved:
        q = q.filter(TraitChangeRequest.status == "pending")

    rows = (
        q.order_by(TraitChangeRequest.created_at.asc()).offset(offset).limit(limit).all()
    )
    # A request whose cat has since been deleted has nothing left to correct.
    rows = [r for r in rows if r.cat is not None]

    # Which of these requesters own the cat they're correcting, resolved in one
    # query rather than one per row. Ownership is a badge here, not authority.
    owner_user_ids: set[int] = set()
    if rows:
        owners = dict(
            db.query(CatClaim.cat_id, CatClaim.user_id)
            .filter(
                CatClaim.cat_id.in_({r.cat_id for r in rows}),
                CatClaim.status == "verified",
            )
            .all()
        )
        owner_user_ids = {r.user_id for r in rows if owners.get(r.cat_id) == r.user_id}

    return [_trait_item(r, owner_user_ids) for r in rows]


@router.post("/trait-changes/{request_id}/apply", response_model=TraitChangeResult)
def apply_trait_change(
    request_id: int,
    body: TraitChangeApplyIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Write the moderator's values onto the cat.

    `body.values` is what the moderator settled on, which is not necessarily
    what was proposed — the dashboard seeds the form with the suggestion and
    lets them fix it first. Both are kept on the row.

    `features_json` is deliberately left alone: it is the raw record of what
    vision saw, not a claim about what is currently true.
    """
    request = _get_trait_request_or_404(db, request_id)
    if request.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"This request was already {request.status}."
        )

    cat = db.query(Cat).filter(Cat.id == request.cat_id).first()
    if not cat:
        raise HTTPException(status_code=404, detail="That cat no longer exists.")

    values = body.values or {}
    validate_trait_values(values)

    applied = {k: v for k, v in values.items() if getattr(cat, k, None) != v}
    for field, value in applied.items():
        setattr(cat, field, value)

    request.status = "applied"
    request.applied_json = json.dumps(applied)
    request.reviewed_by_id = admin.id
    request.decided_at = datetime.now(timezone.utc)

    cat_label = cat.name or "a cat"
    title = f"Your suggestion for {cat_label} was applied"
    # A moderator can agree with the report and still change nothing, if someone
    # else fixed the record first. Saying "applied" then would be a small lie.
    message = (
        "Thanks — the traits you suggested are now on their profile."
        if applied
        else "Thanks for flagging it. A moderator checked, and the record was already right."
    )
    db.add(
        Notification(
            user_id=request.user_id,
            type="trait_change_applied",
            title=title,
            body=message,
            cat_id=cat.id,
        )
    )
    db.commit()

    # Only once the change is durable.
    background_tasks.add_task(
        push_to_user, request.user_id, title, message, {"cat_id": cat.id}
    )
    log.info(
        "Trait change %s applied by moderator %s (%s field(s) changed on cat %s)",
        request.id, admin.id, len(applied), cat.id,
    )
    return TraitChangeResult(request_id=request.id, status=request.status, cat_id=cat.id)


@router.post("/trait-changes/{request_id}/reject", response_model=TraitChangeResult)
def reject_trait_change(
    request_id: int,
    body: RejectTraitChangeIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    admin: User = Depends(require_admin),
):
    """Turn a suggestion down. The cat is left exactly as it was."""
    request = _get_trait_request_or_404(db, request_id)
    if request.status != "pending":
        raise HTTPException(
            status_code=409, detail=f"This request was already {request.status}."
        )

    reason = (body.reason or "").strip() or (
        "A moderator checked this cat's record and left it as it was."
    )
    cat_label = request.cat.name if request.cat else "this cat"

    request.status = "rejected"
    request.rejection_reason = reason
    request.reviewed_by_id = admin.id
    request.decided_at = datetime.now(timezone.utc)

    title = f"Your suggestion for {cat_label} wasn't applied"
    db.add(
        Notification(
            user_id=request.user_id,
            type="trait_change_rejected",
            title=title,
            body=reason,
            cat_id=request.cat_id,
        )
    )
    db.commit()

    background_tasks.add_task(
        push_to_user, request.user_id, title, reason, {"cat_id": request.cat_id}
    )
    log.info("Trait change %s rejected by moderator %s", request.id, admin.id)
    return TraitChangeResult(
        request_id=request.id, status=request.status, cat_id=request.cat_id
    )
