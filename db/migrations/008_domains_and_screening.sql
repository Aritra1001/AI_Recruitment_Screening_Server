-- Domains, domain managers and resume screening. Apply after 007.
--
-- Each job belongs to a domain (Software, Data Engineering, ...). A recruiter sends a resume
-- whose score meets the job's minimum to the domain's managers, who decide whether the
-- candidate gets an interview.

-- New role: domain manager.
ALTER TABLE users DROP CONSTRAINT IF EXISTS users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN ('admin', 'recruiter', 'manager'));

CREATE TABLE IF NOT EXISTS domains (
    domain_id UUID PRIMARY KEY,
    name VARCHAR(100) NOT NULL UNIQUE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- A domain can have several managers and a manager can cover several domains.
CREATE TABLE IF NOT EXISTS domain_managers (
    domain_id UUID NOT NULL REFERENCES domains(domain_id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    PRIMARY KEY (domain_id, user_id)
);

-- Existing jobs get no domain (an admin sets one by editing the job); a domain that jobs
-- still use can't be deleted.
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS domain_id UUID REFERENCES domains(domain_id) ON DELETE RESTRICT;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS shortlist_min_score INT NOT NULL DEFAULT 60
    CHECK (shortlist_min_score BETWEEN 0 AND 100);
CREATE INDEX IF NOT EXISTS ix_jobs_domain_id ON jobs (domain_id);

-- One per resume: sent once, decided once.
CREATE TABLE IF NOT EXISTS screenings (
    screening_id UUID PRIMARY KEY,
    resume_id UUID NOT NULL UNIQUE REFERENCES resumes(resume_id) ON DELETE CASCADE,
    job_id UUID NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'interview', 'rejected')),
    -- The overall score and the job's minimum when it was sent.
    score_at_submission NUMERIC(5, 2) NOT NULL,
    min_score_at_submission INT NOT NULL,
    note TEXT,
    sent_by UUID NOT NULL REFERENCES users(user_id),
    sent_at TIMESTAMP NOT NULL,
    decided_by UUID REFERENCES users(user_id),
    decided_at TIMESTAMP,
    decision_comment TEXT,
    CHECK ((status = 'pending') = (decided_at IS NULL))
);

CREATE INDEX IF NOT EXISTS ix_screenings_job_id ON screenings (job_id);
CREATE INDEX IF NOT EXISTS ix_screenings_status ON screenings (status, sent_at DESC);
