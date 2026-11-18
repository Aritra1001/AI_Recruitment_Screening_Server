import logging
import secrets

from app.job_parsing import normalize_skills, split_experience
from app.models import User
from tests.conftest import ADMIN_EMAIL, PASSWORD, RECRUITER_EMAIL, approve, create_approved_job, pending_id

SENIOR_PYTHON_JOB = {
    "title": "Senior Python Developer",
    "description": "We are looking for a Senior Python Developer...",
    "required_skills": ["Python", "FastAPI", "PostgreSQL", "REST APIs", "5+ years experience"],
    "preferred_skills": ["Azure", "Docker", "Kubernetes", "LangChain"],
}


def update(client, approver, job_id: str, changes: dict) -> dict:
    """PATCH the job, approve the change, and return the job as it is afterwards."""
    resp = client.patch(f"/jobs/{job_id}", json=changes)
    assert resp.status_code == 202, resp.text
    approve(approver, pending_id(resp))
    return client.get(f"/jobs/{job_id}").json()


def test_create_job_extracts_experience(client):
    resp = client.post("/jobs", json=SENIOR_PYTHON_JOB)
    assert resp.status_code == 202
    body = resp.json()
    assert body["title"] == "Senior Python Developer"
    assert body["required_skills"] == ["Python", "FastAPI", "PostgreSQL", "REST APIs"]
    assert body["preferred_skills"] == ["Azure", "Docker", "Kubernetes", "LangChain"]
    assert body["minimum_experience"] == 5
    assert body["job_id"] and body["created_at"]


def test_explicit_minimum_experience_wins(client):
    resp = client.post("/jobs", json={**SENIOR_PYTHON_JOB, "minimum_experience": 7})
    assert resp.json()["minimum_experience"] == 7


def test_get_and_list_jobs(client):
    job_id = client.post("/jobs", json=SENIOR_PYTHON_JOB).json()["job_id"]
    assert client.get(f"/jobs/{job_id}").json()["job_id"] == job_id
    assert [j["job_id"] for j in client.get("/jobs").json()] == [job_id]


def test_get_missing_job_returns_404(client):
    assert client.get("/jobs/00000000-0000-0000-0000-000000000000").status_code == 404


def test_validation_rejects_empty_title(client):
    assert client.post("/jobs", json={**SENIOR_PYTHON_JOB, "title": ""}).status_code == 422


def test_validation_rejects_whitespace_only_title(client):
    assert client.post("/jobs", json={**SENIOR_PYTHON_JOB, "title": "   "}).status_code == 422


def test_update_job_changes_only_sent_fields(client, admin2_client):
    job = create_approved_job(client, admin2_client, SENIOR_PYTHON_JOB)
    body = update(client, admin2_client, job["job_id"], {"title": "Lead Python Developer"})
    assert body["title"] == "Lead Python Developer"
    assert body["description"] == job["description"]
    assert body["required_skills"] == job["required_skills"]
    assert body["minimum_experience"] == 5


def test_update_job_reparses_required_skills(client, admin2_client):
    job_id = create_approved_job(client, admin2_client, SENIOR_PYTHON_JOB)["job_id"]
    body = update(client, admin2_client, job_id, {"required_skills": ["- Python", "Django", "8+ years experience"]})
    assert body["required_skills"] == ["Python", "Django"]
    assert body["minimum_experience"] == 8


def test_update_job_can_clear_minimum_experience(client, admin2_client):
    job_id = create_approved_job(client, admin2_client, SENIOR_PYTHON_JOB)["job_id"]
    assert update(client, admin2_client, job_id, {"minimum_experience": None})["minimum_experience"] is None


def test_update_job_rejects_null_title(client):
    job_id = client.post("/jobs", json=SENIOR_PYTHON_JOB).json()["job_id"]
    assert client.patch(f"/jobs/{job_id}", json={"title": None}).status_code == 422


def test_update_missing_job_returns_404(client):
    resp = client.patch("/jobs/00000000-0000-0000-0000-000000000000", json={"title": "X"})
    assert resp.status_code == 404


