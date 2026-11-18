-- Scores from the Foundry resume-scorer agent: one row per resume per job. Scoring the
-- same resume against the same job again reuses the row when nothing changed (input_hash),
-- otherwise replaces it.
CREATE TABLE IF NOT EXISTS resume_scores (
    score_id UUID PRIMARY KEY,
    resume_id UUID NOT NULL REFERENCES resumes(resume_id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL DEFAULT 'scoring'
        CHECK (status IN ('scoring', 'scored', 'failed')),
    -- Weighted from the per-criterion scores by the backend (app/resume_scoring.py).
    overall_score NUMERIC(5, 2),
    recommendation VARCHAR(20)
        CHECK (recommendation IN ('strong_fit', 'good_fit', 'partial_fit', 'not_a_fit')),
    -- Per criterion: score, rationale, evidence (and matched/missing for the skill criteria).
    criteria JSONB,
    relevant_experience_years NUMERIC(4, 1),
    summary TEXT,
    strengths JSONB,
    gaps JSONB,
    -- The weights used for overall_score, so old scores stay explainable if they change.
    weights JSONB,
    agent_name VARCHAR(100),
    agent_version VARCHAR(20),
    -- The model deployment that answered, as reported by Foundry.
    model VARCHAR(100),
    -- SHA-256 of the agent name, agent version and the exact input sent. A request whose
    -- input hashes the same reuses this score instead of calling the agent again.
    input_hash CHAR(64),
    error TEXT,
    scored_by UUID REFERENCES users(user_id) ON DELETE SET NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    scored_at TIMESTAMP,
    UNIQUE (resume_id, job_id)
);

CREATE INDEX IF NOT EXISTS ix_resume_scores_job_rank ON resume_scores (job_id, overall_score DESC);
