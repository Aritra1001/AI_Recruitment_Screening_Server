import os
import secrets
import tempfile

# Must be set before app.config is imported so test runs don't write to ./logs.
os.environ.setdefault("LOG_DIR", tempfile.mkdtemp(prefix="recruitment-test-logs-"))

import bcrypt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import Base, get_db
from app.main import app
from app.models import Role, User

# Generated per run so no credential-like literal lives in the repo (secret scanners flag them).
PASSWORD = secrets.token_urlsafe(16)
ADMIN_EMAIL = "admin@example.com"
RECRUITER_EMAIL = "recruiter@example.com"
# Minimum bcrypt cost keeps the suite fast; verify_password reads the cost from the hash.
PASSWORD_HASH = bcrypt.hashpw(PASSWORD.encode(), bcrypt.gensalt(rounds=4)).decode()


@pytest.fixture(autouse=True)
def no_azure(monkeypatch):
    """Tests never call Azure: extraction is off unless a test configures it (and fakes it)."""
    monkeypatch.setattr(settings, "azure_openai_endpoint", "")
    monkeypatch.setattr(settings, "azure_document_intelligence_endpoint", "")


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
            User(email=RECRUITER_EMAIL, full_name="Rae Recruiter", role=Role.RECRUITER, password_hash=PASSWORD_HASH),
        ])
        db.commit()

    app.dependency_overrides[get_db] = override_get_db
    yield TestingSession
    app.dependency_overrides.clear()


@pytest.fixture
def anon_client(session_factory):
    return TestClient(app)


def _client_for(email: str) -> TestClient:
    token = TestClient(app).post("/auth/login", json={"email": email, "password": PASSWORD}).json()
    return TestClient(app, headers={"Authorization": f"Bearer {token['access_token']}"})


@pytest.fixture
def client(session_factory):
    """Logged in as an admin."""
    return _client_for(ADMIN_EMAIL)


@pytest.fixture
def recruiter_client(session_factory):
    return _client_for(RECRUITER_EMAIL)
