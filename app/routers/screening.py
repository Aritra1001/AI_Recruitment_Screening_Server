import logging
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import notifications, screening
from app.approvals import utcnow
from app.auth import get_current_user, require_roles, require_staff
from app.database import get_db
from app.models import DomainManager, Job, Resume, ResumeScore, Role, Screening, ScreeningStatus, ScoreStatus, User
from app.resume_scoring import is_current
from app.routers.jobs import get_job_or_404
from app.schemas import ScreeningCreate, ScreeningDecision, ScreeningRead

router = APIRouter(tags=["screening"])
logger = logging.getLogger("app.screening")


def _to_read(s: Screening, user: User) -> ScreeningRead:
    read = ScreeningRead.model_validate(s)
    read.can_decide = screening.can_decide(user, s)
    return read


def _visible(stmt, user: User):
    """Managers see the screenings of their domains' jobs; admins and recruiters see them all."""
    if user.role != Role.MANAGER:
        return stmt
    my_domains = select(DomainManager.domain_id).where(DomainManager.user_id == user.user_id)
    return stmt.join(Job, Job.job_id == Screening.job_id).where(Job.domain_id.in_(my_domains))


def _get_or_404(db: Session, screening_id: UUID, user: User, action: str, lock: bool = False) -> Screening:
    stmt = _visible(select(Screening).where(Screening.screening_id == screening_id), user)
    if lock:  # so two managers deciding at once can't both win
        stmt = stmt.with_for_update(of=Screening).execution_options(populate_existing=True)
    found = db.scalar(stmt)
    if found is None:
        logger.warning("SCREENING_NOT_FOUND action=%s screening_id=%s user=%s", action, screening_id, user.email)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Screening not found")
    return found


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)


@router.post("/jobs/{job_id}/screenings", response_model=list[ScreeningRead], status_code=status.HTTP_201_CREATED)
def send_to_manager(
    job_id: UUID,
    payload: ScreeningCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_staff),
) -> list[ScreeningRead]:
    """Send resumes uploaded for this job to its domain's managers. Each must have a current
    score of at least the job's shortlist_min_score and not have been sent before. All or
    nothing: if any resume can't be sent, none is."""
    job = get_job_or_404(db, job_id, user, "send_to_manager")
    if not job.is_published:
        raise _conflict("This job is waiting for approval. Resumes can be sent once it is approved.")
    if job.domain is None:
        raise _conflict("This job has no domain yet. An admin needs to set one before resumes can be sent to a manager.")
    managers = screening.active_managers(job)
    if not managers:
        raise _conflict(f"The {job.domain.name} domain has no manager yet. An admin needs to assign one first.")

    ids = list(dict.fromkeys(payload.resume_ids))
    resumes = {r.resume_id: r for r in db.scalars(select(Resume).where(Resume.resume_id.in_(ids)))}
    if missing := [str(i) for i in ids if i not in resumes]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Resume not found: {', '.join(missing)}")
    if other_job := [resumes[i].original_filename for i in ids if resumes[i].job_id != job_id]:
        raise _conflict(f"These resumes weren't uploaded for this job: {', '.join(other_job)}")

    sent = set(db.scalars(select(Screening.resume_id).where(Screening.resume_id.in_(ids))))
    scores = {
        s.resume_id: s
        for s in db.scalars(select(ResumeScore).where(ResumeScore.job_id == job_id, ResumeScore.resume_id.in_(ids)))
    }
    problems = []
    for resume_id in ids:
        resume, score = resumes[resume_id], scores.get(resume_id)
        name = resume.candidate_name or resume.original_filename
        if resume_id in sent:
            problems.append(f"{name} was already sent")
        elif score is None or score.status != ScoreStatus.SCORED or score.overall_score is None:
            problems.append(f"{name} hasn't been scored")
        elif not is_current(score, job, resume):
            problems.append(f"{name} changed since it was scored (re-score it first)")
        elif score.overall_score < job.shortlist_min_score:
            problems.append(f"{name} scored {score.overall_score:g}, below the minimum of {job.shortlist_min_score}")
    if problems:
        raise _conflict(f"Nothing was sent: {'; '.join(problems)}.")

    now = utcnow()
    created = [
        Screening(
            resume_id=resume_id,
            resume=resumes[resume_id],
            job_id=job_id,
            job=job,
            status=ScreeningStatus.PENDING,
            score_at_submission=scores[resume_id].overall_score,
            min_score_at_submission=job.shortlist_min_score,
            note=payload.note or None,
            sent_by=user.user_id,
            sender=user,
            sent_at=now,
        )
        for resume_id in ids
    ]
    db.add_all(created)
    try:
        db.commit()
    except IntegrityError:  # someone sent one of them at the same moment
        db.rollback()
        raise _conflict("Some of these resumes were just sent by someone else; refresh to see them.")

    logger.info(
        "RESUMES_SENT_TO_MANAGER job_id=%s domain=%r count=%d resume_ids=%s managers=%s user=%s",
        job_id, job.domain.name, len(created), ",".join(str(i) for i in ids),
        ",".join(m.email for m in managers), user.email,
    )
    background_tasks.add_task(notifications.send, [screening.sent_email(created, job, user, m) for m in managers])
    return [_to_read(s, user) for s in created]


