import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import approvals, notifications
from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models import ApprovalRequest, ApprovalStatus, Role, User
from app.schemas import ApprovalDecision, ApprovalRead

router = APIRouter(prefix="/approvals", tags=["approvals"])
logger = logging.getLogger("app.approvals")


def _to_read(request: ApprovalRequest, user: User) -> ApprovalRead:
    read = ApprovalRead.model_validate(request)
    pending = request.status == ApprovalStatus.PENDING
    read.can_review = pending and user.role == Role.ADMIN and user.user_id != request.requested_by
    read.can_cancel = pending and user.user_id == request.requested_by
    return read


def _not_found(request_id: UUID, user: User, action: str) -> HTTPException:
    logger.warning("APPROVAL_NOT_FOUND action=%s request_id=%s user=%s", action, request_id, user.email)
    return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Approval request not found")


def _locked_or_404(db: Session, request_id: UUID, user: User, action: str) -> ApprovalRequest:
    request = approvals.lock(db, request_id)
    if request is None:
        raise _not_found(request_id, user, action)
    return request


@router.get("", response_model=list[ApprovalRead])
def list_approvals(
    background_tasks: BackgroundTasks,
    status_filter: Literal["pending", "approved", "rejected", "expired", "cancelled", "all"] = Query(
        "pending", alias="status"
    ),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> list[ApprovalRead]:
    """Requests newest first; pending ones by default."""
    approvals.expire_stale_now(db, background_tasks)
    stmt = select(ApprovalRequest).order_by(ApprovalRequest.requested_at.desc(), ApprovalRequest.request_id)
    if status_filter != "all":
        stmt = stmt.where(ApprovalRequest.status == status_filter)
    requests = list(db.scalars(stmt.limit(limit).offset(offset)))
    logger.info(
        "APPROVALS_LISTED status=%s count=%d limit=%d offset=%d user=%s",
        status_filter, len(requests), limit, offset, user.email,
    )
    return [_to_read(r, user) for r in requests]


@router.get("/{request_id}", response_model=ApprovalRead)
def get_approval(
    request_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ApprovalRead:
    """Admins see any request; anyone else only their own (a recruiter's resume deletion)."""
    approvals.expire_stale_now(db, background_tasks)
    request = db.get(ApprovalRequest, request_id)
    if request is None or (user.role != Role.ADMIN and request.requested_by != user.user_id):
        raise _not_found(request_id, user, "read")
    logger.info("APPROVAL_READ request_id=%s user=%s", request_id, user.email)
    return _to_read(request, user)


@router.post("/{request_id}/approve", response_model=ApprovalRead)
def approve(
    request_id: UUID,
    background_tasks: BackgroundTasks,
    payload: ApprovalDecision | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> ApprovalRead:
    """Apply the change. Any admin except the one who asked can approve."""
    request = _locked_or_404(db, request_id, user, "approve")
    emails = approvals.approve(db, request, user, (payload or ApprovalDecision()).comment)
    background_tasks.add_task(notifications.send, emails)
    return _to_read(request, user)


@router.post("/{request_id}/reject", response_model=ApprovalRead)
def reject(
    request_id: UUID,
    background_tasks: BackgroundTasks,
    payload: ApprovalDecision | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> ApprovalRead:
    """Nothing changes; a new job that was waiting for approval is deleted."""
    request = _locked_or_404(db, request_id, user, "reject")
    emails = approvals.reject(db, request, user, (payload or ApprovalDecision()).comment)
    background_tasks.add_task(notifications.send, emails)
    return _to_read(request, user)


@router.post("/{request_id}/cancel", response_model=ApprovalRead)
def cancel(
    request_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ApprovalRead:
    """The person who asked withdraws the request, e.g. to correct an edit."""
    request = _locked_or_404(db, request_id, user, "cancel")
    if user.role != Role.ADMIN and request.requested_by != user.user_id:
        raise _not_found(request_id, user, "cancel")
    approvals.cancel(db, request, user)
    return _to_read(request, user)
