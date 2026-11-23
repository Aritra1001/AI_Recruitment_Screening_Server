"""Approvals: creating, editing or deleting a job, and deleting a resume, waits for a second admin.

The change is stored as an ApprovalRequest and every other active admin is emailed a link to
review it. Nothing goes live until one of them approves:
- job_create: the job is saved unpublished (Job.is_published = False), so only admins see it.
- job_update: the proposed values wait in the request; the job keeps its current ones.
- job_delete / resume_delete: the job or resume stays (and stays usable) until approved.
Rejecting, withdrawing or letting a new job's request expire deletes the unpublished job.

A job or resume has at most one pending request (a partial unique index), so a second change
to it is refused until the first is decided. Requests expire after APPROVAL_EXPIRY_HOURS.
"""

import html
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from fastapi import BackgroundTasks, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import notifications
from app.config import settings
from app.models import (
    ApprovalAction,
    ApprovalRequest,
    ApprovalStatus,
    Job,
    Resume,
    Role,
    User,
)
from app.notifications import Email

logger = logging.getLogger("app.approvals")
jobs_logger = logging.getLogger("app.jobs")
resumes_logger = logging.getLogger("app.resumes")

JOB_FIELDS = ("title", "description", "required_skills", "preferred_skills", "minimum_experience")
FIELD_LABELS = {
    "title": "Title",
    "description": "Description",
    "required_skills": "Required skills",
    "preferred_skills": "Preferred skills",
    "minimum_experience": "Minimum experience",
}
ACTION_LABELS = {
    ApprovalAction.JOB_CREATE: "New job",
    ApprovalAction.JOB_UPDATE: "Job changes",
    ApprovalAction.JOB_DELETE: "Job deletion",
    ApprovalAction.RESUME_DELETE: "Resume deletion",
}
_ACTION_VERBS = {
    ApprovalAction.JOB_CREATE: "create the job",
    ApprovalAction.JOB_UPDATE: "change the job",
    ApprovalAction.JOB_DELETE: "delete the job",
    ApprovalAction.RESUME_DELETE: "delete the resume",
}
# Long descriptions are cut short in emails; the portal shows them in full.
_EMAIL_TEXT_LIMIT = 600


def utcnow() -> datetime:
    """Naive UTC, like the other timestamps the approval tables store."""
    return datetime.now(UTC).replace(tzinfo=None)


def job_values(job: Job) -> dict[str, Any]:
    return {field: getattr(job, field) for field in JOB_FIELDS}


def pending_requests(db: Session, *, job_ids=(), resume_ids=()) -> dict[UUID, ApprovalRequest]:
    """job_id / resume_id -> its pending request, for those that have one."""
    found: dict[UUID, ApprovalRequest] = {}
    for column, ids in ((ApprovalRequest.job_id, job_ids), (ApprovalRequest.resume_id, resume_ids)):
        if ids:
            stmt = select(ApprovalRequest).where(
                ApprovalRequest.status == ApprovalStatus.PENDING, column.in_(list(ids))
            )
            for request in db.scalars(stmt):
                found[request.job_id or request.resume_id] = request
    return found


def _other_admins(db: Session, user: User) -> list[User]:
    stmt = select(User).where(User.role == Role.ADMIN, User.is_active, User.user_id != user.user_id)
    return list(db.scalars(stmt.order_by(User.email)))


def submit(
    db: Session,
    user: User,
    action: ApprovalAction,
    target_name: str,
    details: dict[str, Any],
    *,
    job_id: UUID | None = None,
    resume_id: UUID | None = None,
) -> tuple[ApprovalRequest, list[Email]]:
    """Store the request (committing anything else added to the session with it) and return
    the emails asking the other admins to review it."""
    reviewers = _other_admins(db, user)
    if not reviewers:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="There is no other admin to approve this change. Another admin account is needed first.",
        )
    now = utcnow()
    request = ApprovalRequest(
        action=action,
        job_id=job_id,
        resume_id=resume_id,
        target_name=target_name[:255],
        details=details,
        requested_by=user.user_id,
        requester=user,
        requested_at=now,
        expires_at=now + timedelta(hours=settings.approval_expiry_hours),
    )
    db.add(request)
    try:
        db.commit()
    except IntegrityError:  # the pending-request unique index: lost a race with another change
        db.rollback()
        raise already_pending()
    db.refresh(request)
    logger.info(
        "APPROVAL_REQUESTED request_id=%s action=%s target=%r user=%s reviewers=%s",
        request.request_id, action, request.target_name, user.email, ",".join(a.email for a in reviewers),
    )
    return request, [review_email(request, admin) for admin in reviewers]


