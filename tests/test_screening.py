import logging

import pytest

from tests.conftest import (
    ADMIN2_EMAIL,
    MANAGER_EMAIL,
    RECRUITER_EMAIL,
    approve,
    client_for,
    create_approved_job,
    pending_id,
)
from tests.test_domains import create_domain, user_id
from tests.test_resume_scoring import JOB, extracted_resume, fake_agent  # noqa: F401 (fake_agent is a fixture)
from tests.test_resumes import upload

# The fake agent's scores weigh up to an overall score of 65.5.
FAKE_SCORE = 65.5


@pytest.fixture
def domain(client, session_factory) -> dict:
    """Software, managed by MANAGER_EMAIL."""
    domain = create_domain(client, "Software")
    client.put(f"/domains/{domain['domain_id']}/managers", json={"user_ids": [user_id(session_factory, MANAGER_EMAIL)]})
    return domain


def make_job(client, domain: dict | None, min_score: int = 60, title: str = JOB["title"]) -> str:
    job = {**JOB, "title": title, "shortlist_min_score": min_score}
    if domain:
        job["domain_id"] = domain["domain_id"]
    return create_approved_job(client, client_for(ADMIN2_EMAIL), job)["job_id"]


def scored_resume(recruiter_client, session_factory, job_id: str, name="jane_doe.pdf") -> str:
    resume_id = extracted_resume(recruiter_client, session_factory, job_id, name)
    resp = recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    assert resp.status_code == 202, resp.text
    return resume_id


def send(client, job_id, *resume_ids, note=None):
    return client.post(f"/jobs/{job_id}/screenings", json={"resume_ids": list(resume_ids), "note": note})


def test_a_resume_at_or_above_the_minimum_goes_to_the_domain_manager(
    fake_agent, domain, client, recruiter_client, manager_client, session_factory, outbox
):
    job_id = make_job(client, domain)
    resume_id = scored_resume(recruiter_client, session_factory, job_id)

    resp = send(recruiter_client, job_id, resume_id, note="Strong Python background.")
    assert resp.status_code == 201, resp.text
    [sent] = resp.json()
    assert sent["status"] == "pending"
    assert (sent["score_at_submission"], sent["min_score_at_submission"]) == (FAKE_SCORE, 60)
    assert sent["domain"]["name"] == "Software"
    assert sent["sender"]["email"] == RECRUITER_EMAIL
    assert sent["can_decide"] is False

    email = outbox[-1]
    assert email["recipients"]["to"][0]["address"] == MANAGER_EMAIL
    assert email["content"]["subject"] == f"To screen: a candidate for {JOB['title']}"
    assert "Strong Python background." in email["content"]["plainText"]
    assert f"/screening/{sent['screening_id']}" in email["content"]["plainText"]

    assert recruiter_client.get(f"/screenings?resume_id={resume_id}").json()[0]["screening_id"] == sent["screening_id"]
    [listed] = manager_client.get("/screenings?status=pending").json()
    assert listed["screening_id"] == sent["screening_id"]
    assert listed["can_decide"] is True
    assert listed["resume"]["candidate_name"] == "JANE DOE"


def test_below_the_minimum_nothing_is_sent(fake_agent, domain, client, recruiter_client, session_factory, outbox):
    job_id = make_job(client, domain, min_score=70)
    resume_id = scored_resume(recruiter_client, session_factory, job_id)
    sent_before = len(outbox)

    resp = send(recruiter_client, job_id, resume_id)
    assert resp.status_code == 409
    assert resp.json()["detail"] == f"Nothing was sent: JANE DOE scored {FAKE_SCORE:g}, below the minimum of 70."
    assert recruiter_client.get("/screenings").json() == []
    assert len(outbox) == sent_before


def test_all_or_nothing_with_every_problem_listed(fake_agent, domain, client, recruiter_client, session_factory):
    job_id = make_job(client, domain)
    good = scored_resume(recruiter_client, session_factory, job_id, "good.pdf")
    unscored = extracted_resume(recruiter_client, session_factory, job_id, "unscored.pdf")
    assert send(recruiter_client, job_id, good).status_code == 201

    resp = send(recruiter_client, job_id, good, unscored)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "Nothing was sent: JANE DOE was already sent; JANE DOE hasn't been scored."


def test_resumes_from_another_job_are_refused(fake_agent, domain, client, recruiter_client, session_factory):
    job_id = make_job(client, domain)
    other_job = make_job(client, domain, title="Other job")
    other = scored_resume(recruiter_client, session_factory, other_job)
    resp = send(recruiter_client, job_id, other)
    assert resp.status_code == 409
    assert "weren't uploaded for this job" in resp.json()["detail"]


def test_a_stale_score_must_be_refreshed_first(fake_agent, domain, client, admin2_client, recruiter_client, session_factory):
    job_id = make_job(client, domain)
    resume_id = scored_resume(recruiter_client, session_factory, job_id)
    approve(admin2_client, pending_id(client.patch(f"/jobs/{job_id}", json={"description": "A different role now."})))

    resp = send(recruiter_client, job_id, resume_id)
    assert resp.status_code == 409
    assert "changed since it was scored (re-score it first)" in resp.json()["detail"]
    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    assert send(recruiter_client, job_id, resume_id).status_code == 201


