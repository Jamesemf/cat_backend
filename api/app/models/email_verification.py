from datetime import datetime, timezone

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class EmailVerification(Base):
    """A pending email-verification code issued at registration. Codes are
    stored hashed, single-use, and expire after a short window — the same
    pattern as PasswordReset, persisted so they survive restarts and stay
    consistent across workers."""

    __tablename__ = "email_verifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String, index=True, nullable=False)
    code_hash: Mapped[str] = mapped_column(String, nullable=False)
    # Wrong-guess counter. The code is invalidated once this hits the cap so a
    # short numeric code can't be brute-forced within its validity window.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