def already_pending() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="A change to this is already waiting for approval. It must be approved, rejected or withdrawn first.",
    )


def lock(db: Session, request_id: UUID) -> ApprovalRequest | None:
    """Load the request with a row lock, so two admins deciding at once can't both win."""
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.request_id == request_id)
        .with_for_update(of=ApprovalRequest)
        .execution_options(populate_existing=True)
    )
    return db.scalar(stmt)


def check_decidable(request: ApprovalRequest) -> None:
    if request.status == ApprovalStatus.PENDING and request.expires_at <= utcnow():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="This request has expired")
    if request.status != ApprovalStatus.PENDING:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"This request was already {request.status}")


def approve(db: Session, request: ApprovalRequest, reviewer: User, comment: str | None) -> list[Email]:
    """Apply the change and return the email telling the requester. `request` must be locked."""
    check_decidable(request)
    if reviewer.user_id == request.requested_by:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can't approve your own request")
    _close(request, ApprovalStatus.APPROVED, reviewer, comment)
    _apply(db, request)
    db.commit()
    logger.info(
        "APPROVAL_APPROVED request_id=%s action=%s target=%r requested_by=%s user=%s",
        request.request_id, request.action, request.target_name, request.requester.email, reviewer.email,
    )
    return [decision_email(request)]


def reject(db: Session, request: ApprovalRequest, reviewer: User, comment: str | None) -> list[Email]:
    check_decidable(request)
    if reviewer.user_id == request.requested_by:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can't reject your own request; withdraw it instead"
        )
    _close(request, ApprovalStatus.REJECTED, reviewer, comment)
    _discard_unpublished_job(db, request)
    db.commit()
    logger.info(
        "APPROVAL_REJECTED request_id=%s action=%s target=%r requested_by=%s user=%s comment=%r",
        request.request_id, request.action, request.target_name, request.requester.email, reviewer.email, comment,
    )
    return [decision_email(request)]


def cancel(db: Session, request: ApprovalRequest, user: User) -> None:
    """The requester withdraws their own request."""
    check_decidable(request)
    if user.user_id != request.requested_by:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Only the person who made a request can withdraw it"
        )
    _close(request, ApprovalStatus.CANCELLED, user, None)
    _discard_unpublished_job(db, request)
    db.commit()
    logger.info(
        "APPROVAL_CANCELLED request_id=%s action=%s target=%r user=%s",
        request.request_id, request.action, request.target_name, user.email,
    )


def expire_stale(db: Session) -> list[Email]:
    """Expire every pending request past its expires_at; returns the emails telling the requesters."""
    now = utcnow()
    stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.status == ApprovalStatus.PENDING, ApprovalRequest.expires_at <= now)
        .with_for_update(of=ApprovalRequest, skip_locked=True)
    )
    expired = list(db.scalars(stmt))
    for request in expired:
        _close(request, ApprovalStatus.EXPIRED, None, None)
        _discard_unpublished_job(db, request)
    if not expired:
        return []
    db.commit()
    for request in expired:
        logger.info(
            "APPROVAL_EXPIRED request_id=%s action=%s target=%r requested_by=%s",
            request.request_id, request.action, request.target_name, request.requester.email,
        )
    return [decision_email(request) for request in expired]


def expire_stale_now(db: Session, background_tasks: BackgroundTasks) -> None:
    """expire_stale before a request reads or adds pending requests (the periodic sweep in
    app.main may not have run yet), emailing the requesters afterwards."""
    if emails := expire_stale(db):
        background_tasks.add_task(notifications.send, emails)


