from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class Friendship(Base):
    """A mutual friendship between two spotters, and the request that made it.

    One row per unordered pair, whatever state that pair is in. A request, an
    accepted friendship and a declined approach are the same row with a different
    status — which is why the unique constraint can guard the pair at all, and why
    every awkward case (re-requesting after a decline, both sides asking at once)
    collapses into a single-row UPDATE rather than a cross-table shuffle.

    The pair is stored order-normalised (user_low_id < user_high_id) so "A and B"
    and "B and A" are the same row. That deliberately throws away who asked, so
    `requested_by_id` carries it separately: it is what tells an incoming request
    from an outgoing one, and it outlives the accept.

    Friendships are symmetric once accepted — neither column means "owner", and
    either side may unfriend.
    """

    __tablename__ = "friendships"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # The pair, order-normalised via normalise_pair() below. Both NOT NULL: unlike
    # a merge request there is nothing to keep once a participant is gone, so
    # account deletion removes the row outright (see auth.delete_me).
    user_low_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )
    user_high_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False, index=True
    )

    # Who sent the pending request. Always one of the two columns above — the
    # delete in auth.delete_me relies on that, so nothing here may ever set it to
    # a third party. Kept after an accept so a friendship still remembers who
    # asked, and reassigned when a declined pair is re-requested the other way.
    requested_by_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=False
    )

    # pending | accepted | declined
    #
    # A cancelled request and an unfriending both delete the row instead: neither
    # leaves anything worth remembering, and a fresh approach should look new. A
    # decline is kept, so re-requesting is a visible state change rather than an
    # insert that silently forgets the earlier no.
    status: Mapped[str] = mapped_column(String, default="pending", index=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
    # When the request was accepted or declined. Null while pending.
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Three FKs point at users, so every one must name its column.
    user_low = relationship("User", foreign_keys=[user_low_id])
    user_high = relationship("User", foreign_keys=[user_high_id])
    requested_by = relationship("User", foreign_keys=[requested_by_id])

    __table_args__ = (
        # One row per pair, in any state. This is what makes a duplicate request
        # impossible rather than merely unlikely.
        UniqueConstraint("user_low_id", "user_high_id", name="uq_friendship_pair"),
        # Enforces the normalisation the unique constraint depends on: a
        # mis-ordered insert would otherwise slip past it as a second row for the
        # same pair. Also makes self-friendship physically impossible, since
        # `id < id` is false. Plain SQL, so both SQLite and Postgres enforce it.
        CheckConstraint("user_low_id < user_high_id", name="ck_friendship_pair_order"),
        # friend_ids() and the request lists look a user up from either side, so
        # both columns need their own (user, status) index.
        Index("ix_friendship_low_status", "user_low_id", "status"),
        Index("ix_friendship_high_status", "user_high_id", "status"),
    )

    @staticmethod
    def normalise_pair(one: int, other: int) -> tuple[int, int]:
        """Order a pair the way it is stored, so lookups match inserts."""
        return (one, other) if one < other else (other, one)