def test_a_job_needs_a_domain_with_a_manager(fake_agent, client, recruiter_client, session_factory):
    no_domain = make_job(client, None)
    resume_id = scored_resume(recruiter_client, session_factory, no_domain)
    resp = send(recruiter_client, no_domain, resume_id)
    assert resp.status_code == 409
    assert "has no domain yet" in resp.json()["detail"]

    unmanaged = create_domain(client, "Data Engineering")
    job_id = make_job(client, unmanaged, title="Data Engineer")
    resume_id = scored_resume(recruiter_client, session_factory, job_id, "de.pdf")
    resp = send(recruiter_client, job_id, resume_id)
    assert resp.status_code == 409
    assert resp.json()["detail"] == "The Data Engineering domain has no manager yet. An admin needs to assign one first."


def test_manager_invites_for_interview_and_the_recruiter_is_told(
    fake_agent, domain, client, recruiter_client, manager_client, session_factory, outbox
):
    job_id = make_job(client, domain)
    resume_id = scored_resume(recruiter_client, session_factory, job_id)
    screening_id = send(recruiter_client, job_id, resume_id).json()[0]["screening_id"]

    assert recruiter_client.post(f"/screenings/{screening_id}/decision", json={"decision": "interview"}).status_code == 403

    resp = manager_client.post(f"/screenings/{screening_id}/decision", json={"decision": "interview", "comment": "Book a call."})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["status"], body["decider"]["email"], body["decision_comment"]) == ("interview", MANAGER_EMAIL, "Book a call.")
    assert body["decided_at"] is not None and body["can_decide"] is False

    email = outbox[-1]
    assert email["recipients"]["to"][0]["address"] == RECRUITER_EMAIL
    assert email["content"]["subject"] == f"Interview: JANE DOE – {JOB['title']}"
    assert "Comment: Book a call." in email["content"]["plainText"]

    again = manager_client.post(f"/screenings/{screening_id}/decision", json={"decision": "rejected", "comment": "No."})
    assert again.status_code == 409
    assert recruiter_client.get(f"/screenings/{screening_id}").json()["status"] == "interview"


def test_rejecting_needs_a_comment(fake_agent, domain, client, recruiter_client, manager_client, session_factory):
    job_id = make_job(client, domain)
    resume_id = scored_resume(recruiter_client, session_factory, job_id)
    screening_id = send(recruiter_client, job_id, resume_id).json()[0]["screening_id"]

    assert manager_client.post(f"/screenings/{screening_id}/decision", json={"decision": "rejected"}).status_code == 422
    resp = manager_client.post(
        f"/screenings/{screening_id}/decision", json={"decision": "rejected", "comment": "Not enough FastAPI."}
    )
    assert resp.json()["status"] == "rejected"
    assert [s["screening_id"] for s in recruiter_client.get("/screenings?status=rejected").json()] == [screening_id]


def test_managers_only_see_their_own_domains(fake_agent, domain, client, recruiter_client, manager_client, session_factory):
    other_domain = create_domain(client, "Data Engineering")
    client.put(
        f"/domains/{other_domain['domain_id']}/managers", json={"user_ids": [user_id(session_factory, MANAGER_EMAIL)]}
    )
    job_id = make_job(client, other_domain, title="Data Engineer")
    resume_id = scored_resume(recruiter_client, session_factory, job_id)
    screening_id = send(recruiter_client, job_id, resume_id).json()[0]["screening_id"]
    assert len(manager_client.get("/screenings").json()) == 1

    # Moved off the domain: the manager no longer sees or decides it.
    client.put(f"/domains/{other_domain['domain_id']}/managers", json={"user_ids": []})
    assert manager_client.get("/screenings").json() == []
    assert manager_client.get(f"/screenings/{screening_id}").status_code == 404
    resp = manager_client.post(f"/screenings/{screening_id}/decision", json={"decision": "interview"})
    assert resp.status_code == 404
    # Admins and recruiters see every screening.
    assert len(client.get("/screenings").json()) == 1


def test_managers_read_but_do_not_upload_score_or_send(fake_agent, domain, client, recruiter_client, manager_client, session_factory):
    job_id = make_job(client, domain)
    resume_id = scored_resume(recruiter_client, session_factory, job_id)

    assert manager_client.get(f"/jobs/{job_id}").status_code == 200
    assert manager_client.get(f"/resumes/{resume_id}").status_code == 200
    assert manager_client.get(f"/jobs/{job_id}/scores/{resume_id}").status_code == 200
    assert upload(manager_client, "m.pdf", job_id=job_id).status_code == 403
    assert manager_client.post(f"/jobs/{job_id}/scores").status_code == 403
    assert manager_client.delete(f"/resumes/{resume_id}").status_code == 403
    assert send(manager_client, job_id, resume_id).status_code == 403


def test_screening_actions_are_logged(fake_agent, domain, client, recruiter_client, manager_client, session_factory, caplog):
    caplog.set_level(logging.INFO, logger="app")
    job_id = make_job(client, domain)
    resume_id = scored_resume(recruiter_client, session_factory, job_id)
    screening_id = send(recruiter_client, job_id, resume_id).json()[0]["screening_id"]
    manager_client.post(f"/screenings/{screening_id}/decision", json={"decision": "interview"})

    messages = [r.getMessage() for r in caplog.records if r.name == "app.screening"]
    assert messages[0].startswith(f"RESUMES_SENT_TO_MANAGER job_id={job_id} domain='Software' count=1")
    assert messages[0].endswith(f"managers={MANAGER_EMAIL} user={RECRUITER_EMAIL}")
    assert messages[1].startswith(f"SCREENING_DECIDED screening_id={screening_id}")
    assert "decision=interview" in messages[1]


def test_screening_requires_authentication(anon_client):
    assert anon_client.get("/screenings").status_code == 401
    assert anon_client.get("/domains").status_code == 401
