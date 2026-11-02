CREATE TABLE IF NOT EXISTS resumes (
    resume_id UUID PRIMARY KEY,
    original_filename VARCHAR(255) NOT NULL,
    content_type VARCHAR(100) NOT NULL,
    size_bytes INT NOT NULL,
    -- Used to reject the same file being uploaded twice.
    sha256 CHAR(64) NOT NULL UNIQUE,
    -- File name inside RESUME_STORAGE_DIR (never the user-supplied name).
    storage_name VARCHAR(255) NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'uploaded'
        CHECK (status IN ('uploaded', 'extracting', 'extracted', 'failed')),
    -- Filled in by the extraction step.
    raw_text TEXT,
    extracted JSONB,
    extraction_error TEXT,
    extracted_at TIMESTAMP,
    uploaded_by UUID NOT NULL REFERENCES users(user_id),
    uploaded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ix_resumes_uploaded_at ON resumes (uploaded_at DESC);
