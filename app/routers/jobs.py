import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import approvals, notifications
from app.auth import get_current_user, require_admin
from app.database import get_db
from app.job_parsing import normalize_skills, split_experience
from app.models import ApprovalAction, ApprovalRequest, Job, Role, User
from app.schemas import JobApproval, JobCreate, JobRead, JobUpdate, PendingApproval

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = logging.getLogger("app.jobs")


def _get_job_or_404(db: Session, job_id: UUID, user: User, action: str) -> Job:
    job = db.get(Job, job_id)
    # A job waiting for its creation to be approved doesn't exist as far as recruiters know.
    if job is None or (not job.is_published and user.role != Role.ADMIN):
        logger.warning("JOB_NOT_FOUND action=%s job_id=%s user=%s", action, job_id, user.email)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


def _to_read(job: Job, user: User, pending: ApprovalRequest | None) -> JobRead:
    """JobRead, with the approval status filled in for admins only."""
    read = JobRead.model_validate(job)
    if user.role == Role.ADMIN:
        read.approval = JobApproval(
            status="pending" if pending is not None or not job.is_published else "approved",
            pending=PendingApproval.model_validate(pending) if pending is not None else None,
        )
    return read


def _read_one(db: Session, job: Job, user: User) -> JobRead:
    return _to_read(job, user, approvals.pending_requests(db, job_ids=[job.job_id]).get(job.job_id))


def _check_no_pending(db: Session, job: Job) -> None:
    if approvals.pending_requests(db, job_ids=[job.job_id]):
        raise approvals.already_pending()


def _clean_skill_fields(data: dict[str, Any]) -> dict[str, Any]:
    """Normalise any skill lists in `data`, moving experience lines out of required_skills
    into minimum_experience unless the caller supplied that explicitly."""
    if "required_skills" in data:
        data["required_skills"], years = split_experience(normalize_skills(data["required_skills"]))
        if data.get("minimum_experience") is None and years is not None:
            data["minimum_experience"] = years
    if "preferred_skills" in data:
        data["preferred_skills"] = normalize_skills(data["preferred_skills"])
    return data


@router.post("", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
def create_job(
    payload: JobCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> JobRead:
    """Save the job unpublished and ask the other admins to approve it. Recruiters see it
    once it is approved."""
    job = Job(**_clean_skill_fields(payload.model_dump()), is_published=False)
    db.add(job)
    db.flush()
    request, emails = approvals.submit(
        db, user, ApprovalAction.JOB_CREATE, job.title, approvals.job_values(job), job_id=job.job_id
    )
    db.refresh(job)
    logger.info(
        "JOB_CREATE_REQUESTED job_id=%s title=%r request_id=%s user=%s",
        job.job_id, job.title, request.request_id, user.email,
    )
    background_tasks.add_task(notifications.send, emails)
    return _to_read(job, user, request)


@router.get("", response_model=list[JobRead])
def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[JobRead]:
    stmt = select(Job).order_by(Job.created_at.desc(), Job.job_id).limit(limit).offset(offset)
    if user.role != Role.ADMIN:
        stmt = stmt.where(Job.is_published)
    jobs = list(db.scalars(stmt))
    pending = approvals.pending_requests(db, job_ids=[j.job_id for j in jobs]) if user.role == Role.ADMIN else {}
    logger.info(
        "JOBS_LISTED count=%d limit=%d offset=%d user=%s", len(jobs), limit, offset, user.email
    )
    return [_to_read(job, user, pending.get(job.job_id)) for job in jobs]


@router.get("/{job_id}", response_model=JobRead)
def get_job(
    job_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> JobRead:
    job = _get_job_or_404(db, job_id, user, "read")
    logger.info("JOB_READ job_id=%s user=%s", job_id, user.email)
    return _read_one(db, job, user)


@router.patch("/{job_id}", response_model=JobRead)
def update_job(
    job_id: UUID,
    payload: JobUpdate,
    response: Response,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> JobRead:
    """Ask the other admins to approve the changes (202). The job keeps its current values
    until one of them does. A request that changes nothing returns the job as it is (200)."""
    approvals.expire_stale_now(db, background_tasks)
    job = _get_job_or_404(db, job_id, user, "update")
    changes: dict[str, dict[str, Any]] = {}
    for field, value in _clean_skill_fields(payload.model_dump(exclude_unset=True)).items():
        old = getattr(job, field)
        if old != value:
            changes[field] = {"old": old, "new": value}
    if not changes:
        logger.info("JOB_UPDATE_NO_CHANGES job_id=%s user=%s", job_id, user.email)
        return _read_one(db, job, user)

    _check_no_pending(db, job)
    request, emails = approvals.submit(db, user, ApprovalAction.JOB_UPDATE, job.title, changes, job_id=job.job_id)
    logger.info(
        "JOB_UPDATE_REQUESTED job_id=%s request_id=%s user=%s changes=%s",
        job_id, request.request_id, user.email, changes,
    )
    background_tasks.add_task(notifications.send, emails)
    response.status_code = status.HTTP_202_ACCEPTED
    return _to_read(job, user, request)


@router.delete("/{job_id}", response_model=JobRead, status_code=status.HTTP_202_ACCEPTED)
def delete_job(
    job_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> JobRead:
    """Ask the other admins to approve deleting the job. It stays live until one of them does."""
    approvals.expire_stale_now(db, background_tasks)
    job = _get_job_or_404(db, job_id, user, "delete")
    _check_no_pending(db, job)
    # Keep what will be deleted, since the row itself is gone afterwards.
    request, emails = approvals.submit(
        db, user, ApprovalAction.JOB_DELETE, job.title, approvals.job_values(job), job_id=job.job_id
    )
    logger.info("JOB_DELETE_REQUESTED job_id=%s request_id=%s user=%s", job_id, request.request_id, user.email)
    background_tasks.add_task(notifications.send, emails)
    return _to_read(job, user, request)
