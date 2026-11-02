import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import update

from app.config import DEV_JWT_SECRET, settings
from app.database import SessionLocal
from app.logging_config import setup_logging
from app.models import Resume, ResumeStatus
from app.routers import auth, jobs, resumes

setup_logging()
if settings.jwt_secret == DEV_JWT_SECRET:
    logging.getLogger("app").warning("JWT_SECRET is not set - using an insecure development secret")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Extraction runs as an in-process background task, so a restart (including --reload)
    # kills any that were running. Mark them failed so they can be retried from the portal.
    with SessionLocal() as db:
        interrupted = db.execute(
            update(Resume)
            .where(Resume.status == ResumeStatus.EXTRACTING)
            .values(status=ResumeStatus.FAILED, extraction_error="Interrupted by a server restart")
        ).rowcount
        db.commit()
    if interrupted:
        logging.getLogger("app.resumes").warning("RESUME_EXTRACTIONS_INTERRUPTED count=%d", interrupted)
    yield


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
