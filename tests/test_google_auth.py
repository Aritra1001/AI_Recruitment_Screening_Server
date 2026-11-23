"""Sign in with Google (POST /auth/google). Google's token check is faked: each test says
which claims a "valid" token carries, or what the check raises."""

import pytest
from google.auth.exceptions import TransportError
from sqlalchemy import select

from app import google_auth
from app.config import settings
from app.models import Role, User
from tests.conftest import PASSWORD, RECRUITER_EMAIL

CLIENT_ID = "test-client.apps.googleusercontent.com"


def claims(**overrides) -> dict:
    return {
        "sub": "google-sub-1",
        "email": "new.person@gmail.com",
        "email_verified": True,
        "name": "New Person",
        **overrides,
    }


@pytest.fixture
def google(monkeypatch):
    """Configures Google sign-in. Set `google.result` to the claims the next token verifies
    to, or to an exception to raise. Records the calls in `google.calls`."""

    class FakeGoogle:
        result: dict | Exception = claims()
        calls: list[tuple] = []

    def verify(token, request, audience, clock_skew_in_seconds=0):
        FakeGoogle.calls.append((token, audience))
        if isinstance(FakeGoogle.result, Exception):
            raise FakeGoogle.result
        return FakeGoogle.result

    monkeypatch.setattr(settings, "google_client_id", CLIENT_ID)
    monkeypatch.setattr(settings, "google_allowed_domain", "")
    monkeypatch.setattr(google_auth.id_token, "verify_oauth2_token", verify)
    FakeGoogle.calls = []
    return FakeGoogle


def sign_in(client, credential="id-token"):
    return client.post("/auth/google", json={"credential": credential})


def get_user(session_factory, email) -> User | None:
    with session_factory() as db:
        return db.scalar(select(User).where(User.email == email))


def test_new_google_user_gets_a_recruiter_account(anon_client, google, session_factory):
    resp = sign_in(anon_client, "the-token")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user"]["email"] == "new.person@gmail.com"
    assert body["user"]["full_name"] == "New Person"
    assert body["user"]["role"] == Role.RECRUITER
    assert google.calls == [("the-token", CLIENT_ID)]

    user = get_user(session_factory, "new.person@gmail.com")
    assert user.google_sub == "google-sub-1"
    assert user.password_hash is None

    # The returned token works like a password login's.
    me = anon_client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.status_code == 200
    assert me.json()["email"] == "new.person@gmail.com"


def test_signing_in_again_reuses_the_account(anon_client, google, session_factory):
    first = sign_in(anon_client).json()["user"]
    # Name and email changes on the Google side don't create a second account.
    google.result = claims(email="renamed@gmail.com", name="Renamed")
    second = sign_in(anon_client).json()["user"]

    assert second["user_id"] == first["user_id"]
    with session_factory() as db:
        assert len(db.scalars(select(User).where(User.google_sub == "google-sub-1")).all()) == 1


def test_existing_account_is_linked_and_keeps_its_role(anon_client, google, session_factory):
    google.result = claims(email=RECRUITER_EMAIL.upper(), sub="google-sub-rec")
    resp = sign_in(anon_client)

    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["email"] == RECRUITER_EMAIL
    user = get_user(session_factory, RECRUITER_EMAIL)
    assert user.google_sub == "google-sub-rec"
    # The password still works too.
    login = anon_client.post("/auth/login", json={"email": RECRUITER_EMAIL, "password": PASSWORD})
    assert login.status_code == 200


def test_admin_linking_keeps_admin_role(anon_client, google):
    google.result = claims(email="admin@example.com", sub="google-sub-admin")
    resp = sign_in(anon_client)
    assert resp.status_code == 200
    assert resp.json()["user"]["role"] == Role.ADMIN


def test_email_linked_to_another_google_account_is_rejected(anon_client, google):
    google.result = claims(email=RECRUITER_EMAIL, sub="google-sub-a")
    assert sign_in(anon_client).status_code == 200

    google.result = claims(email=RECRUITER_EMAIL, sub="google-sub-b")
    resp = sign_in(anon_client)
    assert resp.status_code == 409
    assert "different Google account" in resp.json()["detail"]


def test_unverified_email_is_rejected(anon_client, google, session_factory):
    google.result = claims(email_verified=False)
    resp = sign_in(anon_client)
    assert resp.status_code == 401
    assert "not verified" in resp.json()["detail"]
    assert get_user(session_factory, "new.person@gmail.com") is None


def test_invalid_token_is_rejected(anon_client, google):
    google.result = ValueError("Token expired")
    resp = sign_in(anon_client)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Google sign-in failed, please try again"


def test_google_unreachable_is_503(anon_client, google):
    google.result = TransportError("connection refused")
    assert sign_in(anon_client).status_code == 503


def test_allowed_domain(anon_client, google, monkeypatch):
    monkeypatch.setattr(settings, "google_allowed_domain", "Company.com")

    google.result = claims()  # a personal gmail account has no `hd`
    resp = sign_in(anon_client)
    assert resp.status_code == 403
    assert "company.com" in resp.json()["detail"]

    google.result = claims(email="jo@company.com", hd="company.com")
    assert sign_in(anon_client).status_code == 200


def test_inactive_account_is_rejected(anon_client, google, session_factory):
    with session_factory() as db:
        user = db.scalar(select(User).where(User.email == RECRUITER_EMAIL))
        user.is_active = False
        db.commit()

    google.result = claims(email=RECRUITER_EMAIL)
    resp = sign_in(anon_client)
    assert resp.status_code == 403
    # Not linked either.
    assert get_user(session_factory, RECRUITER_EMAIL).google_sub is None


def test_not_configured_is_503(anon_client, google, monkeypatch):
    monkeypatch.setattr(settings, "google_client_id", "")
    resp = sign_in(anon_client)
    assert resp.status_code == 503
    assert google.calls == []


def test_password_login_on_google_only_account_fails_cleanly(anon_client, google):
    assert sign_in(anon_client).status_code == 200
    resp = anon_client.post("/auth/login", json={"email": "new.person@gmail.com", "password": "anything123"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid email or password"


def test_signup_with_a_google_accounts_email_is_rejected(anon_client, google):
    assert sign_in(anon_client).status_code == 200
    resp = anon_client.post(
        "/auth/signup",
        json={"full_name": "Someone", "email": "new.person@gmail.com", "password": "password123"},
    )
    assert resp.status_code == 409
