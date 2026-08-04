from datetime import datetime, timezone

from sqlalchemy import Float, ForeignKey, Integer, String, DateTime, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class ExplorerPost(Base):
    """One item in the Explorer feed.

    Every feed entry is a row here. Sighting-originated posts carry sighting_id
    and denormalized photo/location copied at creation time; direct uploads have
    sighting_id NULL and own their photo. A single concrete table keeps the feed
    one indexed query and gives meows/comments a real FK target.
    """

    __tablename__ = "explorer_posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    # Nullable: legacy anonymous sightings have no submitting user.
    user_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    # Set => sighting-originated post. Unique so a sighting is posted at most once.
    sighting_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("sightings.id"), nullable=True, unique=True
    )
    # Direct uploads can tag a cat: one the uploader has a verified claim on, or
    # a brand-new owner-created cat profile. Sighting posts leave this NULL and
    # resolve their cat through the sighting instead.
    cat_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("cats.id"), nullable=True, index=True)
    photo_path: Mapped[str] = mapped_column(String, nullable=False)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Optional: direct uploads may have no location and simply don't appear on the map.
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )
    # Moderation: set => the post is withheld from the feed, cat grids and all
    # interactions. Reversible (unlike deletion), so it's safe for the automatic
    # report threshold to set it — see services/moderation.py. NULL is visible.
    hidden_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    # auto_reports | moderator
    hidden_reason: Mapped[str | None] = mapped_column(String, nullable=True)

    user = relationship("User")
    sighting = relationship("Sighting")
    cat = relationship("Cat")


class PostMeow(Base):
    """A user's 'meow' (like) on an Explorer post. One per user per post."""

    __tablename__ = "post_meows"
    __table_args__ = (UniqueConstraint("post_id", "user_id", name="uq_meow_post_user"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    post_id: Mapped[int] = mapped_column(Integer, ForeignKey("explorer_posts.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))


class PostComment(Base):
    """A comment on an Explorer post."""

    __tablename__ = "post_comments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    post_id: Mapped[int] = mapped_column(Integer, ForeignKey("explorer_posts.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc), index=True
    )

    user = relationship("User")


class PostReport(Base):
    """A user's report against an Explorer post. One report per user per post,
    ever — the unique constraint makes repeat reports a no-op rather than a way
    to stack the auto-hide threshold.

    A report is 'open' until a moderator resolves it (reviewed_at set). Only
    open reports count toward the auto-hide threshold, so dismissing a post's
    reports doesn't leave it one stale report away from hiding again.
    """

    __tablename__ = "post_reports"
    __table_args__ = (UniqueConstraint("post_id", "reporter_id", name="uq_report_post_reporter"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    post_id: Mapped[int] = mapped_column(Integer, ForeignKey("explorer_posts.id"), nullable=False, index=True)
    reporter_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    # not_a_cat | inappropriate | spam | animal_harm | other
    reason: Mapped[str] = mapped_column(String, nullable=False)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=lambda: datetime.now(timezone.utc))
    # Set when a moderator dismissed, hid, or removed in response to this report.
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    reviewed_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )

    post = relationship("ExplorerPost")
    reporter = relationship("User", foreign_keys=[reporter_id])
