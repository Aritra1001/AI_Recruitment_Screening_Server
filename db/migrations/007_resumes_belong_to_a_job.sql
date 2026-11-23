-- Each resume is uploaded for one job and is only scored against that job. Apply after 006.
--
-- Resumes uploaded before this keep job_id = NULL: they can't be scored any more and no
-- longer appear in any job's candidate list (their old scores stay in resume_scores but are
-- hidden). Re-upload the file for the right job, then delete the old copy.
ALTER TABLE resumes
    ADD COLUMN IF NOT EXISTS job_id UUID REFERENCES jobs(job_id) ON DELETE CASCADE;

CREATE INDEX IF NOT EXISTS ix_resumes_job_id ON resumes (job_id);

-- A file can now be uploaded once per job instead of once overall, so a candidate who
-- applies to two jobs can be uploaded for both.
ALTER TABLE resumes DROP CONSTRAINT IF EXISTS resumes_sha256_key;
ALTER TABLE resumes ADD CONSTRAINT uq_resumes_job_id_sha256 UNIQUE (job_id, sha256);
