-- Creating, editing or deleting a job, and deleting a resume, now waits for a second admin
-- (app/approvals.py). Apply after 005.

-- A new job stays hidden from recruiters until its creation is approved. Jobs that already
-- exist were created before approvals, so they start out published.
ALTER TABLE jobs ADD COLUMN is_published BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE jobs ALTER COLUMN is_published SET DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS approval_requests (
    request_id UUID PRIMARY KEY,
    action VARCHAR(20) NOT NULL
        CHECK (action IN ('job_create', 'job_update', 'job_delete', 'resume_delete')),
    status VARCHAR(20) NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')),
    -- SET NULL so the request (the audit trail) outlives the job or resume it deleted.
    job_id UUID REFERENCES jobs(job_id) ON DELETE SET NULL,
    resume_id UUID REFERENCES resumes(resume_id) ON DELETE SET NULL,
    -- The job title or resume file name, for showing the request once the target is gone.
    target_name VARCHAR(255) NOT NULL,
    -- job_create: the new job's fields. job_update: {field: {"old", "new"}} for each change.
    -- job_delete / resume_delete: what will be deleted.
    details JSONB NOT NULL,
    requested_by UUID NOT NULL REFERENCES users(user_id),
    requested_at TIMESTAMP NOT NULL,
    expires_at TIMESTAMP NOT NULL,
    reviewed_by UUID REFERENCES users(user_id),
    reviewed_at TIMESTAMP,
    review_comment TEXT,
    -- Nobody approves their own request (also checked by the API).
    CHECK (reviewed_by IS NULL OR reviewed_by <> requested_by OR status = 'cancelled')
);

-- At most one pending request per job and per resume: further changes wait for it.
CREATE UNIQUE INDEX IF NOT EXISTS ux_approval_requests_pending_job
    ON approval_requests (job_id) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS ux_approval_requests_pending_resume
    ON approval_requests (resume_id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS ix_approval_requests_status
    ON approval_requests (status, requested_at DESC);
