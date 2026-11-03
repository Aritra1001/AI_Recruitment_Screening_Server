CREATE TABLE IF NOT EXISTS jobs (
    job_id UUID PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    description TEXT NOT NULL,
    required_skills JSONB,
    preferred_skills JSONB,
    minimum_experience INT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
