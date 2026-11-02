import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import Role, User
from app.schemas import LoginRequest, SignupRequest, TokenResponse, UserRead
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])
logger = logging.getLogger("app.auth")


def _token_response(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(str(user.user_id), user.role),
        expires_in=settings.access_token_expire_minutes * 60,
        user=UserRead.model_validate(user),
    )


def _find_by_email(db: Session, email: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.email) == email.lower()))


@router.post("/signup", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
def signup(payload: SignupRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Create an account and log it in. Self-registered users are always recruiters
    (read-only); an admin promotes them with `python -m app.create_user`."""
    email_taken = HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail="An account with this email already exists"
    )
    if _find_by_email(db, payload.email) is not None:
        logger.warning("SIGNUP_REJECTED email=%s reason=email_taken", payload.email)
        raise email_taken

    user = User(
        email=payload.email,
        full_name=payload.full_name,
        password_hash=hash_password(payload.password),
        role=Role.RECRUITER,
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:  # lost a race with a concurrent signup for the same email
        db.rollback()
        logger.warning("SIGNUP_REJECTED email=%s reason=email_taken", payload.email)
        raise email_taken
    db.refresh(user)

    logger.info("SIGNUP_SUCCESS user=%s role=%s", user.email, user.role)
    return _token_response(user)


@router.post("/login", response_model=TokenResponse)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    email = payload.email.lower()
    user = _find_by_email(db, email)
    if user is None or not user.is_active or not verify_password(payload.password, user.password_hash):
        logger.warning("LOGIN_FAILED email=%s", email)
        # Same message either way so the response doesn't reveal which emails exist.
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password")

    logger.info("LOGIN_SUCCESS user=%s role=%s", user.email, user.role)
    return _token_response(user)


@router.get("/me", response_model=UserRead)
def me(user: User = Depends(get_current_user)) -> User:
    return user
