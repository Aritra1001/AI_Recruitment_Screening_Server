"""Email through Azure Communication Services.

Emails are built while the request is handled and sent afterwards (a FastAPI background task),
so a slow or failing email never fails the change it is about. Without ACS_CONNECTION_STRING
nothing is sent and each email is logged as EMAIL_SKIPPED, so the portal works without it.
"""

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
