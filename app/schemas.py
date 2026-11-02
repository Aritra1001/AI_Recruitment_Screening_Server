import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.security import MAX_PASSWORD_BYTES, MIN_PASSWORD_LENGTH


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
