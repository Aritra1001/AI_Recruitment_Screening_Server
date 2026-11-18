import secrets
import uuid
from datetime import timedelta

from app.models import ApprovalRequest, Job, User
from app.approvals import utcnow
from tests.conftest import ADMIN2_EMAIL, ADMIN_EMAIL, RECRUITER_EMAIL, approve, create_approved_job, pending_id
from tests.test_jobs import SENIOR_PYTHON_JOB
from tests.test_resumes import upload


def recipients(outbox) -> list[str]:
    return [m["recipients"]["to"][0]["address"] for m in outbox]


def expire(session_factory, request_id: str) -> None:
    with session_factory() as db:
        db.get(ApprovalRequest, uuid.UUID(request_id)).expires_at = utcnow() - timedelta(minutes=1)
        db.commit()


# --- new jobs -----------------------------------------------------------------------------


def test_new_job_is_hidden_from_recruiters_until_approved(client, admin2_client, recruiter_client, outbox):
    resp = client.post("/jobs", json=SENIOR_PYTHON_JOB)
    job_id, request_id = resp.json()["job_id"], pending_id(resp)
    assert resp.json()["approval"]["status"] == "pending"
    assert resp.json()["approval"]["pending"]["action"] == "job_create"

    assert recruiter_client.get("/jobs").json() == []
    assert recruiter_client.get(f"/jobs/{job_id}").status_code == 404
    assert recruiter_client.get(f"/jobs/{job_id}/scores").status_code == 404
    assert [j["job_id"] for j in client.get("/jobs").json()] == [job_id]  # admins see it

    # Only the other admin is asked, with a link to the request.
    assert recipients(outbox) == [ADMIN2_EMAIL]
    email = outbox[0]
    assert email["senderAddress"] == "DoNotReply@example.azurecomm.net"
    assert email["content"]["subject"] == "Approval needed: New job – Senior Python Developer"
    assert f"https://portal.example.com/approvals/{request_id}" in email["content"]["html"]
    assert "Ada Admin (admin@example.com) asked to create the job" in email["content"]["plainText"]

    approved = approve(admin2_client, request_id)
    assert approved["status"] == "approved" and approved["reviewer"]["email"] == ADMIN2_EMAIL
    assert recruiter_client.get(f"/jobs/{job_id}").json()["approval"] is None  # never shown to recruiters
    assert client.get(f"/jobs/{job_id}").json()["approval"] == {"status": "approved", "pending": None}
    # The requester hears back.
    assert recipients(outbox) == [ADMIN2_EMAIL, ADMIN_EMAIL]
    assert outbox[1]["content"]["subject"] == "Approved: New job – Senior Python Developer"


def test_rejected_new_job_is_deleted(client, admin2_client, outbox):
    resp = client.post("/jobs", json=SENIOR_PYTHON_JOB)
    rejected = admin2_client.post(f"/approvals/{pending_id(resp)}/reject", json={"comment": "Duplicate opening"})
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected" and rejected.json()["job_id"] is None
    assert client.get(f"/jobs/{resp.json()['job_id']}").status_code == 404
    assert outbox[-1]["content"]["subject"] == "Rejected: New job – Senior Python Developer"
    assert "Comment: Duplicate opening" in outbox[-1]["content"]["plainText"]


# --- edits and deletions ------------------------------------------------------------------


def test_edit_stays_invisible_until_approved(client, admin2_client, recruiter_client):
    job_id = create_approved_job(client, admin2_client, SENIOR_PYTHON_JOB)["job_id"]
    resp = client.patch(f"/jobs/{job_id}", json={"title": "Lead Python Developer"})
    assert resp.status_code == 202
    assert resp.json()["title"] == "Senior Python Developer"
    assert recruiter_client.get(f"/jobs/{job_id}").json()["title"] == "Senior Python Developer"
    assert client.get(f"/jobs/{job_id}").json()["approval"]["pending"]["action"] == "job_update"

    request = admin2_client.get(f"/approvals/{pending_id(resp)}").json()
    assert request["details"] == {"title": {"old": "Senior Python Developer", "new": "Lead Python Developer"}}
    assert request["can_review"] is True and request["can_cancel"] is False

    approve(admin2_client, request["request_id"])
    assert recruiter_client.get(f"/jobs/{job_id}").json()["title"] == "Lead Python Developer"


