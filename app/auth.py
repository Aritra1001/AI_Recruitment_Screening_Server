"""Authentication and role-based access control dependencies."""

import logging
from collections.abc import Callable
from uuid import UUID

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Role, User
from app.security import decode_access_token

logger = logging.getLogger("app.auth")

# auto_error=False so a missing header gives our 401 instead of FastAPI's 403.
_bearer = HTTPBearer(auto_error=False)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise _unauthorized("Not authenticated")
    try:
        payload = decode_access_token(credentials.credentials)
        user_id = UUID(payload["sub"])
    except jwt.ExpiredSignatureError:
        raise _unauthorized("Session expired, please log in again")
    except (jwt.InvalidTokenError, KeyError, ValueError):
        raise _unauthorized("Invalid authentication token")

    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise _unauthorized("User not found or inactive")
    return user


def require_roles(*roles: Role) -> Callable[..., User]:
    """Dependency factory: allow the request only if the current user has one of `roles`."""

    def checker(request: Request, user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            logger.warning(
                "ACCESS_DENIED user=%s role=%s action=%s %s",
                user.email, user.role, request.method, request.url.path,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action",
            )
        return user

    return checker


require_admin = require_roles(Role.ADMIN)
