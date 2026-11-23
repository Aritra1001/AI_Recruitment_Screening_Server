import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import update

from app import approvals, notifications
from app.config import DEV_JWT_SECRET, settings
from app.database import SessionLocal
from app.logging_config import setup_logging
from app.models import Resume, ResumeScore, ResumeStatus, ScoreStatus
from app.routers import approvals as approvals_router
from app.routers import auth, domains, jobs, resumes, scores, screening

setup_logging()
if settings.jwt_secret == DEV_JWT_SECRET:
    logging.getLogger("app").warning("JWT_SECRET is not set - using an insecure development secret")

# How often pending approval requests are checked for expiry. The endpoints that use pending
# requests also check, so this only matters for the emails and the badges in job lists.
_EXPIRY_SWEEP_SECONDS = 10 * 60


def _expire_approvals() -> None:
    with SessionLocal() as db:
        emails = approvals.expire_stale(db)
    notifications.send(emails)


async def _expiry_sweep() -> None:
    while True:
        try:
            await asyncio.to_thread(_expire_approvals)
        except Exception:
            logging.getLogger("app.approvals").exception("APPROVAL_EXPIRY_SWEEP_FAILED")
        await asyncio.sleep(_EXPIRY_SWEEP_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Extraction and scoring run as in-process background tasks, so a restart (including
    # --reload) kills any that were running. Mark them failed so they can be retried.
    with SessionLocal() as db:
        interrupted = db.execute(
            update(Resume)
            .where(Resume.status == ResumeStatus.EXTRACTING)
            .values(status=ResumeStatus.FAILED, extraction_error="Interrupted by a server restart")
        ).rowcount
        interrupted_scores = db.execute(
            update(ResumeScore)
            .where(ResumeScore.status == ScoreStatus.SCORING)
            .values(status=ScoreStatus.FAILED, error="Interrupted by a server restart")
        ).rowcount
        db.commit()
    if interrupted:
        logging.getLogger("app.resumes").warning("RESUME_EXTRACTIONS_INTERRUPTED count=%d", interrupted)
    if interrupted_scores:
        logging.getLogger("app.scores").warning("RESUME_SCORINGS_INTERRUPTED count=%d", interrupted_scores)
    sweep = asyncio.create_task(_expiry_sweep())
    yield
    sweep.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await sweep


app = FastAPI(title="AI Recruitment Screening", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(auth.router)
app.include_router(jobs.router)
app.include_router(resumes.router)
app.include_router(scores.router)
app.include_router(approvals_router.router)
app.include_router(domains.router)
app.include_router(screening.router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
