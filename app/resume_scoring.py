"""Resume scoring: the Foundry resume-scorer agent scores one resume against one job.

The agent judges each criterion (score, rationale, evidence). Everything that has to be
consistent from one resume to the next is done here, never by the model:
1. The weighted overall score and the recommendation band (WEIGHTS, _BANDS).
2. Criterion scores are clamped to 0-100.
3. matched/missing skills are reconciled with the job's own skill lists, so a renamed or
   invented skill can't appear and every job skill is in exactly one of the two.
The agent's response format is enforced in Foundry (strict JSON schema) and checked again
here (_AgentOutput); a reply that doesn't match is retried once.

The agent isn't fully deterministic, so a resume isn't sent to it twice for the same input:
each score keeps a hash of the agent, its version and the exact input (input_hash), and a
request with the same hash reuses the stored score unless a re-score is forced.
"""

import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from functools import cache
from typing import Any
from uuid import UUID

from azure.ai.projects import AIProjectClient
from azure.identity import DefaultAzureCredential
from openai import OpenAI
from pydantic import BaseModel, ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import StaleDataError

from app.config import settings
from app.models import Job, Resume, ResumeScore, ScoreStatus

logger = logging.getLogger("app.scores")

_AGENT_TIMEOUT_SECONDS = 180
_ATTEMPTS = 2

# Percent of the overall score each criterion carries.
WEIGHTS = {
    "required_skills": 35,
    "experience": 25,
    "role_relevance": 15,
    "preferred_skills": 10,
    "achievements": 10,
    "education_certifications": 5,
}
# (lowest overall score, recommendation), best band first.
_BANDS = [(80, "strong_fit"), (65, "good_fit"), (50, "partial_fit"), (0, "not_a_fit")]

# Cleared when a resume is (re-)scored, so a failed re-score doesn't show the old result.
_RESULT_FIELDS = (
    "overall_score", "recommendation", "criteria", "relevant_experience_years", "summary",
    "strengths", "gaps", "weights", "agent_name", "agent_version", "model", "input_hash", "error",
    "scored_at",
)


class _Criterion(BaseModel):
    score: int
    rationale: str
    evidence: list[str]


class _SkillCriterion(_Criterion):
    matched: list[str]
    missing: list[str]


class _Criteria(BaseModel):
    required_skills: _SkillCriterion
    preferred_skills: _SkillCriterion
    experience: _Criterion
    role_relevance: _Criterion
    education_certifications: _Criterion
    achievements: _Criterion


class _AgentOutput(BaseModel):
    """The response format set on the agent in Foundry (JSON schema, strict)."""

    criteria: _Criteria
    relevant_experience_years: float | None
    summary: str
    strengths: list[str]
    gaps: list[str]


class ScoringError(Exception):
    pass


def is_configured() -> bool:
    return bool(settings.azure_ai_project_endpoint and settings.scoring_agent_name)


def build_input(job: Job, resume: Resume) -> dict[str, Any]:
    """The message sent to the agent. The extracted name is left out; the agent doesn't need it."""
    extracted = resume.extracted or {}
    return {
        "job": {
            "title": job.title,
            "description": job.description,
            "required_skills": job.required_skills or [],
            "preferred_skills": job.preferred_skills or [],
            "minimum_experience_years": job.minimum_experience,
        },
        "resume": {
            "experience": extracted.get("experience", []),
            "skills": extracted.get("skills", []),
            "education": extracted.get("education", []),
            "raw_text": resume.raw_text or "",
        },
    }


