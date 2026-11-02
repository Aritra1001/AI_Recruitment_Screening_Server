import re
from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

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


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    job_id: UUID
    title: str
    description: str
    required_skills: list[str] | None
    preferred_skills: list[str] | None
    minimum_experience: int | None
    created_at: datetime


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
