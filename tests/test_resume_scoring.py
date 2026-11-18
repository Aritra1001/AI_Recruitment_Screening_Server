import json
import uuid
from pathlib import Path

import pytest
from pydantic import ValidationError

from app import resume_scoring
from app.config import settings
from app.models import Job, Resume, ResumeScore, ResumeStatus
from app.resume_scoring import _AgentOutput, build_input, build_result, parse_reply, recommendation
from tests.conftest import ADMIN2_EMAIL, approve, client_for, create_approved_job, pending_id
from tests.test_resumes import upload

FIXTURES = Path(__file__).parent / "fixtures" / "scoring"

JOB = {
    "title": "Senior Python Developer",
    "description": "Senior Python Developer",
    "required_skills": ["Python", "FastAPI", "PostgreSQL", "REST APIs"],
    "preferred_skills": ["Azure", "Docker", "Kubernetes", "LangChain"],
    "minimum_experience": 5,
}


def criterion(score, **extra):
    return {"score": score, "rationale": "Because.", "evidence": ["Quoted line."], **extra}


def agent_output(**overrides) -> _AgentOutput:
    fields = {
        "criteria": {
            "required_skills": criterion(80, matched=["Python", "PostgreSQL", "REST APIs"], missing=["FastAPI"]),
            "preferred_skills": criterion(50, matched=["Docker", "Azure"], missing=["Kubernetes", "LangChain"]),
            "experience": criterion(60),
            "role_relevance": criterion(70),
            "education_certifications": criterion(60),
            "achievements": criterion(40),
        },
        "relevant_experience_years": 4.8,
        "summary": "A reasonable match.",
        "strengths": ["Python in production"],
        "gaps": ["No FastAPI"],
    }
    return _AgentOutput.model_validate({**fields, **overrides})


def job() -> Job:
    return Job(job_id=uuid.uuid4(), **JOB)


# --- Turning the agent's output into the stored result ------------------------------------


def test_overall_score_is_weighted():
    result = build_result(agent_output(), job())
    # 80*.35 + 60*.25 + 70*.15 + 50*.10 + 40*.10 + 60*.05
    assert result["overall_score"] == 65.5
    assert result["recommendation"] == "good_fit"
    assert result["weights"] == resume_scoring.WEIGHTS
    assert sum(resume_scoring.WEIGHTS.values()) == 100


@pytest.mark.parametrize(
    "overall, band",
    [(100, "strong_fit"), (80, "strong_fit"), (79.99, "good_fit"), (65, "good_fit"),
     (64.5, "partial_fit"), (50, "partial_fit"), (49.9, "not_a_fit"), (0, "not_a_fit")],
)
def test_recommendation_bands(overall, band):
    assert recommendation(overall) == band


def test_scores_are_clamped():
    output = agent_output()
    output.criteria.experience.score = 140
    output.criteria.achievements.score = -10
    criteria = build_result(output, job())["criteria"]
    assert criteria["experience"]["score"] == 100
    assert criteria["achievements"]["score"] == 0


def test_skills_are_reconciled_with_the_job():
    output = agent_output()
    # Different case/punctuation, a skill the job doesn't ask for, and a missing list that disagrees.
    output.criteria.required_skills.matched = ["python", "REST-APIs", "Flask"]
    output.criteria.required_skills.missing = ["Something else"]
    criteria = build_result(output, job())["criteria"]
    assert criteria["required_skills"]["matched"] == ["Python", "REST APIs"]
    assert criteria["required_skills"]["missing"] == ["FastAPI", "PostgreSQL"]
    assert criteria["preferred_skills"]["matched"] == ["Azure", "Docker"]  # job order
    assert criteria["preferred_skills"]["missing"] == ["Kubernetes", "LangChain"]


def test_relevant_years_are_rounded():
    assert build_result(agent_output(relevant_experience_years=8.333), job())["relevant_experience_years"] == 8.3
    assert build_result(agent_output(relevant_experience_years=None), job())["relevant_experience_years"] is None


