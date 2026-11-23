import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user, require_staff
from app.database import get_db
from app.models import Job, Resume, ResumeScore, ResumeStatus, ScoreStatus, User
from app.routers.jobs import get_job_or_404
from app.resume_scoring import is_configured, is_current, score_resumes, start
from app.schemas import ResumeScoreRead, ScoreRequest

router = APIRouter(prefix="/jobs", tags=["scores"])
logger = logging.getLogger("app.scores")


def _resumes_to_score(db: Session, job: Job, resume_ids: list[UUID] | None) -> list[Resume]:
    """The requested resumes (all must exist, be uploaded for this job and be extracted), or
    every extracted resume uploaded for this job."""
    if resume_ids is None:
        resumes = list(db.scalars(
            select(Resume)
            .where(Resume.job_id == job.job_id, Resume.status == ResumeStatus.EXTRACTED)
            .order_by(Resume.uploaded_at)
        ))
        if not resumes:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="There are no extracted resumes uploaded for this job to score",
            )
        return resumes

    ids = list(dict.fromkeys(resume_ids))  # de-duplicated, order kept
    found = {r.resume_id: r for r in db.scalars(select(Resume).where(Resume.resume_id.in_(ids)))}
    if missing := [str(i) for i in ids if i not in found]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Resume not found: {', '.join(missing)}")
    resumes = [found[i] for i in ids]
    if other_job := [r.original_filename for r in resumes if r.job_id != job.job_id]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"These resumes weren't uploaded for this job: {', '.join(other_job)}",
        )
    if not_ready := [r.original_filename for r in resumes if r.status != ResumeStatus.EXTRACTED]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"These resumes haven't been extracted yet: {', '.join(not_ready)}",
        )
    return resumes


@router.post("/{job_id}/scores", response_model=list[ResumeScoreRead], status_code=status.HTTP_202_ACCEPTED)
def request_scores(
    job_id: UUID,
    background_tasks: BackgroundTasks,
    payload: ScoreRequest | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(require_staff),
) -> list[ResumeScore]:
    """Score resumes uploaded for this job in the background (status = scoring). Omit resume_ids
    to score every extracted one. A resume already scored from the same job, resume and agent
    version keeps its stored score unless force is true; one being scored is left alone."""
    job = get_job_or_404(db, job_id, user, "score")
    if not is_configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Resume scoring is not configured")
    payload = payload or ScoreRequest()
    resumes = _resumes_to_score(db, job, payload.resume_ids)

    existing = {
        s.resume_id: s
        for s in db.scalars(
            select(ResumeScore).where(
                ResumeScore.job_id == job_id, ResumeScore.resume_id.in_([r.resume_id for r in resumes])
            )
        )
    }
    scores, to_score, reused = [], [], 0
    for resume in resumes:
        score = existing.get(resume.resume_id)
        if score is None:
            score = ResumeScore(job_id=job.job_id, resume_id=resume.resume_id, resume=resume)
            db.add(score)
        elif score.status == ScoreStatus.SCORING:
            scores.append(score)
            continue
        elif not payload.force and is_current(score, job, resume):
            scores.append(score)
            reused += 1
            continue
        start(score, user.user_id)
        scores.append(score)
        to_score.append(resume.resume_id)
    try:
        db.commit()
    except IntegrityError:  # lost a race with a concurrent request for the same resume
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Scoring was just started for these resumes; refresh to see it"
        )

    logger.info(
        "RESUME_SCORING_REQUESTED job_id=%s count=%d reused=%d already_scoring=%d force=%s user=%s",
        job_id, len(to_score), reused, len(scores) - len(to_score) - reused, payload.force, user.email,
    )
    if to_score:
        background_tasks.add_task(score_resumes, job_id, to_score, db.get_bind())
    return scores


@router.get("/{job_id}/scores", response_model=list[ResumeScoreRead])
def list_scores(
    job_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> list[ResumeScore]:
    """Every resume uploaded for this job that has been scored, best first (still scoring /
    failed last). Scores of resumes from other jobs (kept from before resumes belonged to a
    job) are left out."""
    get_job_or_404(db, job_id, user, "list_scores")
    stmt = (
        select(ResumeScore)
        .join(Resume, Resume.resume_id == ResumeScore.resume_id)
        .where(ResumeScore.job_id == job_id, Resume.job_id == job_id)
        .order_by(ResumeScore.overall_score.desc().nulls_last(), ResumeScore.created_at)
    )
    scores = list(db.scalars(stmt))
    logger.info("RESUME_SCORES_LISTED job_id=%s count=%d user=%s", job_id, len(scores), user.email)
    return scores


@router.get("/{job_id}/scores/{resume_id}", response_model=ResumeScoreRead)
def get_score(
    job_id: UUID, resume_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ResumeScore:
    score = db.scalar(
        select(ResumeScore)
        .join(Resume, Resume.resume_id == ResumeScore.resume_id)
        .where(ResumeScore.job_id == job_id, ResumeScore.resume_id == resume_id, Resume.job_id == job_id)
    )
    if score is None:
        logger.warning("SCORE_NOT_FOUND job_id=%s resume_id=%s user=%s", job_id, resume_id, user.email)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This resume hasn't been scored for this job")
    logger.info("RESUME_SCORE_READ job_id=%s resume_id=%s user=%s", job_id, resume_id, user.email)
    return score