def _close(request: ApprovalRequest, new_status: ApprovalStatus, reviewer: User | None, comment: str | None) -> None:
    request.status = new_status
    request.reviewed_by = reviewer.user_id if reviewer else None
    request.reviewer = reviewer
    request.reviewed_at = utcnow()
    request.review_comment = comment or None


def _discard_unpublished_job(db: Session, request: ApprovalRequest) -> None:
    """A new job that won't be approved is deleted; it was never visible to recruiters."""
    if request.action != ApprovalAction.JOB_CREATE or request.job_id is None:
        return
    job = db.get(Job, request.job_id)
    if job is not None and not job.is_published:
        db.delete(job)
    request.job_id = None


def _apply(db: Session, request: ApprovalRequest) -> None:
    requester, reviewer = request.requester.email, request.reviewer.email
    gone = HTTPException(
        status_code=status.HTTP_409_CONFLICT, detail=f'"{request.target_name}" no longer exists'
    )
    match request.action:
        case ApprovalAction.JOB_CREATE:
            job = db.get(Job, request.job_id) if request.job_id else None
            if job is None:
                raise gone
            job.is_published = True
            jobs_logger.info(
                "JOB_CREATED job_id=%s title=%r user=%s approved_by=%s", job.job_id, job.title, requester, reviewer
            )
        case ApprovalAction.JOB_UPDATE:
            job = db.get(Job, request.job_id) if request.job_id else None
            if job is None:
                raise gone
            for field, change in request.details.items():
                setattr(job, field, change["new"])
            jobs_logger.info(
                "JOB_UPDATED job_id=%s user=%s approved_by=%s changes=%s",
                job.job_id, requester, reviewer, request.details,
            )
        case ApprovalAction.JOB_DELETE:
            job = db.get(Job, request.job_id) if request.job_id else None
            if job is None:
                raise gone
            db.delete(job)  # its scores go with it (ON DELETE CASCADE)
            request.job_id = None
            jobs_logger.info(
                "JOB_DELETED job_id=%s user=%s approved_by=%s deleted=%s",
                job.job_id, requester, reviewer, request.details,
            )
        case ApprovalAction.RESUME_DELETE:
            resume = db.get(Resume, request.resume_id) if request.resume_id else None
            if resume is None:
                raise gone
            db.delete(resume)  # the file and its scores go with the row
            request.resume_id = None
            resumes_logger.info(
                "RESUME_DELETED resume_id=%s file=%r user=%s approved_by=%s",
                resume.resume_id, resume.original_filename, requester, reviewer,
            )


# --- Emails -------------------------------------------------------------------------------


def _link(request: ApprovalRequest) -> str:
    return f"{settings.portal_url.rstrip('/')}/approvals/{request.request_id}"


def _format(field: str, value: Any) -> str:
    if value is None:
        return "(not set)"
    if isinstance(value, list):
        return ", ".join(value) if value else "(none)"
    if field == "minimum_experience":
        return f"{value}+ years"
    text = str(value)
    return text if len(text) <= _EMAIL_TEXT_LIMIT else text[:_EMAIL_TEXT_LIMIT] + "…"


def _detail_rows(request: ApprovalRequest) -> tuple[list[str], list[list[str]]]:
    """(column headings, rows) describing the change, shared by the HTML and text bodies."""
    details = request.details
    if request.action == ApprovalAction.JOB_UPDATE:
        return ["Field", "Current", "Proposed"], [
            [FIELD_LABELS[f], _format(f, c["old"]), _format(f, c["new"])] for f, c in details.items()
        ]
    if request.action == ApprovalAction.RESUME_DELETE:
        return ["", ""], [
            ["File", details.get("original_filename") or request.target_name],
            ["Job", details.get("job_title") or "(none)"],
            ["Candidate", details.get("candidate_name") or "(not extracted)"],
            ["Uploaded by", details.get("uploaded_by") or ""],
        ]
    return ["", ""], [[FIELD_LABELS[f], _format(f, details.get(f))] for f in JOB_FIELDS]


