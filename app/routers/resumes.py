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
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import Resume, ResumeStatus, Role, User
from app.resume_extraction import extract_resume, is_configured
from app.schemas import ResumeDetail, ResumeRead

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
    if resume is None:
        logger.warning("RESUME_NOT_FOUND action=%s resume_id=%s user=%s", action, resume_id, user.email)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Resume not found")
    return resume


def _reject(code: int, detail: str, filename: str, user: User) -> HTTPException:
    logger.warning("RESUME_REJECTED file=%r reason=%r user=%s", filename, detail, user.email)
    return HTTPException(status_code=code, detail=detail)


def _duplicate(existing: Resume, filename: str, user: User) -> HTTPException:
    logger.warning(
        "RESUME_DUPLICATE file=%r existing_resume_id=%s user=%s", filename, existing.resume_id, user.email
    )
    # A dict detail so the portal can link to the resume that is already there.
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail={
            "message": f'This file was already uploaded as "{existing.original_filename}".',
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
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resume:
    """Upload one PDF or DOCX resume. The portal uploads several files as parallel requests.
    Extraction starts in the background (status = extracting) when Azure is configured."""
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
    existing = db.scalar(select(Resume).where(Resume.sha256 == sha256))
    if existing is not None:
        raise _duplicate(existing, filename, user)

    resume = Resume(
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
        raise _duplicate(db.scalar(select(Resume).where(Resume.sha256 == sha256)), filename, user)

    db.refresh(resume)
    logger.info(
        "RESUME_UPLOADED resume_id=%s file=%r size=%d user=%s",
        resume.resume_id, filename, resume.size_bytes, user.email,
    )
    if resume.status == ResumeStatus.EXTRACTING:
        background_tasks.add_task(extract_resume, resume.resume_id, db.get_bind())
    return resume


@router.get("", response_model=list[ResumeRead])
def list_resumes(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Resume]:
    stmt = (
        select(Resume)
        .order_by(Resume.uploaded_at.desc(), Resume.resume_id)
        .limit(limit)
        .offset(offset)
    )
    resumes = list(db.scalars(stmt))
    logger.info(
        "RESUMES_LISTED count=%d limit=%d offset=%d user=%s", len(resumes), limit, offset, user.email
    )
    return resumes


@router.get("/{resume_id}", response_model=ResumeDetail)
def get_resume(
    resume_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Resume:
    resume = _get_resume_or_404(db, resume_id, user, "read")
    logger.info("RESUME_READ resume_id=%s user=%s", resume_id, user.email)
    return resume


@router.post("/{resume_id}/extract", response_model=ResumeDetail, status_code=status.HTTP_202_ACCEPTED)
def extract(
    resume_id: UUID,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Resume:
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
    return resume


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


@router.delete("/{resume_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_resume(
    resume_id: UUID, db: Session = Depends(get_db), user: User = Depends(get_current_user)
) -> Response:
    resume = _get_resume_or_404(db, resume_id, user, "delete")
    if user.role != Role.ADMIN and resume.uploaded_by != user.user_id:
        logger.warning(
            "ACCESS_DENIED user=%s role=%s action=DELETE resume_id=%s reason=not_uploader",
            user.email, user.role, resume_id,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can only delete resumes you uploaded"
        )

    filename = resume.original_filename
    db.delete(resume)  # the file goes with the row
    db.commit()
    logger.info("RESUME_DELETED resume_id=%s file=%r user=%s", resume_id, filename, user.email)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
