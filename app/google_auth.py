"""Sign in with Google: verifying the ID token the Google button hands the browser.

The browser gets a signed ID token (a JWT) from Google Identity Services and posts it to
`POST /auth/google`. We check it was signed by Google, issued to our OAuth client and has
not expired; Google itself has already checked the password, 2FA and so on.
"""

import ssl
from dataclasses import dataclass

import requests
import truststore
from google.auth.exceptions import GoogleAuthError, TransportError
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token
from requests.adapters import HTTPAdapter

from app.config import settings

# Tolerated difference between our clock and Google's when checking `iat`/`exp`.
CLOCK_SKEW_SECONDS = 10


class _OSTrustStoreAdapter(HTTPAdapter):
    """Checks HTTPS certificates against the operating system's trust store instead of only
    `requests`' bundled list, so a corporate proxy that re-signs HTTPS traffic with a root
    certificate installed in Windows (or macOS/Linux) is trusted as the OS trusts it."""

    def init_poolmanager(self, *args, **kwargs):
        kwargs["ssl_context"] = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        super().init_poolmanager(*args, **kwargs)


# One session for fetching Google's signing keys, reusing connections between sign-ins.
_session = requests.Session()
_session.mount("https://", _OSTrustStoreAdapter())


class GoogleSignInError(Exception):
    """The token is not acceptable. `status_code` is what the API should answer with."""

    def __init__(self, status_code: int, message: str, reason: str):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.reason = reason  # short code for the log


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str  # Google's permanent account id
    email: str
    name: str


def verify_id_token(token: str) -> GoogleIdentity:
    """Raises GoogleSignInError unless `token` is a valid Google ID token for our client with
    a verified email (and, if configured, from the allowed Workspace domain)."""
    try:
        claims = id_token.verify_oauth2_token(
            token,
            google_requests.Request(session=_session),
            settings.google_client_id,
            clock_skew_in_seconds=CLOCK_SKEW_SECONDS,
        )
    except TransportError as exc:  # couldn't download Google's signing keys
        raise GoogleSignInError(503, "Could not reach Google, please try again", f"transport:{exc}")
    except (ValueError, GoogleAuthError) as exc:  # bad signature, wrong audience/issuer, expired
        raise GoogleSignInError(401, "Google sign-in failed, please try again", f"invalid_token:{exc}")

    email = (claims.get("email") or "").strip().lower()
    if not claims.get("sub") or not email:
        raise GoogleSignInError(401, "Google did not share an email address", "no_email")
    if claims.get("email_verified") is not True:
        raise GoogleSignInError(401, "Your Google email address is not verified", "email_unverified")

    allowed = settings.google_allowed_domain.strip().lower()
    if allowed and (claims.get("hd") or "").lower() != allowed:
        raise GoogleSignInError(403, f"Only {allowed} Google accounts can sign in", "domain_not_allowed")

    name = (claims.get("name") or "").strip() or email.split("@")[0]
    return GoogleIdentity(sub=claims["sub"], email=email, name=name[:255])
