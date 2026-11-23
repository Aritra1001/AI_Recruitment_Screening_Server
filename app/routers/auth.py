import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import google_auth
from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import Role, User
from app.schemas import GoogleLoginRequest, LoginRequest, SignupRequest, TokenResponse, UserRead
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


@router.post("/google", response_model=TokenResponse)
def google_login(payload: GoogleLoginRequest, db: Session = Depends(get_db)) -> TokenResponse:
    """Sign in with the ID token from Google's sign-in button.

    The account is found by Google id, then by email (an existing account is linked to the
    Google account on its first Google sign-in and keeps its role). Anyone else gets a new
    recruiter account without a password, as with `/signup`.
    """
    if not settings.google_client_id:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Google sign-in is not configured"
        )
    try:
        identity = google_auth.verify_id_token(payload.credential)
    except google_auth.GoogleSignInError as exc:
        logger.warning("LOGIN_FAILED method=google reason=%s", exc.reason)
        raise HTTPException(status_code=exc.status_code, detail=exc.message)

    user = db.scalar(select(User).where(User.google_sub == identity.sub))
    if user is None:
        user = _find_by_email(db, identity.email)
        if user is not None and user.google_sub is not None:
            # The email belongs to an account already linked to another Google account.
            logger.warning("LOGIN_FAILED method=google email=%s reason=linked_elsewhere", identity.email)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This email is already linked to a different Google account",
            )

    if user is not None and not user.is_active:
        logger.warning("LOGIN_FAILED method=google email=%s reason=inactive", user.email)
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="This account has been deactivated")

    event = "LOGIN_SUCCESS"
    if user is None:
        user = User(
            email=identity.email,
            full_name=identity.name,
            password_hash=None,
            google_sub=identity.sub,
            role=Role.RECRUITER,
        )
        db.add(user)
        event = "SIGNUP_SUCCESS"
    elif user.google_sub is None:
        user.google_sub = identity.sub
        event = "GOOGLE_LINKED"

    if event != "LOGIN_SUCCESS":  # created or linked
        try:
            db.commit()
        except IntegrityError:  # lost a race with a concurrent sign-in for the same account
            db.rollback()
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Please try signing in again")
        db.refresh(user)

    logger.info("%s method=google user=%s role=%s", event, user.email, user.role)
    return _token_response(user)


@router.get("/me", response_model=UserRead)
def me(user: User = Depends(get_current_user)) -> User:
    return user