def test_delete_job(client, admin2_client):
    job_id = create_approved_job(client, admin2_client, SENIOR_PYTHON_JOB)["job_id"]
    resp = client.delete(f"/jobs/{job_id}")
    assert resp.status_code == 202
    assert client.get(f"/jobs/{job_id}").status_code == 200  # still there until approved
    approve(admin2_client, pending_id(resp))
    assert client.get(f"/jobs/{job_id}").status_code == 404
    assert client.delete(f"/jobs/{job_id}").status_code == 404


def test_parsing_helpers():
    assert normalize_skills(["- Python", "python", "  ", "• Docker"]) == ["Python", "Docker"]
    assert split_experience(["Python 3+ years", "7+ years of experience", "3 yrs"]) == (
        ["Python 3+ years"],
        7,
    )


# --- auth ---------------------------------------------------------------------------------


def test_login_returns_token_and_user(anon_client):
    resp = anon_client.post("/auth/login", json={"email": "ADMIN@example.com ", "password": PASSWORD})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] and body["token_type"] == "bearer"
    assert body["user"]["email"] == ADMIN_EMAIL and body["user"]["role"] == "admin"
    assert "password_hash" not in body["user"]


def test_login_rejects_bad_password_and_unknown_email(anon_client):
    for email, password in [(ADMIN_EMAIL, "wrong"), ("nobody@example.com", PASSWORD)]:
        resp = anon_client.post("/auth/login", json={"email": email, "password": password})
        assert resp.status_code == 401
        assert resp.json()["detail"] == "Invalid email or password"


def test_login_rejects_inactive_user(anon_client, session_factory):
    with session_factory() as db:
        db.query(User).filter_by(email=RECRUITER_EMAIL).update({"is_active": False})
        db.commit()
    resp = anon_client.post("/auth/login", json={"email": RECRUITER_EMAIL, "password": PASSWORD})
    assert resp.status_code == 401


def test_me_returns_current_user(recruiter_client):
    assert recruiter_client.get("/auth/me").json()["email"] == RECRUITER_EMAIL


