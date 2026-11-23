import os
import secrets
import tempfile

# Must be set before app.config is imported so test runs don't write to ./logs.
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="recruitment-test-logs-"))

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import notifications
from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Job, Role, User

# Generated per run so no credential-like literal lives in the repo (secret scanners flag them).
PASSWORD = secrets.token_urlsafe(16)
ADMIN_EMAIL = "admin@example.com"
# Changes need a second admin's approval.
ADMIN2_EMAIL = "admin2@example.com"
RECRUITER_EMAIL = "recruiter@example.com"
# Minimum bcrypt cost keeps the suite fast; verify_password reads the cost from the hash.
PASSWORD_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture(autouse=True)
def no_azure(monkeypatch):
    """Tests never call Azure: extraction and scoring are off unless a test configures them
    (and fakes them)."""
    monkeypatch.setattr(settings, "azure_openai_endpoint", "")
    monkeypatch.setattr(settings, "azure_document_intelligence_endpoint", "")
    monkeypatch.setattr(settings, "azure_ai_project_endpoint", "")


class FakeEmailClient:
    def __init__(self):
        self.sent: list[dict] = []

    def begin_send(self, message):
        self.sent.append(message)
        return type("Poller", (), {"result": lambda self: {"status": "Succeeded"}})()


@pytest.fixture(autouse=True)
def outbox(monkeypatch) -> list[dict]:
    """Emails go here instead of Azure Communication Services: the ACS message dicts sent."""
    fake = FakeEmailClient()
    monkeypatch.setattr(settings, "acs_connection_string", "endpoint=https://example.communication.azure.com/;accesskey=x")
    monkeypatch.setattr(settings, "email_sender_address", "DoNotReply@example.azurecomm.net")
    monkeypatch.setattr(settings, "portal_url", "https://portal.example.com")
    monkeypatch.setattr(notifications, "_client", lambda: fake)
    return fake.sent


@pytest.fixture
def session_factory():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    with TestingSession() as db:
        db.add_all([
            User(email=ADMIN_EMAIL, full_name="Ada Admin", role=Role.ADMIN, password_hash=PASSWORD_HASH),
            User(email=ADMIN2_EMAIL, full_name="Abe Admin", role=Role.ADMIN, password_hash=PASSWORD_HASH),
            User(email=RECRUITER_EMAIL, full_name="Rae Recruiter", role=Role.RECRUITER, password_hash=PASSWORD_HASH),
        ])
        db.commit()

    app.dependency_overrides[get_db] = override_get_db
    yield TestingSession
    app.dependency_overrides.clear()


@pytest.fixture
def anon_client(session_factory):
    return TestClient(app)


def client_for(email: str) -> TestClient:
    token = TestClient(app).post("/auth/login", json={"email": email, "password": PASSWORD}).json()
    return TestClient(app, headers={"Authorization": f"Bearer {token['access_token']}"})


@pytest.fixture
def client(session_factory):
    """Logged in as an admin."""
    return client_for(ADMIN_EMAIL)


@pytest.fixture
def admin2_client(session_factory):
    """The second admin, who approves what `client` asks for."""
    return client_for(ADMIN2_EMAIL)


@pytest.fixture
def recruiter_client(session_factory):
    return client_for(RECRUITER_EMAIL)


def pending_id(response) -> str:
    """The approval request a job or resume response says is pending."""
    body = response.json()
    pending = body["approval"]["pending"] if "approval" in body else body["pending_deletion"]
    return pending["request_id"]


def approve(approver: TestClient, request_id: str) -> dict:
    resp = approver.post(f"/approvals/{request_id}/approve")
    assert resp.status_code == 200, resp.text
    return resp.json()


DEFAULT_JOB_TITLE = "Job for test uploads"


def default_job_id() -> str:
    """A published job that tests upload resumes for when the job doesn't matter, inserted
    directly (no approval round-trip) the first time it is needed."""
    sessions = app.dependency_overrides[get_db]()
    db = next(sessions)
    try:
        job = db.scalar(select(Job).where(Job.title == DEFAULT_JOB_TITLE))
        if job is None:
            job = Job(title=DEFAULT_JOB_TITLE, description="Any role.", is_published=True)
            db.add(job)
            db.commit()
        return str(job.job_id)
    finally:
        sessions.close()


def create_approved_job(client: TestClient, approver: TestClient, job: dict) -> dict:
    resp = client.post("/jobs", json=job)
    assert resp.status_code == 202, resp.text
    approve(approver, pending_id(resp))
    return approver.get(f"/jobs/{resp.json()['job_id']}").json()
