import uuid
from datetime import date

import pytest

from app import resume_extraction
from app.config import settings
from app.models import Resume
from app.resume_extraction import _ModelOutput, _Position, _years, build_result
from tests.test_resumes import upload

TODAY = date(2026, 10, 30)

RESUME_TEXT = """\
JANE DOE
Backend Engineer | Pune, India
WORK EXPERIENCE
Software Engineer - ABC Technologies Pvt. Ltd. Sep 2023 - Present
• Built REST APIs with FastAPI and PostgreSQL; containerised services with Docker.
Intern - XYZ Labs Jun 2023 - Sep 2023
TECHNICAL SKILLS
Languages: Python, SQL
EDUCATION
B.Tech Computer Science
Some University
2019 - 2023
"""


def model_output(**overrides) -> _ModelOutput:
    fields = {
        "name": "JANE DOE",
        "experience": [
            _Position(company="ABC Technologies Pvt. Ltd.", role="Software Engineer", start_date="Sep 2023", end_date="Present"),
            _Position(company="XYZ Labs", role="Intern", start_date="Jun 2023", end_date="Sep 2023"),
        ],
        "skills": ["Python", "SQL", "FastAPI", "PostgreSQL", "Docker", "python"],
        "education": ["B.Tech Computer Science"],
    }
    return _ModelOutput(**{**fields, **overrides})


def test_build_result_matches_the_contract():
    result, dropped = build_result(model_output(), RESUME_TEXT, TODAY)
    assert dropped == []
    assert result.model_dump() == {
        "name": "JANE DOE",
        "experience": [
            {"company": "ABC Technologies Pvt. Ltd.", "role": "Software Engineer", "years": 3.1},
            {"company": "XYZ Labs", "role": "Intern", "years": 0.2},
        ],
        "skills": ["Python", "SQL", "FastAPI", "PostgreSQL", "Docker"],  # duplicate "python" removed
        "education": ["B.Tech Computer Science"],
    }


def test_values_not_in_the_resume_are_dropped():
    output = model_output(
        name="Jane Q. Doe",  # "q" is not in the text
        experience=[_Position(company="Google", role="Software Engineer", start_date="2015", end_date="Sep 2023")],
        skills=["Python", "Kubernetes", "Java"],  # "Java" must not match "JavaScript"-style substrings
        education=["Bachelor of Technology in Computer Science"],  # reworded
    )
    result, dropped = build_result(output, RESUME_TEXT, TODAY)
    assert result.name is None
    # The role is in the text, so the position is kept without the invented company or start date.
    assert result.experience[0].model_dump() == {"company": None, "role": "Software Engineer", "years": None}
    assert result.skills == ["Python"]
    assert result.education == []
    assert sorted(dropped) == sorted(
        ["name", "experience.company", "experience.start_date", "skills", "skills", "education"]
    )


def test_position_with_neither_company_nor_role_is_dropped():
    output = model_output(experience=[_Position(company="Acme", role="CTO", start_date=None, end_date=None)])
    result, _ = build_result(output, RESUME_TEXT, TODAY)
    assert result.experience == []


@pytest.mark.parametrize(
    "start, end, years",
    [
        ("Sep 2023", "Present", 3.1),
        ("September 2023", "Current", 3.1),
        ("2018", "2022", 4.0),
        ("06/2019", "2021-06", 2.0),
        ("Jan 2020", "Jun 2020", 0.4),
        (None, "2022", None),
        ("2020", None, None),
        ("Summer", "2022", None),
        ("2022", "2020", None),  # end before start
    ],
)
def test_years(start, end, years):
    assert _years(start, end, TODAY) == years


