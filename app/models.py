import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base

# JSONB on PostgreSQL, plain JSON elsewhere (e.g. SQLite in tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class Domain(Base):
    """A business area such as Software or Data Engineering. Mirrors 008_domains_and_screening.sql.

    Each job belongs to a domain, and the domain's managers screen the resumes that recruiters
    shortlist for its jobs."""

    __tablename__ = "domains"

    domain_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=True, server_default=func.current_timestamp()
    )

    managers: Mapped[list["User"]] = relationship(
        secondary="domain_managers", lazy="selectin", order_by="User.full_name"
    )


class DomainManager(Base):
    __tablename__ = "domain_managers"

    domain_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("domains.domain_id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)


# The score a resume needs before it can be sent to the domain manager, when the job doesn't say.
DEFAULT_SHORTLIST_MIN_SCORE = 60


class Job(Base):
    """Mirrors db/migrations/001_create_jobs.sql + 008_domains_and_screening.sql."""

    __tablename__ = "jobs"

    job_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    required_skills: Mapped[list[str] | None] = mapped_column(JSONType)
    preferred_skills: Mapped[list[str] | None] = mapped_column(JSONType)
    minimum_experience: Mapped[int | None] = mapped_column(Integer)
    # Null only for jobs created before domains existed; their resumes can't be shortlisted
    # until an admin sets one.
    domain_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("domains.domain_id", ondelete="RESTRICT"))
    # A resume needs at least this overall score (0-100) to be sent to the domain manager.
    shortlist_min_score: Mapped[int] = mapped_column(
        Integer, nullable=False, default=DEFAULT_SHORTLIST_MIN_SCORE, server_default=str(DEFAULT_SHORTLIST_MIN_SCORE)
    )
    # False until a second admin approves the job's creation; recruiters only see published jobs.
    is_published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=True, server_default=func.current_timestamp()
    )

    domain: Mapped[Domain | None] = relationship(lazy="joined")


class Role(StrEnum):
    # Creates, edits and deletes jobs, and deletes any resume, each once another admin approves.
    ADMIN = "admin"
    # Read-only on jobs; uploads resumes, deletes only their own (once an admin approves);
    # sends resumes that meet the job's minimum score to the domain manager.
    RECRUITER = "recruiter"
    # Screens the resumes sent for jobs in their domains: interview or reject. Read-only
    # everywhere else.
    MANAGER = "manager"


class User(Base):
    """Mirrors db/migrations/002_create_users.sql + 009_google_sign_in.sql."""

    __tablename__ = "users"

    user_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # None for accounts that only sign in with Google.
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Google's permanent account id (the ID token's `sub`), set on the first Google sign-in.
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
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
    """Mirrors db/migrations/003_create_resumes.sql, 004_store_resume_files_in_db.sql and
    007_resumes_belong_to_a_job.sql."""

    __tablename__ = "resumes"
    # The same file can be uploaded once per job (a candidate applying to two jobs).
    __table_args__ = (UniqueConstraint("job_id", "sha256"),)

    resume_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # The job this resume was uploaded for; it is only scored against that job. Every upload
    # sets it. Null only for resumes uploaded before resumes belonged to a job.
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("jobs.job_id", ondelete="CASCADE"), index=True
    )
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
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
    job: Mapped[Job | None] = relationship(lazy="joined")

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


class ApprovalAction(StrEnum):
    JOB_CREATE = "job_create"
    JOB_UPDATE = "job_update"
    JOB_DELETE = "job_delete"
    RESUME_DELETE = "resume_delete"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"  # withdrawn by the person who asked


class ApprovalRequest(Base):
    """Mirrors db/migrations/006_create_approval_requests.sql."""

    __tablename__ = "approval_requests"
    # At most one pending request per job and per resume: further changes wait for it.
    __table_args__ = (
        Index(
            "ux_approval_requests_pending_job", "job_id", unique=True,
            postgresql_where=text("status = 'pending'"), sqlite_where=text("status = 'pending'"),
        ),
        Index(
            "ux_approval_requests_pending_resume", "resume_id", unique=True,
            postgresql_where=text("status = 'pending'"), sqlite_where=text("status = 'pending'"),
        ),
    )

    request_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ApprovalStatus.PENDING)
    # SET NULL so the request (the audit trail) outlives the job or resume it deleted.
    job_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("jobs.job_id", ondelete="SET NULL"))
    resume_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("resumes.resume_id", ondelete="SET NULL"))
    # The job title or resume file name, for showing the request once the target is gone.
    target_name: Mapped[str] = mapped_column(String(255), nullable=False)
    # job_create: the new job's fields. job_update: {field: {"old", "new"}} for each change.
    # job_delete / resume_delete: what will be deleted.
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    requested_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.user_id"))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime)
    review_comment: Mapped[str | None] = mapped_column(Text)

    requester: Mapped[User] = relationship(foreign_keys=[requested_by], lazy="joined")
    reviewer: Mapped[User | None] = relationship(foreign_keys=[reviewed_by], lazy="joined")


class ScreeningStatus(StrEnum):
    PENDING = "pending"  # sent by a recruiter, waiting for the domain manager
    INTERVIEW = "interview"
    REJECTED = "rejected"


class Screening(Base):
    """A resume a recruiter sent to the job's domain manager, and the manager's decision.
    One per resume (a resume belongs to one job). Mirrors 008_domains_and_screening.sql.

    The domain isn't stored: it is the job's, so moving a job to another domain moves its
    pending screenings with it."""

    __tablename__ = "screenings"

    screening_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    resume_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resumes.resume_id", ondelete="CASCADE"), unique=True, nullable=False
    )
    job_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("jobs.job_id", ondelete="CASCADE"), index=True, nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=ScreeningStatus.PENDING)
    # The overall score and the job's minimum when it was sent; re-scoring later doesn't change them.
    score_at_submission: Mapped[float] = mapped_column(Numeric(5, 2, asdecimal=False), nullable=False)
    min_score_at_submission: Mapped[int] = mapped_column(Integer, nullable=False)
    note: Mapped[str | None] = mapped_column(Text)  # from the recruiter, for the manager
    sent_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.user_id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime)
    decision_comment: Mapped[str | None] = mapped_column(Text)

    resume: Mapped[Resume] = relationship(lazy="joined")
    job: Mapped[Job] = relationship(lazy="joined")
    sender: Mapped[User] = relationship(foreign_keys=[sent_by], lazy="joined")
    decider: Mapped[User | None] = relationship(foreign_keys=[decided_by], lazy="joined")

    @property
    def domain(self) -> Domain | None:
        return self.job.domain
