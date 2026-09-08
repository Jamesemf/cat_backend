from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, Integer, String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class TraitChangeRequest(Base):
    """A proposed correction to a cat's traits, decided by a human moderator.

    A cat's traits are written once by vision at first sighting and never
    revisited, so a cat recorded as "gray, solid" stays that way however many
    people can see it is a ginger tabby. This is how they say so.

    Every request lands as `pending`. A moderator reads the proposal beside what
    is on record, adjusts anything the requester got wrong, and applies — so what
    lands on the cat is `applied_json`, which is not necessarily what was asked
    for. Both are kept: the proposal is what the requester will be told about,
    the applied values are what actually happened.

    Anyone signed in may file one, the cat's verified owner included. Ownership
    is not authority over the record here — it earns a badge in the queue, not a
    bypass of it.
    """

    __tablename__ = "trait_change_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    cat_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("cats.id"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    # pending | applied | rejected
    status: Mapped[str] = mapped_column(String, default="pending", index=True)

    # Only the fields the requester actually changed, as a JSON object. A dict
    # rather than seven columns because "no second colour" is a real correction:
    # a missing key means "leave it alone", an explicit null means "clear it",
    # and nullable columns cannot tell those two apart.
    proposed_json: Mapped[str] = mapped_column(Text, nullable=False)
    # What the moderator actually wrote, which may differ from the proposal.
    # Null until decided.
    applied_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    # The moderator who decided it. Nulled when that account is deleted,
    # mirroring CatClaim.reviewed_by_id.
    reviewed_by_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    cat = relationship("Cat")
    # Two FKs point at users, so both sides must name their column.
    user = relationship("User", foreign_keys=[user_id])
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_id])

    __table_args__ = (
        # The open-request guard reads (user, cat, status) on every submission.
        Index("ix_trait_change_user_cat_status", "user_id", "cat_id", "status"),
    )
