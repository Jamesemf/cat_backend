from datetime import datetime, timezone

from sqlalchemy import Boolean, Float, ForeignKey, Index, Integer, String, DateTime, Text, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class CatClaim(Base):
    """An ownership claim on a cat. Verified by matching owner-submitted photos
    against the cat's stored vision features. At most one verified claim per cat,
    enforced by a partial unique index."""

    __tablename__ = "cat_claims"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    cat_id: Mapped[int] = mapped_column(Integer, ForeignKey("cats.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # pending | verified | rejected | revoked
    status: Mapped[str] = mapped_column(String, default="pending", index=True)
    avg_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

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
    user = relationship("User")
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
    """A photo submitted as evidence for a claim, with its individual match score."""

    __tablename__ = "claim_photos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    claim_id: Mapped[int] = mapped_column(Integer, ForeignKey("cat_claims.id"), nullable=False, index=True)
    photo_path: Mapped[str] = mapped_column(String, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    features_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    claim = relationship("CatClaim", back_populates="photos")
