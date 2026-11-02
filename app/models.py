import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import Boolean, DateTime, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class Role(StrEnum):
    ADMIN = "admin"  # full CRUD on jobs; can delete any resume
    RECRUITER = "recruiter"  # read-only on jobs; uploads resumes, deletes only their own


class User(Base):
    """Mirrors db/migrations/002_create_users.sql."""

    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default=Role.RECRUITER)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=True, server_default=func.current_timestamp()
    )
