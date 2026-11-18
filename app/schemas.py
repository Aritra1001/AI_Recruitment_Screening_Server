import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

from app.security import MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH


class JobCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(min_length=1)
    required_skills: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    # If omitted, derived from a line like "5+ years experience" in required_skills.
    minimum_experience: int | None = Field(default=None, ge=0, le=60)


class JobUpdate(BaseModel):
    """Partial update: only the fields present in the request body are changed."""

    model_config = ConfigDict(str_strip_whitespace=True)

    title: str | None = Field(default=None, min_length=1, max_length=255)
    description: str | None = Field(default=None, min_length=1)
    required_skills: list[str] | None = None
    preferred_skills: list[str] | None = None
    # Send null to clear it.
    minimum_experience: int | None = Field(default=None, ge=0, le=60)

    @field_validator("title", "description", "required_skills", "preferred_skills", mode="before")
    @classmethod
    def reject_null(cls, value):
        # Defaults aren't validated, so this only fires when the client explicitly sends null.
        if value is None:
            raise ValueError("may be omitted but not null (send [] to clear a skill list)")
        return value


def _as_utc(value: datetime | None) -> datetime | None:
    """The approval tables store naive UTC; say so, so browsers convert it to local time."""
    return value.replace(tzinfo=UTC) if value is not None and value.tzinfo is None else value


UtcDatetime = Annotated[datetime, AfterValidator(_as_utc)]


class UserSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    full_name: str
    email: str


class PendingApproval(BaseModel):
    """The request waiting for approval on a job or resume."""

    model_config = ConfigDict(from_attributes=True)

    request_id: UUID
    action: str
    requester: UserSummary
    requested_at: UtcDatetime
    expires_at: UtcDatetime


class JobApproval(BaseModel):
    # pending: the job's creation, an edit or its deletion is waiting for approval.
    status: Literal["pending", "approved"]
    pending: PendingApproval | None


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: UUID
    title: str
    description: str
    required_skills: list[str] | None
    preferred_skills: list[str] | None
    minimum_experience: int | None
    created_at: datetime
    # Admins only; always null for recruiters.
    approval: JobApproval | None = None


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=255)
    password: str = Field(min_length=1, max_length=128)


# Deliberately loose (no email-validator dependency): something@domain.tld, no spaces.
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SignupRequest(BaseModel):
    """Self-registration. There is no role field: new accounts are always recruiters."""

    model_config = ConfigDict(str_strip_whitespace=True)

    full_name: str = Field(min_length=1, max_length=255)
    email: str = Field(max_length=255)
    password: str = Field(min_length=MIN_PASSWORD_LENGTH)

    @field_validator("email")
    @classmethod
    def valid_email(cls, value: str) -> str:
        if not _EMAIL.match(value):
            raise ValueError("must be a valid email address")
        return value.lower()

    @field_validator("password")
    @classmethod
    def fits_bcrypt(cls, value: str) -> str:
        if len(value.encode()) > MAX_PASSWORD_BYTES:
            raise ValueError(f"must be at most {MAX_PASSWORD_BYTES} bytes")
        return value


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    email: str
    full_name: str
    role: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds
    user: UserRead


class ResumeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    resume_id: UUID
    original_filename: str
    content_type: str
    size_bytes: int
    status: str
    uploaded_at: datetime
    uploader: UserSummary
    # A deletion waiting for approval. Shown to admins and the person who asked for it.
    pending_deletion: PendingApproval | None = None


# --- Extracted resume fields --------------------------------------------------------------
# What the extraction step (app/resume_extraction.py) stores in resumes.extracted. Every value
# is copied from the resume text except `years`, which is computed from the job's dates as
# written (null when the resume gives no usable dates).


class ExperienceEntry(BaseModel):
    company: str | None = None
    role: str | None = None
    years: float | None = None


class ExtractedResume(BaseModel):
    name: str | None = None
    experience: list[ExperienceEntry] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    education: list[str] = Field(default_factory=list)


class ResumeDetail(ResumeRead):
    extracted: ExtractedResume | None
    extraction_error: str | None
    extracted_at: datetime | None


# --- Resume scores ------------------------------------------------------------------------
# What the scoring step (app/resume_scoring.py) stores in resume_scores.


class ScoreRequest(BaseModel):
    # Omit to score every extracted resume.
    resume_ids: list[UUID] | None = Field(default=None, min_length=1, max_length=200)
    # A resume already scored with the same job, resume and agent version keeps its stored
    # score; true asks the agent again anyway.
    force: bool = False


class CriterionScore(BaseModel):
    score: int
    rationale: str
    evidence: list[str]
    # Only on the skill criteria (required_skills, preferred_skills).
    matched: list[str] | None = None
    missing: list[str] | None = None


class ScoredResume(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    resume_id: UUID
    original_filename: str
    candidate_name: str | None


class ResumeScoreRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    score_id: UUID
    job_id: UUID
    resume: ScoredResume
    status: str
    overall_score: float | None
    recommendation: str | None
    criteria: dict[str, CriterionScore] | None
    relevant_experience_years: float | None
    summary: str | None
    strengths: list[str] | None
    gaps: list[str] | None
    weights: dict[str, int] | None
    agent_name: str | None
    agent_version: str | None
    model: str | None
    error: str | None
    scored_at: datetime | None


# --- Approvals ----------------------------------------------------------------------------


class ApprovalRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    request_id: UUID
    action: str
    status: str
    # Null once the job or resume is gone (deleted, or a new job that wasn't approved).
    job_id: UUID | None
    resume_id: UUID | None
    target_name: str
    # job_create: the new job's fields. job_update: {field: {"old", "new"}} for each change.
    # job_delete / resume_delete: what will be deleted.
    details: dict[str, Any]
    requester: UserSummary
    requested_at: UtcDatetime
    expires_at: UtcDatetime
    reviewer: UserSummary | None
    reviewed_at: Annotated[datetime | None, AfterValidator(_as_utc)]
    review_comment: str | None
    # For the current user.
    can_review: bool = False
    can_cancel: bool = False


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # Optional; included in the email to the requester.
    comment: str | None = Field(default=None, max_length=2000)
