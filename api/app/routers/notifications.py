from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session, joinedload

from app.db.session import get_db
from app.models.notification import Notification, PushToken
from app.models.user import User
from app.schemas.notification import MarkReadIn, NotificationOut, PushTokenIn, UnreadCount
from app.services.auth_service import get_current_user

router = APIRouter(prefix="/notifications", tags=["notifications"])


@router.get("", response_model=list[NotificationOut])
def list_notifications(
    limit: int = 30,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (
        db.query(Notification)
        .options(joinedload(Notification.cat), joinedload(Notification.sighting))
        .filter(Notification.user_id == current_user.id)
        .order_by(Notification.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    return [
        NotificationOut(
            id=n.id,
            type=n.type,
            title=n.title,
            body=n.body,
            cat_id=n.cat_id,
            sighting_id=n.sighting_id,
            post_id=n.post_id,
            created_at=n.created_at,
            read_at=n.read_at,
            cat_name=n.cat.name if n.cat else None,
            cat_photo_path=n.cat.last_photo_path if n.cat else None,
            latitude=n.sighting.latitude if n.sighting else None,
            longitude=n.sighting.longitude if n.sighting else None,
        )
        for n in rows
    ]


@router.get("/unread-count", response_model=UnreadCount)
def unread_count(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = (
        db.query(Notification)
        .filter(Notification.user_id == current_user.id, Notification.read_at.is_(None))
        .count()
    )
    return UnreadCount(count=count)


@router.post("/mark-read")
def mark_read(
    body: MarkReadIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    query = db.query(Notification).filter(
        Notification.user_id == current_user.id, Notification.read_at.is_(None)
    )
    if not body.all:
        if not body.ids:
            return {"updated": 0}
        query = query.filter(Notification.id.in_(body.ids))
    updated = query.update(
        {Notification.read_at: datetime.now(timezone.utc)}, synchronize_session=False
    )
    db.commit()
    return {"updated": updated}


@router.post("/push-token", status_code=204)
def register_push_token(
    body: PushTokenIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    now = datetime.now(timezone.utc)
    existing = db.query(PushToken).filter(PushToken.token == body.token).first()
    if existing:
        # Device changed hands (new sign-in): reassign to the current user.
        existing.user_id = current_user.id
        existing.platform = body.platform or existing.platform
        existing.last_used_at = now
    else:
        db.add(
            PushToken(
                user_id=current_user.id,
                token=body.token,
                platform=body.platform,
                created_at=now,
                last_used_at=now,
            )
        )
    db.commit()


@router.delete("/push-token", status_code=204)
def remove_push_token(
    body: PushTokenIn,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    db.query(PushToken).filter(
        PushToken.token == body.token, PushToken.user_id == current_user.id
    ).delete(synchronize_session=False)
    db.commit()
