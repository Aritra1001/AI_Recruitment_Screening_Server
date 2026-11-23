"""Domains (Software, Data Engineering, ...) and the managers who screen their jobs' resumes.

Admins manage them directly; unlike job changes, these don't need a second admin's approval.
Everyone can list them (the job form and filters need the names)."""

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_admin
from app.database import get_db
from app.models import Domain, Job, Role, User
from app.schemas import DomainInput, DomainManagersUpdate, DomainRead, UserRead

router = APIRouter(tags=["domains"])
logger = logging.getLogger("app.domains")


def _get_or_404(db: Session, domain_id: UUID, user: User, action: str) -> Domain:
    domain = db.get(Domain, domain_id)
    if domain is None:
        logger.warning("DOMAIN_NOT_FOUND action=%s domain_id=%s user=%s", action, domain_id, user.email)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found")
    return domain


def _job_counts(db: Session) -> dict[UUID, int]:
    stmt = select(Job.domain_id, func.count()).where(Job.domain_id.is_not(None)).group_by(Job.domain_id)
    return {domain_id: count for domain_id, count in db.execute(stmt)}


def _read(domain: Domain, counts: dict[UUID, int]) -> DomainRead:
    read = DomainRead.model_validate(domain)
    read.job_count = counts.get(domain.domain_id, 0)
    return read


def _name_taken(db: Session, name: str, except_id: UUID | None = None) -> bool:
    stmt = select(Domain).where(func.lower(Domain.name) == name.lower())
    if except_id is not None:
        stmt = stmt.where(Domain.domain_id != except_id)
    return db.scalar(stmt) is not None


def _duplicate(name: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f'A domain called "{name}" already exists')


@router.get("/domains", response_model=list[DomainRead])
def list_domains(db: Session = Depends(get_db), user: User = Depends(get_current_user)) -> list[DomainRead]:
    domains = list(db.scalars(select(Domain).order_by(Domain.name)))
    counts = _job_counts(db)
    return [_read(d, counts) for d in domains]


@router.post("/domains", response_model=DomainRead, status_code=status.HTTP_201_CREATED)
def create_domain(
    payload: DomainInput, db: Session = Depends(get_db), user: User = Depends(require_admin)
) -> DomainRead:
    if _name_taken(db, payload.name):
        raise _duplicate(payload.name)
    domain = Domain(name=payload.name)
    db.add(domain)
    try:
        db.commit()
    except IntegrityError:  # lost a race with the same name
        db.rollback()
        raise _duplicate(payload.name)
    db.refresh(domain)
    logger.info("DOMAIN_CREATED domain_id=%s name=%r user=%s", domain.domain_id, domain.name, user.email)
    return _read(domain, {})


@router.patch("/domains/{domain_id}", response_model=DomainRead)
def rename_domain(
    domain_id: UUID, payload: DomainInput, db: Session = Depends(get_db), user: User = Depends(require_admin)
) -> DomainRead:
    domain = _get_or_404(db, domain_id, user, "rename")
    if _name_taken(db, payload.name, except_id=domain_id):
        raise _duplicate(payload.name)
    old = domain.name
    domain.name = payload.name
    db.commit()
    logger.info("DOMAIN_RENAMED domain_id=%s old=%r new=%r user=%s", domain_id, old, domain.name, user.email)
    return _read(domain, _job_counts(db))


@router.delete("/domains/{domain_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_domain(domain_id: UUID, db: Session = Depends(get_db), user: User = Depends(require_admin)) -> Response:
    domain = _get_or_404(db, domain_id, user, "delete")
    if jobs := _job_counts(db).get(domain_id, 0):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{jobs} {'job uses' if jobs == 1 else 'jobs use'} this domain. Move them to another domain first.",
        )
    db.delete(domain)
    db.commit()
    logger.info("DOMAIN_DELETED domain_id=%s name=%r user=%s", domain_id, domain.name, user.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/domains/{domain_id}/managers", response_model=DomainRead)
def set_domain_managers(
    domain_id: UUID,
    payload: DomainManagersUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> DomainRead:
    """Replace the domain's managers. Each must be an active user with the manager role."""
    domain = _get_or_404(db, domain_id, user, "set_managers")
    ids = list(dict.fromkeys(payload.user_ids))
    found = {u.user_id: u for u in db.scalars(select(User).where(User.user_id.in_(ids)))} if ids else {}
    if invalid := [str(i) for i in ids if i not in found or found[i].role != Role.MANAGER or not found[i].is_active]:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Not an active manager account: {', '.join(invalid)}",
        )
    old = sorted(m.email for m in domain.managers)
    domain.managers = [found[i] for i in ids]
    db.commit()
    db.refresh(domain)
    logger.info(
        "DOMAIN_MANAGERS_SET domain_id=%s name=%r old=%s new=%s user=%s",
        domain_id, domain.name, ",".join(old), ",".join(sorted(m.email for m in domain.managers)), user.email,
    )
    return _read(domain, _job_counts(db))


@router.get("/users", response_model=list[UserRead])
def list_users(
    role: Role | None = Query(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
) -> list[User]:
    """Active users, e.g. ?role=manager for the accounts that can be made a domain's managers."""
    stmt = select(User).where(User.is_active).order_by(User.full_name)
    if role is not None:
        stmt = stmt.where(User.role == role)
    return list(db.scalars(stmt))
