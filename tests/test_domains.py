from app.models import User
from tests.conftest import ADMIN2_EMAIL, MANAGER_EMAIL, approve, client_for, create_approved_job, pending_id
from tests.test_jobs import SENIOR_PYTHON_JOB


def user_id(session_factory, email: str) -> str:
    with session_factory() as db:
        return str(next(u.user_id for u in db.query(User).all() if u.email == email))


def create_domain(client, name="Software") -> dict:
    resp = client.post("/domains", json={"name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_admins_manage_domains_and_everyone_lists_them(client, recruiter_client):
    software = create_domain(client, "Software")
    create_domain(client, "Data Engineering")
    assert client.post("/domains", json={"name": "software"}).status_code == 409  # names are case-insensitive
    assert recruiter_client.post("/domains", json={"name": "Sales"}).status_code == 403

    assert [d["name"] for d in recruiter_client.get("/domains").json()] == ["Data Engineering", "Software"]

    renamed = client.patch(f"/domains/{software['domain_id']}", json={"name": "Software Engineering"})
    assert renamed.json()["name"] == "Software Engineering"
    assert client.patch(f"/domains/{software['domain_id']}", json={"name": "Data Engineering"}).status_code == 409


def test_only_manager_accounts_can_be_assigned(client, session_factory):
    domain = create_domain(client)
    manager = user_id(session_factory, MANAGER_EMAIL)
    recruiter = user_id(session_factory, "recruiter@example.com")

    resp = client.put(f"/domains/{domain['domain_id']}/managers", json={"user_ids": [manager, recruiter]})
    assert resp.status_code == 422
    resp = client.put(f"/domains/{domain['domain_id']}/managers", json={"user_ids": [manager]})
    assert [m["email"] for m in resp.json()["managers"]] == [MANAGER_EMAIL]
    assert [m["email"] for m in client.get("/domains").json()[0]["managers"]] == [MANAGER_EMAIL]

    assert [u["email"] for u in client.get("/users?role=manager").json()] == [MANAGER_EMAIL]


def test_users_list_is_admin_only(recruiter_client, manager_client):
    assert recruiter_client.get("/users").status_code == 403
    assert manager_client.get("/users").status_code == 403


def test_a_domain_in_use_cannot_be_deleted(client):
    domain = create_domain(client)
    job = create_approved_job(client, client_for(ADMIN2_EMAIL), {**SENIOR_PYTHON_JOB, "domain_id": domain["domain_id"]})
    assert client.get("/domains").json()[0]["job_count"] == 1
    resp = client.delete(f"/domains/{domain['domain_id']}")
    assert resp.status_code == 409
    assert "1 job uses this domain" in resp.json()["detail"]

    unused = create_domain(client, "Sales")
    assert client.delete(f"/domains/{unused['domain_id']}").status_code == 204
    assert job["domain"]["name"] == "Software"


def test_jobs_carry_a_domain_and_minimum_score(client, admin2_client, outbox):
    domain = create_domain(client)
    resp = client.post("/jobs", json={**SENIOR_PYTHON_JOB, "domain_id": domain["domain_id"], "shortlist_min_score": 70})
    assert resp.status_code == 202
    body = resp.json()
    assert body["domain"] == {"domain_id": domain["domain_id"], "name": "Software"}
    assert body["shortlist_min_score"] == 70
    # The approval email names the domain, not its id.
    assert "Domain: Software" in outbox[-1]["content"]["plainText"]
    assert "Minimum score to shortlist: 70 / 100" in outbox[-1]["content"]["plainText"]

    defaults = client.post("/jobs", json={**SENIOR_PYTHON_JOB, "title": "Other"}).json()
    assert (defaults["domain"], defaults["shortlist_min_score"]) == (None, 60)

    unknown = client.post("/jobs", json={**SENIOR_PYTHON_JOB, "domain_id": "00000000-0000-0000-0000-000000000000"})
    assert unknown.status_code == 422
    assert client.post("/jobs", json={**SENIOR_PYTHON_JOB, "shortlist_min_score": 101}).status_code == 422


def test_changing_a_jobs_domain_needs_approval(client, admin2_client, outbox):
    software = create_domain(client, "Software")
    data = create_domain(client, "Data Engineering")
    job = create_approved_job(client, admin2_client, {**SENIOR_PYTHON_JOB, "domain_id": software["domain_id"]})

    resp = client.patch(f"/jobs/{job['job_id']}", json={"domain_id": data["domain_id"], "shortlist_min_score": 65})
    assert resp.status_code == 202
    assert resp.json()["domain"]["name"] == "Software"  # unchanged until approved
    text = outbox[-1]["content"]["plainText"]
    assert "Domain:\n  current:  Software\n  proposed: Data Engineering" in text

    approve(admin2_client, pending_id(resp))
    updated = client.get(f"/jobs/{job['job_id']}").json()
    assert (updated["domain"]["name"], updated["shortlist_min_score"]) == ("Data Engineering", 65)
    assert client.patch(f"/jobs/{job['job_id']}", json={"domain_id": None}).status_code == 422