def test_parse_reply_accepts_bare_or_fenced_json():
    text = agent_output().model_dump_json()
    assert parse_reply(text) == agent_output()
    assert parse_reply(f"```json\n{text}\n```") == agent_output()


def test_parse_reply_rejects_a_missing_field():
    data = agent_output().model_dump()
    del data["criteria"]["achievements"]
    with pytest.raises(ValidationError):
        parse_reply(json.dumps(data))


def test_input_leaves_out_the_name():
    resume = Resume(
        extracted={"name": "JANE DOE", "experience": [{"company": "ABC", "role": "Dev", "years": 3.1}],
                   "skills": ["Python"], "education": ["B.Tech"]},
        raw_text="JANE DOE\nDev at ABC",
    )
    payload = build_input(job(), resume)
    assert "name" not in payload["resume"]
    assert payload["job"]["minimum_experience_years"] == 5
    assert payload["resume"]["experience"][0]["years"] == 3.1
    assert payload["resume"]["raw_text"] == "JANE DOE\nDev at ABC"


@pytest.mark.parametrize("path", sorted(FIXTURES.glob("*.json")), ids=lambda p: p.stem)
def test_fixtures_have_the_shape_build_input_sends(path):
    """The playground test cases are what the backend sends, so they stay usable for evaluation."""
    case = json.loads(path.read_text(encoding="utf-8"))
    sent = build_input(job(), Resume(extracted={}, raw_text=""))
    assert case.keys() == sent.keys()
    assert case["job"].keys() == sent["job"].keys()
    assert case["resume"].keys() == sent["resume"].keys()


# --- API -----------------------------------------------------------------------------------


@pytest.fixture
def fake_agent(monkeypatch):
    """Turn scoring on, with the Foundry agent replaced by a fake that records its input."""
    monkeypatch.setattr(settings, "azure_ai_project_endpoint", "https://example.services.ai.azure.com/api/projects/p")
    monkeypatch.setattr(settings, "scoring_agent_version", "3")
    monkeypatch.setattr(settings, "scoring_max_concurrency", 1)
    calls = []

    def call_agent(payload):
        calls.append(payload)
        return agent_output(), "3", "gpt-5.5"

    monkeypatch.setattr(resume_scoring, "call_agent", call_agent)
    return calls


def create_job(client) -> str:
    return create_approved_job(client, client_for(ADMIN2_EMAIL), JOB)["job_id"]


def extracted_resume(client, session_factory, name="jane_doe.pdf", raw_text="JANE DOE\nPython developer") -> str:
    resume_id = upload(client, name=name).json()["resume_id"]
    with session_factory() as db:
        resume = db.get(Resume, uuid.UUID(resume_id))
        resume.status = ResumeStatus.EXTRACTED
        resume.extracted = {"name": "JANE DOE", "experience": [], "skills": ["Python"], "education": []}
        resume.raw_text = raw_text
        db.commit()
    return resume_id


def test_scoring_runs_in_the_background_and_is_stored(fake_agent, client, recruiter_client, session_factory):
    job_id = create_job(client)
    resume_id = extracted_resume(recruiter_client, session_factory)

    response = recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    assert response.status_code == 202
    assert response.json()[0]["status"] == "scoring"
    assert response.json()[0]["resume"]["candidate_name"] == "JANE DOE"

    # TestClient runs background tasks before returning, so scoring has finished.
    body = recruiter_client.get(f"/jobs/{job_id}/scores/{resume_id}").json()
    assert body["status"] == "scored"
    assert body["overall_score"] == 65.5
    assert body["recommendation"] == "good_fit"
    assert body["criteria"]["required_skills"]["missing"] == ["FastAPI"]
    assert body["criteria"]["experience"]["matched"] is None
    assert body["summary"] == "A reasonable match."
    assert (body["agent_name"], body["agent_version"], body["model"]) == ("resume-scorer", "3", "gpt-5.5")
    assert body["scored_at"] is not None
    assert body["error"] is None
    assert fake_agent[0]["job"]["title"] == "Senior Python Developer"
    assert fake_agent[0]["resume"]["raw_text"] == "JANE DOE\nPython developer"