def _html_table(headings: list[str], rows: list[list[str]]) -> str:
    cell = "padding:8px;border-bottom:1px solid #e2e5ec;vertical-align:top;text-align:left"
    head = ""
    if any(headings):
        head = "<tr>" + "".join(f'<th style="{cell};color:#6b7385">{html.escape(h)}</th>' for h in headings) + "</tr>"
    body = "".join(
        "<tr>"
        + "".join(
            f'<td style="{cell}{";font-weight:600;white-space:nowrap" if i == 0 else ""}">'
            f"{html.escape(value).replace(chr(10), '<br>')}</td>"
            for i, value in enumerate(row)
        )
        + "</tr>"
        for row in rows
    )
    return f'<table style="border-collapse:collapse;width:100%;font-size:14px">{head}{body}</table>'


def _text_table(headings: list[str], rows: list[list[str]]) -> str:
    if headings == ["Field", "Current", "Proposed"]:
        return "\n".join(f"{f}:\n  current:  {old}\n  proposed: {new}" for f, old, new in rows)
    return "\n".join(f"{label}: {value}" for label, value in rows)


def _html(paragraphs: list[str], table: str, button: str, footer: str) -> str:
    paras = "".join(f'<p style="margin:0 0 12px">{p}</p>' for p in paragraphs)
    return (
        '<div style="font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;color:#1d2433;'
        'max-width:600px;margin:0 auto;padding:16px">'
        f"{paras}{table}"
        f'<p style="margin:24px 0"><a href="{html.escape(button)}" style="background:#3554d1;color:#fff;'
        'padding:12px 20px;border-radius:6px;text-decoration:none;font-weight:600;display:inline-block">'
        "Open in the portal</a></p>"
        f'<p style="color:#6b7385;font-size:12px">{footer}</p></div>'
    )


def review_email(request: ApprovalRequest, admin: User) -> Email:
    action = ACTION_LABELS[request.action]
    requester = request.requester
    headings, rows = _detail_rows(request)
    expires = f"{request.expires_at:%d %b %Y %H:%M} UTC"
    intro = f"{requester.full_name} ({requester.email}) asked to {_ACTION_VERBS[request.action]} \"{request.target_name}\"."
    note = "It only takes effect once another admin approves it."
    link = _link(request)
    return Email(
        to=admin.email,
        subject=f"Approval needed: {action} – {request.target_name}",
        text=(
            f"Hi {admin.full_name},\n\n{intro} {note}\n\n{_text_table(headings, rows)}\n\n"
            f"Review, approve or reject it here: {link}\n\nThe request expires on {expires}."
        ),
        html=_html(
            [f"Hi {html.escape(admin.full_name)},", f"{html.escape(intro)} {note}"],
            _html_table(headings, rows),
            link,
            f"Approve or reject it in the portal (you'll be asked to sign in). The request expires on {expires}.",
        ),
    )


def decision_email(request: ApprovalRequest) -> Email:
    """Tells the requester their request was approved, rejected or expired."""
    action = ACTION_LABELS[request.action]
    requester = request.requester
    if request.status == ApprovalStatus.EXPIRED:
        outcome = f"expired after {settings.approval_expiry_hours} hours without a decision, so nothing was changed"
    else:
        outcome = f"was {request.status} by {request.reviewer.full_name}"
        if request.status == ApprovalStatus.APPROVED:
            outcome += " and is now live"
        else:
            outcome += ", so nothing was changed"
    sentence = f'Your request ({action.lower()}: "{request.target_name}") {outcome}.'
    comment = f"Comment: {request.review_comment}" if request.review_comment else ""
    headings, rows = _detail_rows(request)
    link = _link(request)
    return Email(
        to=requester.email,
        subject=f"{request.status.capitalize()}: {action} – {request.target_name}",
        text="\n\n".join(filter(None, [f"Hi {requester.full_name},", sentence, comment, _text_table(headings, rows), link])),
        html=_html(
            [html.escape(p) for p in filter(None, [f"Hi {requester.full_name},", sentence, comment])],
            _html_table(headings, rows),
            link,
            "You're receiving this because you asked for this change in the Recruitment Portal.",
        ),
    )
