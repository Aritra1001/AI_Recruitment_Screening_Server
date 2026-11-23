import hashlib
import logging
from pathlib import Path
from urllib.parse import quote
from uuid import UUID

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import approvals, notifications
from app.auth import get_current_user, require_staff
from app.config import settings
from app.database import get_db
from app.models import ApprovalAction, ApprovalRequest, Job, Resume, ResumeStatus, Role, User
from app.resume_extraction import extract_resume, is_configured
from app.routers.jobs import get_job_or_404
from app.schemas import PendingApproval, ResumeDetail, ResumeRead

router = APIRouter(prefix="/resumes", tags=["resumes"])
logger = logging.getLogger("app.resumes")

# Extension -> (content type we store/serve, bytes the file must start with). The content
# type the browser sends is ignored. DOCX files are zip archives.
ALLOWED_TYPES = {
    ".pdf": ("application/pdf", b"%PDF-"),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        b"PK\x03\x04",
    ),
}
_CHUNK_BYTES = 1024 * 1024


def _content_disposition(filename: str) -> str:
    """Same header FileResponse builds: plain for ASCII names, RFC 5987 for anything else."""
    quoted = quote(filename)
    if quoted != filename:
        return f"attachment; filename*=utf-8''{quoted}"
    return f'attachment; filename="{filename}"'


def _get_resume_or_404(db: Session, resume_id: UUID, user: User, action: str) -> Resume:
    resume = db.get(Resume, resume_id)
    # Like the job itself, resumes uploaded for a job awaiting approval are admin-only.
    if resume is None or (resume.job is not None and not resume.job.is_published and user.role != Role.ADMIN):
        logger.warning("RESUME_NOT_FOUND action=%s resume_id=%s user=%s", action, resume_id, user.email)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found")
    return resume


def _with_pending[R: ResumeRead](read: R, user: User, pending: ApprovalRequest | None) -> R:
    """Fill in pending_deletion for admins and for whoever asked for the deletion."""
    if pending is not None and (user.role == Role.ADMIN or pending.requested_by == user.user_id):
        read.pending_deletion = PendingApproval.model_validate(pending)
    return read


def _detail(db: Session, resume: Resume, user: User) -> ResumeDetail:
    pending = approvals.pending_requests(db, resume_ids=[resume.resume_id]).get(resume.resume_id)
    return _with_pending(ResumeDetail.model_validate(resume), user, pending)


def _reject(code: int, detail: str, filename: str, user: User) -> HTTPException:
    logger.warning("RESUME_REJECTED file=%r reason=%r user=%s", filename, detail, user.email)
    return HTTPException(status_code=code, detail=detail)


def _duplicate(existing: Resume, filename: str, user: User) -> HTTPException:
    logger.warning(
        "RESUME_DUPLICATE file=%r job_id=%s existing_resume_id=%s user=%s",
        filename, existing.job_id, existing.resume_id, user.email,
    )
    # A dict detail so the portal can link to the resume that is already there.
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": f'This file was already uploaded for this job as "{existing.original_filename}".',
            "resume_id": str(existing.resume_id),
        },
    )


def _display_name(raw: str | None, ext: str) -> str:
    """Strip any client-side directory and keep the name within the 255-char column."""
    name = Path(raw or f"resume{ext}").name
    if len(name) > 255:
        name = Path(name).stem[: 255 - len(ext)] + ext
    return name


