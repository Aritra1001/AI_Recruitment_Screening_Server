import logging
import uuid

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.orm import undefer

from app.config import settings
from app.models import Resume
from tests.conftest import ADMIN_EMAIL, RECRUITER_EMAIL

DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def pdf_bytes(marker: str = "a") -> bytes:
    # Only the header is checked, so a marker is enough to make files distinct.
    return b"%PDF-1.7\n% resume " + marker.encode() + b"\n%%EOF\n"


def upload(client, name="jane_doe.pdf", content=None, content_type="application/pdf"):
    content = pdf_bytes(name) if content is None else content
    return client.post("/resumes", files={"file": (name, content, content_type)})


def stored_files(session_factory) -> dict[str, bytes]:
    """resume_id -> the file bytes held in resumes.content."""
    with session_factory() as db:
        resumes = db.scalars(select(Resume).options(undefer(Resume.content)))
        return {str(r.resume_id): r.content for r in resumes}


def test_upload_pdf_stores_file_and_metadata(recruiter_client, session_factory):
    content = pdf_bytes()
    resp = upload(recruiter_client, content=content)
    assert resp.status_code == 201
    body = resp.json()
    assert body["original_filename"] == "jane_doe.pdf"
    assert body["content_type"] == "application/pdf"
    assert body["size_bytes"] == len(content)
    assert body["status"] == "uploaded"
    assert body["uploader"]["email"] == RECRUITER_EMAIL
    assert stored_files(session_factory) == {body["resume_id"]: content}


def test_upload_docx(recruiter_client):
    resp = upload(recruiter_client, "cv.DOCX", b"PK\x03\x04 fake docx body", "application/octet-stream")
    assert resp.status_code == 201
    # The stored type comes from the extension, not what the browser claimed.
    assert resp.json()["content_type"] == DOCX_TYPE


def test_upload_strips_client_directories_from_name(recruiter_client):
    resp = upload(recruiter_client, "../../etc/resume.pdf")
    assert resp.json()["original_filename"] == "resume.pdf"


@pytest.mark.parametrize(
    ("name", "content", "code"),
    [
        ("notes.txt", b"hello", 415),
        ("resume.doc", b"\xd0\xcf\x11\xe0 old word", 415),
        ("fake.pdf", b"PK\x03\x04 actually a zip", 400),
        ("fake.docx", b"%PDF-1.7 actually a pdf", 400),
        ("empty.pdf", b"", 400),
    ],
)
def test_upload_rejects_bad_files(recruiter_client, session_factory, name, content, code):
    assert upload(recruiter_client, name, content).status_code == code
    assert stored_files(session_factory) == {}


def test_upload_rejects_files_over_the_limit(recruiter_client, session_factory, monkeypatch):
    monkeypatch.setattr(settings, "max_resume_mb", 1)
    resp = upload(recruiter_client, content=b"%PDF-" + b"0" * (1024 * 1024))
    assert resp.status_code == 413
    assert resp.json()["detail"] == "File is larger than 1 MB"
    assert stored_files(session_factory) == {}


def test_duplicate_upload_points_to_existing_resume(recruiter_client, client, session_factory):
    first = upload(recruiter_client, "jane.pdf", pdf_bytes("same")).json()
    resp = upload(client, "jane (1).pdf", pdf_bytes("same"))
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "message": 'This file was already uploaded as "jane.pdf".',
        "resume_id": first["resume_id"],
    }
    assert list(stored_files(session_factory)) == [first["resume_id"]]


def test_list_get_and_download(recruiter_client, client):
    a = upload(recruiter_client, "a.pdf").json()
    b = upload(client, "b.pdf").json()

    listed = [r["resume_id"] for r in recruiter_client.get("/resumes").json()]
    assert set(listed) == {a["resume_id"], b["resume_id"]}

    detail = recruiter_client.get(f"/resumes/{b['resume_id']}").json()
    assert detail["uploader"]["email"] == ADMIN_EMAIL
    assert detail["extracted"] is None and detail["extraction_error"] is None

    file = recruiter_client.get(f"/resumes/{a['resume_id']}/file")
    assert file.status_code == 200
    assert file.content == pdf_bytes("a.pdf")
    assert file.headers["content-type"] == "application/pdf"
    assert 'filename="a.pdf"' in file.headers["content-disposition"]
    assert file.headers["x-content-type-options"] == "nosniff"


