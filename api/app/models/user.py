from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email: Mapped[str] = mapped_column(String, unique=True, index=True, nullable=False)
    hashed_password: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    apple_sub: Mapped[str | None] = mapped_column(String, unique=True, nullable=True, index=True)
    google_sub: Mapped[str | None] = mapped_column(String, unique=True, nullable=True, index=True)
    avatar_emoji: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Whether the email address has been confirmed via a verification code.
    # Email/password signups start False (a code is emailed on register);
    # Apple/Google signups are True since the provider already verified it.
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc)
    )
