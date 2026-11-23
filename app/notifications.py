"""Email through Azure Communication Services.

Emails are built while the request is handled and sent afterwards (a FastAPI background task),
so a slow or failing email never fails the change it is about. Without ACS_CONNECTION_STRING
nothing is sent and each email is logged as EMAIL_SKIPPED, so the portal works without it.
"""

import html
import logging
from dataclasses import dataclass
from functools import cache

from azure.communication.email import EmailClient

from app.config import settings

logger = logging.getLogger("app.notifications")


@dataclass(frozen=True)
class Email:
    to: str
    subject: str
    text: str
    html: str


def is_configured() -> bool:
    return bool(settings.acs_connection_string and settings.email_sender_address)


@cache
def _client() -> EmailClient:
    return EmailClient.from_connection_string(settings.acs_connection_string)


def send(emails: list[Email]) -> None:
    """Send each email on its own, so one bad address doesn't stop the rest. Never raises."""
    for email in emails:
        if not is_configured():
            logger.info("EMAIL_SKIPPED reason=not_configured to=%s subject=%r", email.to, email.subject)
            continue
        try:
            poller = _client().begin_send({
                "senderAddress": settings.email_sender_address,
                "recipients": {"to": [{"address": email.to}]},
                "content": {"subject": email.subject, "plainText": email.text, "html": email.html},
            })
            result = poller.result()
            logger.info("EMAIL_SENT to=%s subject=%r status=%s", email.to, email.subject, result.get("status"))
        except Exception:
            logger.exception("EMAIL_FAILED to=%s subject=%r", email.to, email.subject)


# --- Shared email layout ---------------------------------------------------------------


def html_table(headings: list[str], rows: list[list[str]]) -> str:
    """A label/value (or Field/Current/Proposed) table for an email body."""
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


def text_table(headings: list[str], rows: list[list[str]]) -> str:
    if headings == ["Field", "Current", "Proposed"]:
        return "\n".join(f"{f}:\n  current:  {old}\n  proposed: {new}" for f, old, new in rows)
    return "\n".join(f"{label}: {value}" for label, value in rows)


def html_page(paragraphs: list[str], table: str, button: str, footer: str) -> str:
    """The email layout: paragraphs (already HTML-escaped), a table, an "Open in the portal" button."""
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