def test_download_non_ascii_filename(recruiter_client):
    resume_id = upload(recruiter_client, "José CV.pdf").json()["resume_id"]
    file = recruiter_client.get(f"/resumes/{resume_id}/file")
    assert file.headers["content-disposition"] == "attachment; filename*=utf-8''Jos%C3%A9%20CV.pdf"


def test_list_and_detail_do_not_load_file_bytes(recruiter_client, session_factory):
    resume_id = uuid.UUID(upload(recruiter_client).json()["resume_id"])
    with session_factory() as db:
        resume = db.get(Resume, resume_id)
        assert "content" in inspect(resume).unloaded


def test_detail_returns_extracted_fields(recruiter_client, session_factory):
    resume_id = upload(recruiter_client).json()["resume_id"]
    with session_factory() as db:
        resume = db.get(Resume, uuid.UUID(resume_id))
        resume.status = "extracted"
        resume.extracted = {"name": "Jane Doe", "skills": ["Python"]}
        db.commit()
    body = recruiter_client.get(f"/resumes/{resume_id}").json()
    assert body["status"] == "extracted"
    assert body["extracted"]["name"] == "Jane Doe"
    assert body["extracted"]["skills"] == ["Python"]
    assert body["extracted"]["experience"] == []  # missing sections default to empty


def test_missing_resume_returns_404(recruiter_client):
    missing = "00000000-0000-0000-0000-000000000000"
    assert recruiter_client.get(f"/resumes/{missing}").status_code == 404
    assert recruiter_client.get(f"/resumes/{missing}/file").status_code == 404
    assert recruiter_client.delete(f"/resumes/{missing}").status_code == 404


def test_resumes_require_authentication(anon_client):
    assert anon_client.get("/resumes").status_code == 401
    assert upload(anon_client).status_code == 401


def test_uploader_can_delete_own_resume(recruiter_client, session_factory):
    resume_id = upload(recruiter_client).json()["resume_id"]
    assert recruiter_client.delete(f"/resumes/{resume_id}").status_code == 204
    assert recruiter_client.get(f"/resumes/{resume_id}").status_code == 404
    assert stored_files(session_factory) == {}


def test_recruiter_cannot_delete_someone_elses_resume(recruiter_client, client):
    resume_id = upload(client).json()["resume_id"]
    resp = recruiter_client.delete(f"/resumes/{resume_id}")
    assert resp.status_code == 403
    assert resp.json()["detail"] == "You can only delete resumes you uploaded"
    assert recruiter_client.get(f"/resumes/{resume_id}").status_code == 200


def test_admin_can_delete_any_resume(recruiter_client, client, session_factory):
    resume_id = upload(recruiter_client).json()["resume_id"]
    assert client.delete(f"/resumes/{resume_id}").status_code == 204
    assert stored_files(session_factory) == {}


def test_resume_actions_are_logged(recruiter_client, caplog):
    caplog.set_level(logging.INFO, logger="app")
    resume_id = upload(recruiter_client, "jane.pdf").json()["resume_id"]
    upload(recruiter_client, "notes.txt", b"x")
    recruiter_client.get("/resumes")
    recruiter_client.get(f"/resumes/{resume_id}")
    recruiter_client.get(f"/resumes/{resume_id}/file")
    recruiter_client.delete(f"/resumes/{resume_id}")

    messages = [r.getMessage() for r in caplog.records if r.name == "app.resumes"]
    assert messages[0] == f"RESUME_UPLOADED resume_id={resume_id} file='jane.pdf' size={len(pdf_bytes('jane.pdf'))} user={RECRUITER_EMAIL}"
    assert messages[1].startswith("RESUME_REJECTED file='notes.txt'")
    assert messages[2].startswith("RESUMES_LISTED count=1")
    assert messages[3] == f"RESUME_READ resume_id={resume_id} user={RECRUITER_EMAIL}"
    assert messages[4] == f"RESUME_FILE_DOWNLOADED resume_id={resume_id} user={RECRUITER_EMAIL}"
    assert messages[5] == f"RESUME_DELETED resume_id={resume_id} file='jane.pdf' user={RECRUITER_EMAIL}"
