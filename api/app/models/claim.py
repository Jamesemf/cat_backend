from datetime import datetime, timezone

from sqlalchemy import Boolean, ForeignKey, Index, Integer, String, DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class CatClaim(Base):
    """An ownership claim on a cat, decided by a human moderator.

    Every claim lands as `pending` and stays there until an admin approves or
    rejects it in /moderation/claims. Nothing is granted by machine: vision runs
    on the submitted photos for content safety and to record what it saw, but the
    ownership decision is entirely a person's.

    Two sources feed the same queue. A `claim` names an existing cat; a `register`
    proposes a brand-new one, so its `cat_id` is null until approval creates the
    Cat from the evidence photos. At most one verified claim per cat, enforced by
    a partial unique index.
    """

    __tablename__ = "cat_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # Null while a `register` claim is pending — the cat doesn't exist yet, and
    # won't unless a moderator approves it.
    cat_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("cats.id"), nullable=True, index=True
    )
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # pending | verified | rejected | revoked
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    # claim (an existing cat) | register (a new cat, created on approval)
    source: Mapped[str] = mapped_column(
        String, default="claim", server_default="claim", nullable=False
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The moderator who decided it. Nulled when that account is deleted, mirroring
    # PostReport.reviewed_by_id.
    reviewed_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )

    # Owner card, shown publicly on the cat's profile
    real_name: Mapped[str | None] = mapped_column(String, nullable=True)
    likes_petting: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    accepts_treats: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    age_years: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fun_fact: Mapped[str | None] = mapped_column(Text, nullable=True)
    # indoor | outdoor | both
    indoor_outdoor: Mapped[str | None] = mapped_column(String, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    cat = relationship("Cat")
    # Two FKs point at users now, so both sides must name their column.
    user = relationship("User", foreign_keys=[user_id])
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_id])
    photos = relationship("ClaimPhoto", back_populates="claim")

    __table_args__ = (
        # Partial unique index: at most one verified claim per cat. The predicate
        # kwarg is dialect-specific — SQLAlchemy emits whichever matches the engine
        # and ignores the other, so both must be present for SQLite (dev) and
        # Postgres (prod) to enforce the constraint.
        Index(
            "ix_one_verified_claim_per_cat",
            "cat_id",
            unique=True,
            sqlite_where=text("status = 'verified'"),
            postgresql_where=text("status = 'verified'"),
        ),
    )


class ClaimPhoto(Base):
    """A photo submitted as evidence for a claim.

    `features_json` is what vision saw in this photo. It is not scored — the
    moderator reads it beside the cat's own recorded features and decides. For a
    `register` claim these photos are also the raw material the Cat is built from
    on approval, so they must outlive the review.
    """

    __tablename__ = "claim_photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    claim_id: Mapped[int] = mapped_column(Integer, ForeignKey("cat_claims.id"), nullable=False, index=True)
    photo_path: Mapped[str] = mapped_column(String, nullable=False)
    features_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    claim = relationship("CatClaim", back_populates="photos")