@router.get("/screenings", response_model=list[ScreeningRead])
def list_screenings(
    status_filter: Literal["pending", "interview", "rejected", "all"] = Query("all", alias="status"),
    job_id: UUID | None = Query(None),
    resume_id: UUID | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ScreeningRead]:
    """Newest first. Managers get the ones for their domains' jobs."""
    stmt = _visible(select(Screening), user).order_by(Screening.sent_at.desc(), Screening.screening_id)
    if status_filter != "all":
        stmt = stmt.where(Screening.status == status_filter)
    if job_id is not None:
        stmt = stmt.where(Screening.job_id == job_id)
    if resume_id is not None:
        stmt = stmt.where(Screening.resume_id == resume_id)
    found = list(db.scalars(stmt.limit(limit).offset(offset)))
    logger.info(
        "SCREENINGS_LISTED status=%s job_id=%s count=%d user=%s", status_filter, job_id, len(found), user.email
    )
    return [_to_read(s, user) for s in found]


@router.get("/screenings/{screening_id}", response_model=ScreeningRead)
def get_screening(
    screening_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ScreeningRead:
    found = _get_or_404(db, screening_id, user, "read")
    logger.info("SCREENING_READ screening_id=%s user=%s", screening_id, user.email)
    return _to_read(found, user)


@router.post("/screenings/{screening_id}/decision", response_model=ScreeningRead)
def decide(
    screening_id: UUID,
    payload: ScreeningDecision,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_roles(Role.MANAGER)),
) -> ScreeningRead:
    """A manager of the job's domain invites the candidate for an interview or rejects them
    (with a comment). The first decision stands."""
    found = _get_or_404(db, screening_id, user, "decide", lock=True)
    if found.status != ScreeningStatus.PENDING:
        raise _conflict(
            f"{found.decider.full_name if found.decider else 'Someone'} already decided: "
            f"{screening.DECISION_LABELS[ScreeningStatus(found.status)].lower()}."
        )
    if not screening.can_decide(user, found):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only this domain's managers can decide")
    found.status = payload.decision
    found.decided_by = user.user_id
    found.decider = user
    found.decided_at = utcnow()
    found.decision_comment = payload.comment or None
    db.commit()
    logger.info(
        "SCREENING_DECIDED screening_id=%s job_id=%s resume_id=%s decision=%s sent_by=%s user=%s comment=%r",
        screening_id, found.job_id, found.resume_id, found.status, found.sender.email, user.email,
        found.decision_comment,
    )
    background_tasks.add_task(notifications.send, [screening.decision_email(found)])
    return _to_read(found, user)
