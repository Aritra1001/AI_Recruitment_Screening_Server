"""Screening: a recruiter sends a resume that meets the job's minimum score to the managers of the
job's domain, and one of them decides whether the candidate gets an interview.

    scored (>= job.shortlist_min_score) --send--> pending --manager--> interview | rejected

A resume is sent once and decided once (one Screening per resume). The managers are emailed
when resumes are sent to them, and the recruiter who sent one is emailed the decision.
"""

import html

from app.config import settings
from app.models import Job, Role, Screening, ScreeningStatus, User
from app.notifications import Email, html_page, html_table, text_table

DECISION_LABELS = {ScreeningStatus.INTERVIEW: "Interview", ScreeningStatus.REJECTED: "Rejected"}


def active_managers(job: Job) -> list[User]:
    return [m for m in job.domain.managers if m.is_active] if job.domain else []


def can_decide(user: User, screening: Screening) -> bool:
    return (
        screening.status == ScreeningStatus.PENDING
        and user.role == Role.MANAGER
        and any(m.user_id == user.user_id for m in active_managers(screening.job))
    )


def candidate(screening: Screening) -> str:
    return screening.resume.candidate_name or screening.resume.original_filename


def _portal(path: str) -> str:
    return f"{settings.portal_url.rstrip('/')}{path}"


def _score(value: float) -> str:
    return f"{value:g}"


def sent_email(screenings: list[Screening], job: Job, sender: User, manager: User) -> Email:
    """Asks a domain manager to screen the resumes just sent for `job`."""
    count = len(screenings)
    what = "a candidate" if count == 1 else f"{count} candidates"
    intro = (
        f"{sender.full_name} ({sender.email}) sent {what} for \"{job.title}\" ({job.domain.name}) for you to "
        f"screen. Each scored at least the job's minimum of {job.shortlist_min_score}."
    )
    note = f"Note from {sender.full_name}: {screenings[0].note}" if screenings[0].note else ""
    rows = [[candidate(s), f"{_score(s.score_at_submission)} / 100"] for s in screenings]
    link = _portal(f"/screening/{screenings[0].screening_id}" if count == 1 else "/screening")
    action = "Decide whether to interview them here"
    return Email(
        to=manager.email,
        subject=f"To screen: {what} for {job.title}",
        text="\n\n".join(filter(None, [
            f"Hi {manager.full_name},", intro, note, text_table(["", ""], rows), f"{action}: {link}",
        ])),
        html=html_page(
            [html.escape(p) for p in filter(None, [f"Hi {manager.full_name},", intro, note])],
            html_table(["Candidate", "Match score"], rows),
            link,
            "You're receiving this because you manage this job's domain in the Recruitment Portal.",
        ),
    )


def decision_email(screening: Screening) -> Email:
    """Tells the recruiter who sent the resume what the manager decided."""
    sender, decider, job = screening.sender, screening.decider, screening.job
    outcome = "invited for an interview" if screening.status == ScreeningStatus.INTERVIEW else "rejected"
    sentence = f'{decider.full_name} {outcome} {candidate(screening)} for "{job.title}".'
    comment = f"Comment: {screening.decision_comment}" if screening.decision_comment else ""
    rows = [
        ["Candidate", candidate(screening)],
        ["Job", job.title],
        ["Match score", f"{_score(screening.score_at_submission)} / 100"],
        ["Decision", DECISION_LABELS[ScreeningStatus(screening.status)]],
    ]
    link = _portal(f"/screening/{screening.screening_id}")
    return Email(
        to=sender.email,
        subject=f"{DECISION_LABELS[ScreeningStatus(screening.status)]}: {candidate(screening)} – {job.title}",
        text="\n\n".join(filter(None, [f"Hi {sender.full_name},", sentence, comment, text_table(["", ""], rows), link])),
        html=html_page(
            [html.escape(p) for p in filter(None, [f"Hi {sender.full_name},", sentence, comment])],
            html_table(["", ""], rows),
            link,
            "You're receiving this because you sent this candidate to the domain manager.",
        ),
    )