def test_without_resume_ids_every_extracted_resume_is_scored(fake_agent, client, recruiter_client, session_factory):
    job_id = create_job(client)
    extracted_resume(recruiter_client, session_factory, "a.pdf")
    extracted_resume(recruiter_client, session_factory, "b.pdf")
    upload(recruiter_client, name="not_extracted.pdf")

    response = recruiter_client.post(f"/jobs/{job_id}/scores")
    assert response.status_code == 202
    assert sorted(s["resume"]["original_filename"] for s in response.json()) == ["a.pdf", "b.pdf"]
    assert len(fake_agent) == 2


def test_list_is_ranked_best_first(fake_agent, client, recruiter_client, session_factory, monkeypatch):
    job_id = create_job(client)
    weak = extracted_resume(recruiter_client, session_factory, "weak.pdf", raw_text="weak")
    strong = extracted_resume(recruiter_client, session_factory, "strong.pdf", raw_text="strong")

    def call_agent(payload):
        output = agent_output()
        for c in output.criteria.__dict__.values():
            c.score = 95 if payload["resume"]["raw_text"] == "strong" else 20
        return output, "3", "gpt-5.5"

    monkeypatch.setattr(resume_scoring, "call_agent", call_agent)

    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [weak, strong]})
    ranked = recruiter_client.get(f"/jobs/{job_id}/scores").json()
    assert [s["resume"]["resume_id"] for s in ranked] == [strong, weak]
    assert [s["recommendation"] for s in ranked] == ["strong_fit", "not_a_fit"]


def test_unchanged_input_reuses_the_stored_score(fake_agent, client, recruiter_client, session_factory):
    job_id = create_job(client)
    resume_id = extracted_resume(recruiter_client, session_factory)
    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    stored = recruiter_client.get(f"/jobs/{job_id}/scores/{resume_id}").json()

    response = recruiter_client.post(f"/jobs/{job_id}/scores")  # by id or "all", same result
    assert response.status_code == 202
    assert response.json() == [stored]
    assert len(fake_agent) == 1


def test_force_rescores_and_replaces_the_row(fake_agent, client, recruiter_client, session_factory):
    job_id = create_job(client)
    resume_id = extracted_resume(recruiter_client, session_factory)
    first = recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]}).json()[0]
    second = recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id], "force": True}).json()[0]
    assert second["status"] == "scoring"
    assert first["score_id"] == second["score_id"]
    assert len(recruiter_client.get(f"/jobs/{job_id}/scores").json()) == 1
    assert len(fake_agent) == 2


def test_editing_the_job_rescores(fake_agent, client, recruiter_client, session_factory):
    job_id = create_job(client)
    resume_id = extracted_resume(recruiter_client, session_factory)
    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    resp = client.patch(f"/jobs/{job_id}", json={"description": "Own the recruitment platform's backend."})
    approve(client_for(ADMIN2_EMAIL), pending_id(resp))

    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    assert len(fake_agent) == 2
    assert fake_agent[1]["job"]["description"] == "Own the recruitment platform's backend."


def test_re_extracting_the_resume_rescores(fake_agent, client, recruiter_client, session_factory):
    job_id = create_job(client)
    resume_id = extracted_resume(recruiter_client, session_factory)
    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    with session_factory() as db:
        db.get(Resume, uuid.UUID(resume_id)).raw_text = "JANE DOE\nPython developer, 6 years"
        db.commit()

    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    assert len(fake_agent) == 2


def test_a_new_agent_version_rescores(fake_agent, client, recruiter_client, session_factory, monkeypatch):
    job_id = create_job(client)
    resume_id = extracted_resume(recruiter_client, session_factory)
    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    monkeypatch.setattr(settings, "scoring_agent_version", "4")

    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    assert len(fake_agent) == 2