def test_edit_email_shows_current_and_proposed_values(client, admin2_client, outbox):
    job_id = create_approved_job(client, admin2_client, SENIOR_PYTHON_JOB)["job_id"]
    outbox.clear()
    client.patch(f"/jobs/{job_id}", json={"title": "<b>Lead</b> Python Developer"})
    text, html = outbox[0]["content"]["plainText"], outbox[0]["content"]["html"]
    assert "Title:\n  current:  Senior Python Developer\n  proposed: <b>Lead</b> Python Developer" in text
    assert "&lt;b&gt;Lead&lt;/b&gt; Python Developer" in html and "<b>Lead</b>" not in html


def test_second_change_is_blocked_while_one_is_pending(client, admin2_client):
    job_id = create_approved_job(client, admin2_client, SENIOR_PYTHON_JOB)["job_id"]
    client.patch(f"/jobs/{job_id}", json={"title": "Lead Python Developer"})
    for resp in (
        client.patch(f"/jobs/{job_id}", json={"title": "Staff Python Developer"}),
        admin2_client.patch(f"/jobs/{job_id}", json={"description": "Other"}),
        client.delete(f"/jobs/{job_id}"),
    ):
        assert resp.status_code == 409
        assert "already waiting for approval" in resp.json()["detail"]


def test_new_job_cannot_be_edited_before_it_is_approved(client):
    job_id = client.post("/jobs", json=SENIOR_PYTHON_JOB).json()["job_id"]
    assert client.patch(f"/jobs/{job_id}", json={"title": "X"}).status_code == 409


def test_delete_needs_approval(client, admin2_client, recruiter_client):
    job_id = create_approved_job(client, admin2_client, SENIOR_PYTHON_JOB)["job_id"]
    request_id = pending_id(client.delete(f"/jobs/{job_id}"))
    assert recruiter_client.get(f"/jobs/{job_id}").status_code == 200
    approved = approve(admin2_client, request_id)
    assert approved["job_id"] is None and approved["target_name"] == "Senior Python Developer"
    assert approved["details"]["title"] == "Senior Python Developer"  # what was deleted
    assert recruiter_client.get(f"/jobs/{job_id}").status_code == 404


# --- who can decide -----------------------------------------------------------------------


def test_nobody_approves_their_own_request(client):
    request_id = pending_id(client.post("/jobs", json=SENIOR_PYTHON_JOB))
    assert client.get(f"/approvals/{request_id}").json()["can_review"] is False
    resp = client.post(f"/approvals/{request_id}/approve")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "You can't approve your own request"
    assert client.post(f"/approvals/{request_id}/reject").status_code == 403


def test_recruiters_cannot_approve_or_list(client, recruiter_client):
    request_id = pending_id(client.post("/jobs", json=SENIOR_PYTHON_JOB))
    assert recruiter_client.post(f"/approvals/{request_id}/approve").status_code == 403
    assert recruiter_client.get("/approvals").status_code == 403
    assert recruiter_client.get(f"/approvals/{request_id}").status_code == 404


def test_a_request_is_decided_once(client, admin2_client):
    request_id = pending_id(client.post("/jobs", json=SENIOR_PYTHON_JOB))
    approve(admin2_client, request_id)
    for action in ("approve", "reject"):
        resp = admin2_client.post(f"/approvals/{request_id}/{action}")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "This request was already approved"


def test_change_is_refused_when_there_is_no_other_admin(client, session_factory):
    with session_factory() as db:
        db.query(User).filter_by(email=ADMIN2_EMAIL).update({"is_active": False})
        db.commit()
    resp = client.post("/jobs", json=SENIOR_PYTHON_JOB)
    assert resp.status_code == 409
    assert "no other admin" in resp.json()["detail"]
    with session_factory() as db:
        assert db.query(Job).count() == 0


# --- withdrawing and expiry ---------------------------------------------------------------