@pytest.fixture
def fake_azure(monkeypatch):
    """Turn extraction on, with Document Intelligence and the model replaced by fakes."""
    monkeypatch.setattr(settings, "azure_openai_endpoint", "https://example.openai.azure.com/")
    monkeypatch.setattr(settings, "azure_openai_api_key", "test-key")
    monkeypatch.setattr(settings, "azure_document_intelligence_endpoint", "https://example.cognitiveservices.azure.com/")
    monkeypatch.setattr(settings, "azure_document_intelligence_api_key", "test-key")
    calls = {"read": 0}

    def read_text(content: bytes) -> str:
        calls["read"] += 1
        return RESUME_TEXT

    monkeypatch.setattr(resume_extraction, "read_text", read_text)
    monkeypatch.setattr(resume_extraction, "structure", lambda text: model_output())
    return calls


def get_resume(session_factory, resume_id: str) -> Resume:
    with session_factory() as db:
        return db.get(Resume, uuid.UUID(resume_id))


def test_upload_extracts_in_the_background(fake_azure, recruiter_client, session_factory):
    response = upload(recruiter_client)
    assert response.status_code == 201
    assert response.json()["status"] == "extracting"

    # TestClient runs background tasks before returning, so the job has finished.
    body = recruiter_client.get(f"/resumes/{response.json()['resume_id']}").json()
    assert body["status"] == "extracted"
    assert body["extracted"]["name"] == "JANE DOE"
    assert body["extracted"]["experience"][0]["years"] is not None
    assert body["extraction_error"] is None
    assert body["extracted_at"] is not None
    assert get_resume(session_factory, body["resume_id"]).raw_text == RESUME_TEXT


def test_upload_without_azure_skips_extraction(recruiter_client):
    resume_id = upload(recruiter_client).json()["resume_id"]
    assert recruiter_client.get(f"/resumes/{resume_id}").json()["status"] == "uploaded"


def test_failed_extraction_is_recorded(fake_azure, recruiter_client, monkeypatch, caplog):
    def broken(text):
        raise RuntimeError("service unavailable")

    monkeypatch.setattr(resume_extraction, "structure", broken)
    resume_id = upload(recruiter_client).json()["resume_id"]
    body = recruiter_client.get(f"/resumes/{resume_id}").json()
    assert body["status"] == "failed"
    assert body["extraction_error"] == "RuntimeError: service unavailable"
    assert body["extracted"] is None
    assert "RESUME_EXTRACTION_FAILED" in caplog.text


def test_empty_document_fails(fake_azure, recruiter_client, monkeypatch):
    monkeypatch.setattr(resume_extraction, "read_text", lambda content: "  \n")
    resume_id = upload(recruiter_client).json()["resume_id"]
    body = recruiter_client.get(f"/resumes/{resume_id}").json()
    assert body["status"] == "failed"
    assert body["extraction_error"] == "No text could be read from this file"


def test_extract_endpoint_reruns_extraction(recruiter_client, session_factory, monkeypatch, fake_azure):
    # Uploaded while extraction was off...
    monkeypatch.setattr(settings, "azure_openai_endpoint", "")
    resume_id = upload(recruiter_client).json()["resume_id"]
    assert fake_azure["read"] == 0

    # ...then extracted on request.
    monkeypatch.setattr(settings, "azure_openai_endpoint", "https://example.openai.azure.com/")
    response = recruiter_client.post(f"/resumes/{resume_id}/extract")
    assert response.status_code == 202
    assert response.json()["status"] == "extracting"
    assert recruiter_client.get(f"/resumes/{resume_id}").json()["status"] == "extracted"
    assert fake_azure["read"] == 1


def test_extract_endpoint_rejects_a_resume_already_extracting(fake_azure, recruiter_client, session_factory):
    resume_id = upload(recruiter_client).json()["resume_id"]
    with session_factory() as db:
        db.get(Resume, uuid.UUID(resume_id)).status = "extracting"
        db.commit()
    assert recruiter_client.post(f"/resumes/{resume_id}/extract").status_code == 409


def test_extract_endpoint_requires_azure(recruiter_client):
    resume_id = upload(recruiter_client).json()["resume_id"]
    response = recruiter_client.post(f"/resumes/{resume_id}/extract")
    assert response.status_code == 503


def test_extract_missing_resume_returns_404(fake_azure, recruiter_client):
    assert recruiter_client.post(f"/resumes/{uuid.uuid4()}/extract").status_code == 404
