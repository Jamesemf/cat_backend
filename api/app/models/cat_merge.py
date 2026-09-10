from datetime import datetime, timezone

from sqlalchemy import ForeignKey, Index, Integer, String, DateTime, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


class CatMergeRequest(Base):
    """A report that two cat profiles are the same animal, decided by a human.

    Re-ID sometimes misses a match, so one cat photographed twice becomes two
    records, each holding half a history. The people who would notice are the
    ones who walk that street every day; this is how they say so.

    Deciding one is not a yes/no. Which profile survives depends on things the
    requester cannot see — which side carries a verified owner, the longer
    history, the better photo — so they report *that these are the same cat* and
    suggest a keeper, and the moderator chooses, seeded with that suggestion.
    Same shape as a trait correction, where the moderator submits the values
    rather than accepting a proposal whole.

    Anyone signed in may file one. Unlike a trait correction, the open-request
    guard is per *pair* rather than per person: a pair only needs deciding once,
    however many people notice it.
    """

    __tablename__ = "cat_merge_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)

    # The pair, stored order-normalised (cat_a_id < cat_b_id) so "A and B" filed
    # after "B and A" is caught as the duplicate it is.
    #
    # Nullable because approving deletes one of them: the loser's id is cleared
    # rather than left pointing at a row that no longer exists. Postgres would
    # refuse the delete outright otherwise.
    cat_a_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("cats.id"), nullable=True, index=True
    )
    cat_b_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("cats.id"), nullable=True, index=True
    )
    # Names as they read when the request was filed, so a decided row still says
    # which two cats it was about once one of them is gone.
    cat_a_name: Mapped[str | None] = mapped_column(String, nullable=True)
    cat_b_name: Mapped[str | None] = mapped_column(String, nullable=True)

    # Which of the two the requester would keep. A hint for the moderator, not a
    # decision, and null when they had no preference.
    suggested_keep_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id"), nullable=True, index=True
    )
    # pending | merged | rejected | superseded
    #
    # `superseded` is for a request whose cat disappeared under it — merged away
    # by a different request, or deleted with its last sighting. Nobody decided
    # it, and there is nothing left to decide.
    status: Mapped[str] = mapped_column(String, default="pending", index=True)

    # The survivor, set on approval. Null until then, and on any other outcome.
    merged_into_cat_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("cats.id"), nullable=True
    )

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

    # Three FKs point at cats and two at users, so every one must name its column.
    cat_a = relationship("Cat", foreign_keys=[cat_a_id])
    cat_b = relationship("Cat", foreign_keys=[cat_b_id])
    merged_into = relationship("Cat", foreign_keys=[merged_into_cat_id])
    user = relationship("User", foreign_keys=[user_id])
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_id])

    __table_args__ = (
        # The open-pair guard reads (cat_a, cat_b, status) on every submission.
        Index("ix_cat_merge_pair_status", "cat_a_id", "cat_b_id", "status"),
    )

    @staticmethod
    def normalise_pair(one: int, other: int) -> tuple[int, int]:
        """Order a pair the way it is stored, so lookups match inserts."""
        return (one, other) if one < other else (other, one)
