import logging
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.job_parsing import normalize_skills, split_experience
from app.models import Job, User
from app.schemas import JobCreate, JobRead, JobUpdate

router = APIRouter(prefix="/jobs", tags=["jobs"])
logger = logging.getLogger("app.jobs")

_SNAPSHOT_FIELDS = ("title", "required_skills", "preferred_skills", "minimum_experience")


def _get_job_or_404(db: Session, job_id: UUID, user: User, action: str) -> Job:
    job = db.get(Job, job_id)
    if job is None:
        logger.warning("JOB_NOT_FOUND action=%s job_id=%s user=%s", action, job_id, user.email)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job not found")
    return job


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


@router.post("", response_model=JobRead, status_code=status.HTTP_201_CREATED)
def create_job(
    payload: JobCreate, db: Session = Depends(get_db), user: User = Depends(require_admin)
) -> Job:
    job = Job(**_clean_skill_fields(payload.model_dump()))
    db.add(job)
    db.commit()
    db.refresh(job)
    logger.info("JOB_CREATED job_id=%s title=%r user=%s", job.job_id, job.title, user.email)
    return job


@router.get("", response_model=list[JobRead])
def list_jobs(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Job]:
    stmt = select(Job).order_by(Job.created_at.desc()).limit(limit).offset(offset)
    jobs = list(db.scalars(stmt))
    logger.info(
        "JOBS_LISTED count=%d limit=%d offset=%d user=%s", len(jobs), limit, offset, user.email
    )
    return jobs


@router.get("/{job_id}", response_model=JobRead)
def get_job(
    job_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Job:
    job = _get_job_or_404(db, job_id, user, "read")
    logger.info("JOB_READ job_id=%s user=%s", job_id, user.email)
    return job


@router.patch("/{job_id}", response_model=JobRead)
def update_job(
    job_id: UUID,
    payload: JobUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> Job:
    job = _get_job_or_404(db, job_id, user, "update")
    changes: dict[str, dict[str, Any]] = {}
    for field, value in _clean_skill_fields(payload.model_dump(exclude_unset=True)).items():
        old = getattr(job, field)
        if old != value:
            changes[field] = {"old": old, "new": value}
            setattr(job, field, value)
    db.commit()
    db.refresh(job)
    if changes:
        logger.info("JOB_UPDATED job_id=%s user=%s changes=%s", job_id, user.email, changes)
    else:
        logger.info("JOB_UPDATE_NO_CHANGES job_id=%s user=%s", job_id, user.email)
    return job


@router.delete("/{job_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_job(
    job_id: UUID, db: Session = Depends(get_db), user: User = Depends(require_admin)
) -> Response:
    job = _get_job_or_404(db, job_id, user, "delete")
    # Keep what was deleted in the audit log, since the row itself is gone afterwards.
    snapshot = {field: getattr(job, field) for field in _SNAPSHOT_FIELDS}
    db.delete(job)
    db.commit()
    logger.info("JOB_DELETED job_id=%s user=%s deleted=%s", job_id, user.email, snapshot)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