@router.post("", response_model=ResumeRead, status_code=status.HTTP_201_CREATED)
def upload_resume(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    job_id: UUID = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_staff),
) -> Resume:
    """Upload one PDF or DOCX resume for a job; it is only ever scored against that job. The
    portal uploads several files as parallel requests. Extraction starts in the background
    (status = extracting) when Azure is configured."""
    job = get_job_or_404(db, job_id, user, "upload_resume")
    ext = Path(file.filename or "").suffix.lower()
    filename = _display_name(file.filename, ext)
    if ext not in ALLOWED_TYPES:
        raise _reject(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Only PDF and DOCX files are supported", filename, user)
    content_type, magic = ALLOWED_TYPES[ext]
    max_bytes = settings.max_resume_mb * 1024 * 1024

    # Read in chunks so an oversized upload is rejected without buffering all of it.
    content = bytearray()
    while chunk := file.file.read(_CHUNK_BYTES):
        content += chunk
        if len(content) > max_bytes:
            raise _reject(
                status.HTTP_413_CONTENT_TOO_LARGE,
                f"File is larger than {settings.max_resume_mb} MB",
                filename,
                user,
            )

    if not content:
        raise _reject(status.HTTP_400_BAD_REQUEST, "File is empty", filename, user)
    if not content.startswith(magic):
        raise _reject(
            status.HTTP_400_BAD_REQUEST,
            f"File content is not a valid {ext[1:].upper()} file",
            filename,
            user,
        )

    sha256 = hashlib.sha256(content).hexdigest()
    same_file = select(Resume).where(Resume.job_id == job.job_id, Resume.sha256 == sha256)
    existing = db.scalar(same_file)
    if existing is not None:
        raise _duplicate(existing, filename, user)

    resume = Resume(
        job_id=job.job_id,
        original_filename=filename,
        content_type=content_type,
        size_bytes=len(content),
        sha256=sha256,
        content=bytes(content),
        status=ResumeStatus.EXTRACTING if is_configured() else ResumeStatus.UPLOADED,
        uploaded_by=user.user_id,
    )
    db.add(resume)
    try:
        db.commit()
    except IntegrityError:  # lost a race with a concurrent upload of the same file
        db.rollback()
        raise _duplicate(db.scalar(same_file), filename, user)

    db.refresh(resume)
    logger.info(
        "RESUME_UPLOADED resume_id=%s job_id=%s file=%r size=%d user=%s",
        resume.resume_id, job.job_id, filename, resume.size_bytes, user.email,
    )
    if resume.status == ResumeStatus.EXTRACTING:
        background_tasks.add_task(extract_resume, resume.resume_id, db.get_bind())
    return resume


@router.get("", response_model=list[ResumeRead])
def list_resumes(
    job_id: UUID | None = Query(None, description="Only resumes uploaded for this job"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[ResumeRead]:
    stmt = (
        select(Resume)
        .order_by(Resume.uploaded_at.desc(), Resume.resume_id)
        .limit(limit)
        .offset(offset)
    )
    if job_id is not None:
        get_job_or_404(db, job_id, user, "list_resumes")
        stmt = stmt.where(Resume.job_id == job_id)
    elif user.role != Role.ADMIN:
        # Resumes uploaded for a job awaiting approval are admin-only, like the job.
        published = select(Job.job_id).where(Job.is_published)
        stmt = stmt.where(or_(Resume.job_id.is_(None), Resume.job_id.in_(published)))
    resumes = list(db.scalars(stmt))
    pending = approvals.pending_requests(db, resume_ids=[r.resume_id for r in resumes])
    logger.info(
        "RESUMES_LISTED job_id=%s count=%d limit=%d offset=%d user=%s",
        job_id, len(resumes), limit, offset, user.email,
    )
    return [_with_pending(ResumeRead.model_validate(r), user, pending.get(r.resume_id)) for r in resumes]


@router.get("/{resume_id}", response_model=ResumeDetail)
def get_resume(
    resume_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> ResumeDetail:
    resume = _get_resume_or_404(db, resume_id, user, "read")
    logger.info("RESUME_READ resume_id=%s user=%s", resume_id, user.email)
    return _detail(db, resume, user)


@router.post("/{resume_id}/extract", response_model=ResumeDetail, status_code=status.HTTP_202_ACCEPTED)
def extract(
    resume_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_staff),
) -> ResumeDetail:
    """(Re-)run extraction: for resumes uploaded before it was configured, failures, or a refresh."""
    resume = _get_resume_or_404(db, resume_id, user, "extract")
    if not is_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Resume extraction is not configured"
        )
    if resume.status == ResumeStatus.EXTRACTING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This resume is already being extracted")

    resume.status = ResumeStatus.EXTRACTING
    resume.extraction_error = None
    db.commit()
    logger.info("RESUME_EXTRACTION_REQUESTED resume_id=%s user=%s", resume_id, user.email)
    background_tasks.add_task(extract_resume, resume.resume_id, db.get_bind())
    return _detail(db, resume, user)


@router.get("/{resume_id}/file")
def download_resume_file(
    resume_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    resume = _get_resume_or_404(db, resume_id, user, "download")
    logger.info("RESUME_FILE_DOWNLOADED resume_id=%s user=%s", resume_id, user.email)
    return Response(
        content=resume.content,  # loads the deferred column
        media_type=resume.content_type,
        headers={
            "Content-Disposition": _content_disposition(resume.original_filename),
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.delete("/{resume_id}", response_model=ResumeRead, status_code=status.HTTP_202_ACCEPTED)
def delete_resume(
    resume_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(require_staff),
) -> ResumeRead:
    """Ask the admins (other than the caller) to approve deleting the resume. It stays, and can
    still be scored, until one of them does."""
    approvals.expire_stale_now(db, background_tasks)
    resume = _get_resume_or_404(db, resume_id, user, "delete")
    if user.role != Role.ADMIN and resume.uploaded_by != user.user_id:
        logger.warning(
            "ACCESS_DENIED user=%s role=%s action=DELETE resume_id=%s reason=not_uploader",
            user.email, user.role, resume_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete resumes you uploaded"
        )
    if approvals.pending_requests(db, resume_ids=[resume.resume_id]):
        raise approvals.already_pending()

    details = {
        "original_filename": resume.original_filename,
        "job_title": resume.job.title if resume.job else None,
        "candidate_name": resume.candidate_name,
        "uploaded_by": resume.uploader.full_name,
        "uploaded_at": resume.uploaded_at.isoformat() if resume.uploaded_at else None,
    }
    request, emails = approvals.submit(
        db, user, ApprovalAction.RESUME_DELETE, resume.original_filename, details, resume_id=resume.resume_id
    )
    logger.info(
        "RESUME_DELETE_REQUESTED resume_id=%s file=%r request_id=%s user=%s",
        resume_id, resume.original_filename, request.request_id, user.email,
    )
    background_tasks.add_task(notifications.send, emails)
    return _with_pending(ResumeRead.model_validate(resume), user, request)
