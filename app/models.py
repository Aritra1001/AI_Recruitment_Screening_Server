import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite in tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Job(Base):
    """Mirrors db/migrations/001_create_jobs.sql."""

    __tablename__ = "jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    required_skills: Mapped[list[str] | None] = mapped_column(JSONType)
    preferred_skills: Mapped[list[str] | None] = mapped_column(JSONType)
    minimum_experience: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=True, server_default=func.current_timestamp()
    )


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


class ResumeStatus(StrEnum):
    UPLOADED = "uploaded"
    EXTRACTING = "extracting"
    EXTRACTED = "extracted"
    FAILED = "failed"


class Resume(Base):
    """Mirrors db/migrations/003_create_resumes.sql + 004_store_resume_files_in_db.sql."""

    __tablename__ = "resumes"

    resume_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # The file itself (BYTEA). Deferred so listing/reading resumes doesn't load the bytes;
    # only the download endpoint touches it.
    content: Mapped[bytes] = mapped_column(LargeBinary, nullable=False, deferred=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ResumeStatus.UPLOADED)
    # Filled in by the extraction step (app/resume_extraction.py).
    raw_text: Mapped[str | None] = mapped_column(Text)
    extracted: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    extraction_error: Mapped[str | None] = mapped_column(Text)
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=True, server_default=func.current_timestamp()
    )

    uploader: Mapped[User] = relationship(lazy="joined")

    @property
    def candidate_name(self) -> str | None:
        return (self.extracted or {}).get("name")


class ScoreStatus(StrEnum):
    SCORING = "scoring"
    SCORED = "scored"
    FAILED = "failed"


class ResumeScore(Base):
    """Mirrors db/migrations/005_create_resume_scores.sql."""

    __tablename__ = "resume_scores"
    __table_args__ = (UniqueConstraint("resume_id", "job_id"),)

    score_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    resume_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resumes.resume_id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("jobs.job_id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ScoreStatus.SCORING)
    # Filled in by the scoring step (app/resume_scoring.py).
    overall_score: Mapped[float | None] = mapped_column(Numeric(5, 2, asdecimal=False))
    recommendation: Mapped[str | None] = mapped_column(String(20))
    criteria: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    relevant_experience_years: Mapped[float | None] = mapped_column(Numeric(4, 1, asdecimal=False))
    summary: Mapped[str | None] = mapped_column(Text)
    strengths: Mapped[list[str] | None] = mapped_column(JSONType)
    gaps: Mapped[list[str] | None] = mapped_column(JSONType)
    weights: Mapped[dict[str, int] | None] = mapped_column(JSONType)
    agent_name: Mapped[str | None] = mapped_column(String(100))
    agent_version: Mapped[str | None] = mapped_column(String(20))
    model: Mapped[str | None] = mapped_column(String(100))
    input_hash: Mapped[str | None] = mapped_column(String(64))
    error: Mapped[str | None] = mapped_column(Text)
    scored_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.user_id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=True, server_default=func.current_timestamp()
    )
    scored_at: Mapped[datetime | None] = mapped_column(DateTime)

    resume: Mapped[Resume] = relationship(lazy="joined")