def test_jobs_require_authentication(anon_client):
    assert anon_client.get("/jobs").status_code == 401
    assert anon_client.post("/jobs", json=SENIOR_PYTHON_JOB).status_code == 401
    resp = anon_client.get("/jobs", headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


# --- signup -------------------------------------------------------------------------------

NEW_USER = {"full_name": "Nina New", "email": "Nina@Example.com", "password": secrets.token_urlsafe(16)}


def test_signup_creates_recruiter_and_logs_in(anon_client):
    resp = anon_client.post("/auth/signup", json=NEW_USER)
    assert resp.status_code == 201
    body = resp.json()
    assert body["user"] == {**body["user"], "email": "nina@example.com", "full_name": "Nina New", "role": "recruiter"}
    me = anon_client.get("/auth/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.json()["email"] == "nina@example.com"
    login = anon_client.post("/auth/login", json={"email": "nina@example.com", "password": NEW_USER["password"]})
    assert login.status_code == 200


def test_signup_cannot_choose_admin_role(anon_client):
    resp = anon_client.post("/auth/signup", json={**NEW_USER, "role": "admin"})
    assert resp.json()["user"]["role"] == "recruiter"
    token = resp.json()["access_token"]
    created = anon_client.post("/jobs", json=SENIOR_PYTHON_JOB, headers={"Authorization": f"Bearer {token}"})
    assert created.status_code == 403


def test_signup_rejects_existing_email_case_insensitively(anon_client):
    resp = anon_client.post("/auth/signup", json={**NEW_USER, "email": ADMIN_EMAIL.upper()})
    assert resp.status_code == 409
    assert resp.json()["detail"] == "An account with this email already exists"


def test_signup_validation(anon_client):
    for bad in [
        {"email": "not-an-email"},
        {"email": "a b@example.com"},
        {"password": "short"},
        {"password": "\u00e9" * 40},  # 80 bytes: over bcrypt's limit
        {"full_name": "   "},
    ]:
        assert anon_client.post("/auth/signup", json={**NEW_USER, **bad}).status_code == 422, bad


def test_signup_is_logged(anon_client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    anon_client.post("/auth/signup", json=NEW_USER)
    anon_client.post("/auth/signup", json=NEW_USER)
    messages = _messages(caplog, "app.auth")
    assert "SIGNUP_SUCCESS user=nina@example.com role=recruiter" in messages
    assert "SIGNUP_REJECTED email=nina@example.com reason=email_taken" in messages


# --- RBAC ---------------------------------------------------------------------------------


def test_recruiter_can_read_jobs(client, admin2_client, recruiter_client):
    job_id = create_approved_job(client, admin2_client, SENIOR_PYTHON_JOB)["job_id"]
    assert recruiter_client.get("/jobs").status_code == 200
    assert recruiter_client.get(f"/jobs/{job_id}").json()["job_id"] == job_id


def test_recruiter_cannot_create_update_or_delete(client, recruiter_client):
    job_id = client.post("/jobs", json=SENIOR_PYTHON_JOB).json()["job_id"]
    assert recruiter_client.post("/jobs", json=SENIOR_PYTHON_JOB).status_code == 403
    assert recruiter_client.patch(f"/jobs/{job_id}", json={"title": "X"}).status_code == 403
    assert recruiter_client.delete(f"/jobs/{job_id}").status_code == 403
    job = client.get(f"/jobs/{job_id}").json()
    assert job["title"] == SENIOR_PYTHON_JOB["title"]


# --- logging ------------------------------------------------------------------------------


def _messages(caplog, logger_name: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger_name]


def test_crud_operations_are_logged_with_user(client, admin2_client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    resp = client.post("/jobs", json=SENIOR_PYTHON_JOB)
    job_id = resp.json()["job_id"]
    approve(admin2_client, pending_id(resp))
    client.get("/jobs")
    client.get(f"/jobs/{job_id}")
    approve(admin2_client, pending_id(client.patch(f"/jobs/{job_id}", json={"title": "Lead Python Developer"})))
    approve(admin2_client, pending_id(client.delete(f"/jobs/{job_id}")))

    (
        create_requested, created, listed, read, update_requested, updated, delete_requested, deleted,
    ) = _messages(caplog, "app.jobs")
    assert create_requested.startswith(f"JOB_CREATE_REQUESTED job_id={job_id}") and ADMIN_EMAIL in create_requested
    assert created.startswith(f"JOB_CREATED job_id={job_id}")
    assert f"user={ADMIN_EMAIL} approved_by=admin2@example.com" in created
    assert listed.startswith("JOBS_LISTED count=1")
    assert read.startswith(f"JOB_READ job_id={job_id}")
    assert update_requested.startswith(f"JOB_UPDATE_REQUESTED job_id={job_id}")
    assert updated.startswith(f"JOB_UPDATED job_id={job_id}")
    assert "'old': 'Senior Python Developer', 'new': 'Lead Python Developer'" in updated
    assert "description" not in updated  # unchanged fields are not logged
    assert delete_requested.startswith(f"JOB_DELETE_REQUESTED job_id={job_id}")
    assert deleted.startswith(f"JOB_DELETED job_id={job_id}") and "Lead Python Developer" in deleted


def test_update_without_changes_is_logged_as_such(client, caplog):
    job_id = client.post("/jobs", json=SENIOR_PYTHON_JOB).json()["job_id"]
    caplog.set_level(logging.INFO, logger="app")
    caplog.clear()
    client.patch(f"/jobs/{job_id}", json={"title": SENIOR_PYTHON_JOB["title"]})
    assert _messages(caplog, "app.jobs")[0].startswith("JOB_UPDATE_NO_CHANGES")


def test_denied_and_failed_actions_are_logged(anon_client, recruiter_client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    anon_client.post("/auth/login", json={"email": ADMIN_EMAIL, "password": "wrong"})
    recruiter_client.delete("/jobs/00000000-0000-0000-0000-000000000000")

    auth_messages = _messages(caplog, "app.auth")
    assert any(m.startswith(f"LOGIN_FAILED email={ADMIN_EMAIL}") for m in auth_messages)
    denied = [r for r in caplog.records if r.getMessage().startswith("ACCESS_DENIED")]
    assert denied and denied[0].levelno == logging.WARNING
    assert f"user={RECRUITER_EMAIL} role=recruiter action=DELETE" in denied[0].getMessage()