def input_hash(payload: dict[str, Any]) -> str:
    """SHA-256 of everything that decides the agent's answer: its name, version and the input.
    With no pinned version this is "latest", so a new agent version isn't noticed."""
    key = {
        "agent": settings.scoring_agent_name,
        "version": settings.scoring_agent_version or "latest",
        "input": payload,
    }
    return hashlib.sha256(json.dumps(key, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def is_current(score: ResumeScore, job: Job, resume: Resume) -> bool:
    """Whether this score came from the agent seeing exactly what it would see now."""
    return score.status == ScoreStatus.SCORED and score.input_hash == input_hash(build_input(job, resume))


@cache
def _client() -> OpenAI:
    project = AIProjectClient(endpoint=settings.azure_ai_project_endpoint, credential=DefaultAzureCredential())
    return project.get_openai_client(timeout=_AGENT_TIMEOUT_SECONDS)


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$")


def parse_reply(text: str) -> _AgentOutput:
    # Strict mode returns bare JSON; the fence strip only matters if the schema is ever removed.
    return _AgentOutput.model_validate_json(_FENCE.sub("", text))


def _agent_version(response: Any) -> str | None:
    """The agent version that answered, when Foundry reports it; else the pinned one."""
    reference = getattr(response, "agent_reference", None) or getattr(response, "agent", None)
    if isinstance(reference, dict) and reference.get("version"):
        return str(reference["version"])
    return settings.scoring_agent_version or None


def call_agent(payload: dict[str, Any]) -> tuple[_AgentOutput, str | None, str | None]:
    """(output, agent version, model) for one resume/job pair."""
    agent = {"type": "agent_reference", "name": settings.scoring_agent_name}
    if settings.scoring_agent_version:
        agent["version"] = settings.scoring_agent_version
    message = json.dumps(payload, ensure_ascii=False)

    error: ValidationError | None = None
    for attempt in range(1, _ATTEMPTS + 1):
        response = _client().responses.create(input=message, extra_body={"agent_reference": agent})
        if response.status == "incomplete":
            reason = response.incomplete_details.reason if response.incomplete_details else "unknown"
            raise ScoringError(f"The agent's reply was cut off ({reason})")
        try:
            return parse_reply(response.output_text), _agent_version(response), response.model
        except ValidationError as exc:
            error = exc
            logger.warning("RESUME_SCORING_BAD_REPLY attempt=%d errors=%d", attempt, exc.error_count())
    raise ScoringError(f"The agent's reply did not match the expected format: {error.errors()[0]['msg']}")


# --- Turning the agent's judgement into the stored result ---------------------------------


def _skill_key(skill: str) -> str:
    return re.sub(r"\W", "", skill.casefold())


def _reconcile(matched: list[str], job_skills: list[str]) -> tuple[list[str], list[str]]:
    """(matched, missing), both in the job's spelling and order. A skill the agent matched
    that isn't one of the job's is dropped; every job skill not matched is missing."""
    keys = {_skill_key(skill) for skill in matched}
    hits = [skill for skill in job_skills if _skill_key(skill) in keys]
    return hits, [skill for skill in job_skills if skill not in hits]


def recommendation(overall: float) -> str:
    return next(band for floor, band in _BANDS if overall >= floor)


def build_result(output: _AgentOutput, job: Job) -> dict[str, Any]:
    """The resume_scores fields for this output: clamped scores, reconciled skills, the
    weighted overall score and its recommendation."""
    criteria = output.criteria.model_dump()
    for criterion in criteria.values():
        criterion["score"] = min(max(criterion["score"], 0), 100)
    for name, job_skills in (("required_skills", job.required_skills), ("preferred_skills", job.preferred_skills)):
        criteria[name]["matched"], criteria[name]["missing"] = _reconcile(criteria[name]["matched"], job_skills or [])

    overall = round(sum(criteria[name]["score"] * weight for name, weight in WEIGHTS.items()) / sum(WEIGHTS.values()), 2)
    years = output.relevant_experience_years
    return {
        "overall_score": overall,
        "recommendation": recommendation(overall),
        "criteria": criteria,
        "relevant_experience_years": round(max(years, 0), 1) if years is not None else None,
        "summary": output.summary,
        "strengths": output.strengths,
        "gaps": output.gaps,
        "weights": WEIGHTS,
    }


# --- Running it ---------------------------------------------------------------------------


def start(score: ResumeScore, user_id: UUID) -> None:
    """Mark a (new or existing) score row as being scored, clearing any previous result."""
    for field in _RESULT_FIELDS:
        setattr(score, field, None)
    score.status = ScoreStatus.SCORING
    score.scored_by = user_id


def _error_message(exc: Exception) -> str:
    if isinstance(exc, ScoringError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"[:500]


def score_resume(job_id: UUID, resume_id: UUID, bind: Engine) -> None:
    """Score one resume. The caller has already set status = scoring (start()); this sets
    scored or failed."""
    with Session(bind=bind, expire_on_commit=False) as db:
        score = db.scalar(
            select(ResumeScore).where(ResumeScore.job_id == job_id, ResumeScore.resume_id == resume_id)
        )
        if score is None:  # job or resume deleted before the job started
            return
        job = db.get(Job, job_id)
        # Built from the rows as they are now, so the hash matches what the agent actually saw.
        payload = build_input(job, score.resume)
        started = time.monotonic()
        try:
            output, version, model = call_agent(payload)
            result = build_result(output, job)
        except Exception as exc:
            logger.exception("RESUME_SCORING_FAILED job_id=%s resume_id=%s", job_id, resume_id)
            score.status = ScoreStatus.FAILED
            score.error = _error_message(exc)
        else:
            for field, value in result.items():
                setattr(score, field, value)
            score.status = ScoreStatus.SCORED
            score.agent_name = settings.scoring_agent_name
            score.agent_version = version
            score.model = model
            score.input_hash = input_hash(payload)
            score.error = None
            score.scored_at = func.current_timestamp()
            logger.info(
                "RESUME_SCORED job_id=%s resume_id=%s overall=%.1f recommendation=%s seconds=%.1f",
                job_id, resume_id, result["overall_score"], result["recommendation"], time.monotonic() - started,
            )
        try:
            db.commit()
        except StaleDataError:  # job or resume deleted while the agent was running
            logger.info("RESUME_SCORING_DISCARDED job_id=%s resume_id=%s reason=deleted", job_id, resume_id)


def score_resumes(job_id: UUID, resume_ids: list[UUID], bind: Engine) -> None:
    """Background job for POST /jobs/{id}/scores: scores up to SCORING_MAX_CONCURRENCY at once."""
    workers = min(settings.scoring_max_concurrency, len(resume_ids))
    if workers <= 1:
        for resume_id in resume_ids:
            score_resume(job_id, resume_id, bind)
        return
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in [pool.submit(score_resume, job_id, resume_id, bind) for resume_id in resume_ids]:
            if exc := future.exception():  # score_resume records agent failures itself; this is e.g. the DB
                logger.error("RESUME_SCORING_CRASHED job_id=%s error=%s", job_id, _error_message(exc))