def test_input_hash_covers_agent_version_and_input(monkeypatch):
    payload = build_input(job(), Resume(extracted={"skills": ["Python"]}, raw_text="text"))
    monkeypatch.setattr(settings, "scoring_agent_version", "3")
    first = resume_scoring.input_hash(payload)
    assert resume_scoring.input_hash(json.loads(json.dumps(payload))) == first  # stable
    assert len(first) == 64

    changed = json.loads(json.dumps(payload))
    changed["resume"]["skills"].append("Docker")
    assert resume_scoring.input_hash(changed) != first
    monkeypatch.setattr(settings, "scoring_agent_version", "4")
    assert resume_scoring.input_hash(payload) != first


def test_a_resume_already_scoring_is_left_alone(fake_agent, client, recruiter_client, session_factory):
    job_id = create_job(client)
    resume_id = extracted_resume(recruiter_client, session_factory)
    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    with session_factory() as db:
        db.query(ResumeScore).one().status = "scoring"
        db.commit()

    response = recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    assert response.status_code == 202
    assert response.json()[0]["status"] == "scoring"
    assert len(fake_agent) == 1


def test_failed_scoring_is_recorded(fake_agent, client, recruiter_client, session_factory, monkeypatch, caplog):
    job_id = create_job(client)
    resume_id = extracted_resume(recruiter_client, session_factory)
    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})

    def broken(payload):
        raise resume_scoring.ScoringError("The agent's reply was cut off (max_output_tokens)")

    monkeypatch.setattr(resume_scoring, "call_agent", broken)
    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id], "force": True})
    body = recruiter_client.get(f"/jobs/{job_id}/scores/{resume_id}").json()
    assert body["status"] == "failed"
    assert body["error"] == "The agent's reply was cut off (max_output_tokens)"
    assert body["overall_score"] is None  # the earlier result isn't left behind
    assert "RESUME_SCORING_FAILED" in caplog.text

    # A failed score is never reused: the next request asks the agent again.
    monkeypatch.setattr(resume_scoring, "call_agent", lambda payload: (agent_output(), "3", "gpt-5.5"))
    recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    assert recruiter_client.get(f"/jobs/{job_id}/scores/{resume_id}").json()["status"] == "scored"


def test_unextracted_resume_is_rejected(fake_agent, client, recruiter_client):
    job_id = create_job(client)
    resume_id = upload(recruiter_client).json()["resume_id"]
    response = recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    assert response.status_code == 409
    assert "jane_doe.pdf" in response.json()["detail"]


def test_no_extracted_resumes_is_rejected(fake_agent, client, recruiter_client):
    job_id = create_job(client)
    assert recruiter_client.post(f"/jobs/{job_id}/scores").status_code == 409


def test_missing_job_or_resume_returns_404(fake_agent, client, recruiter_client, session_factory):
    resume_id = extracted_resume(recruiter_client, session_factory)
    assert recruiter_client.post(f"/jobs/{uuid.uuid4()}/scores", json={"resume_ids": [resume_id]}).status_code == 404
    job_id = create_job(client)
    response = recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [str(uuid.uuid4())]})
    assert response.status_code == 404
    assert recruiter_client.get(f"/jobs/{job_id}/scores/{resume_id}").status_code == 404
    assert recruiter_client.get(f"/jobs/{uuid.uuid4()}/scores").status_code == 404


def test_scoring_requires_configuration(client, recruiter_client, session_factory):
    job_id = create_job(client)
    resume_id = extracted_resume(recruiter_client, session_factory)
    response = recruiter_client.post(f"/jobs/{job_id}/scores", json={"resume_ids": [resume_id]})
    assert response.status_code == 503


def test_scores_require_authentication(anon_client):
    assert anon_client.get(f"/jobs/{uuid.uuid4()}/scores").status_code == 401
    assert anon_client.post(f"/jobs/{uuid.uuid4()}/scores").status_code == 401