def test_requester_can_withdraw_and_try_again(client, admin2_client, outbox):
    job_id = create_approved_job(client, admin2_client, SENIOR_PYTHON_JOB)["job_id"]
    request_id = pending_id(client.patch(f"/jobs/{job_id}", json={"title": "Typo Developr"}))
    assert admin2_client.post(f"/approvals/{request_id}/cancel").status_code == 403
    sent = len(outbox)

    cancelled = client.post(f"/approvals/{request_id}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    assert len(outbox) == sent  # nobody needs telling
    assert client.get(f"/jobs/{job_id}").json()["title"] == "Senior Python Developer"
    assert client.patch(f"/jobs/{job_id}", json={"title": "Lead Python Developer"}).status_code == 202


def test_requests_expire(client, admin2_client, session_factory, outbox):
    job_id = create_approved_job(client, admin2_client, SENIOR_PYTHON_JOB)["job_id"]
    request_id = pending_id(client.patch(f"/jobs/{job_id}", json={"title": "Lead Python Developer"}))
    expire(session_factory, request_id)

    assert admin2_client.post(f"/approvals/{request_id}/approve").json()["detail"] == "This request has expired"
    assert admin2_client.get("/approvals").json() == []  # listing expires it
    assert admin2_client.get(f"/approvals/{request_id}").json()["status"] == "expired"
    assert outbox[-1]["content"]["subject"] == "Expired: Job changes – Senior Python Developer"
    assert recipients(outbox)[-1] == ADMIN_EMAIL
    assert client.get(f"/jobs/{job_id}").json()["title"] == "Senior Python Developer"
    # The job is free for another change.
    assert client.patch(f"/jobs/{job_id}", json={"title": "Lead Python Developer"}).status_code == 202


def test_expired_new_job_is_deleted(client, admin2_client, session_factory):
    resp = client.post("/jobs", json=SENIOR_PYTHON_JOB)
    expire(session_factory, pending_id(resp))
    assert admin2_client.get("/approvals", params={"status": "expired"}).json()[0]["job_id"] is None
    assert client.get(f"/jobs/{resp.json()['job_id']}").status_code == 404


def test_list_filters_by_status(client, admin2_client):
    first = pending_id(client.post("/jobs", json=SENIOR_PYTHON_JOB))
    second = pending_id(client.post("/jobs", json={**SENIOR_PYTHON_JOB, "title": "Data Engineer"}))
    approve(admin2_client, first)
    assert [r["request_id"] for r in admin2_client.get("/approvals").json()] == [second]
    assert [r["request_id"] for r in admin2_client.get("/approvals?status=approved").json()] == [first]
    assert len(admin2_client.get("/approvals?status=all").json()) == 2


# --- resume deletion ----------------------------------------------------------------------


def test_recruiter_resume_deletion_needs_an_admin(recruiter_client, client, admin2_client, anon_client, outbox):
    resume_id = upload(recruiter_client).json()["resume_id"]
    resp = recruiter_client.delete(f"/resumes/{resume_id}")
    request_id = pending_id(resp)
    # Every admin is asked, since the requester isn't one.
    assert sorted(recipients(outbox)) == [ADMIN2_EMAIL, ADMIN_EMAIL]
    assert "asked to delete the resume" in outbox[0]["content"]["plainText"]

    # The requester and admins see the pending deletion; other recruiters don't.
    assert recruiter_client.get(f"/resumes/{resume_id}").json()["pending_deletion"]["request_id"] == request_id
    assert client.get("/resumes").json()[0]["pending_deletion"]["request_id"] == request_id
    other = anon_client.post(
        "/auth/signup", json={"full_name": "Otto Other", "email": "otto@example.com", "password": secrets.token_urlsafe(16)}
    ).json()["access_token"]
    other_view = anon_client.get(f"/resumes/{resume_id}", headers={"Authorization": f"Bearer {other}"}).json()
    assert other_view["pending_deletion"] is None

    # The requester can follow their own request, and asking twice is refused.
    assert recruiter_client.get(f"/approvals/{request_id}").json()["can_cancel"] is True
    assert recruiter_client.delete(f"/resumes/{resume_id}").status_code == 409

    approve(client, request_id)
    assert recruiter_client.get(f"/resumes/{resume_id}").status_code == 404
    assert recipients(outbox)[-1] == RECRUITER_EMAIL


def test_emails_are_skipped_when_not_configured(client, monkeypatch, outbox, caplog):
    from app.config import settings

    monkeypatch.setattr(settings, "acs_connection_string", "")
    caplog.set_level("INFO", logger="app.notifications")
    assert client.post("/jobs", json=SENIOR_PYTHON_JOB).status_code == 202
    assert outbox == []
    assert any(r.getMessage().startswith(f"EMAIL_SKIPPED reason=not_configured to={ADMIN2_EMAIL}") for r in caplog.records)
