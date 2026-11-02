-- Resume files are stored in the database instead of on disk (RESUME_STORAGE_DIR).
-- Apply after 003. The NOT NULL column can only be added while the table has no rows
-- uploaded with the old on-disk layout.
ALTER TABLE resumes ADD COLUMN content BYTEA NOT NULL;
ALTER TABLE resumes DROP COLUMN storage_name;

-- PDF and DOCX are already compressed; skip TOAST's compression attempt but still store
-- the bytes out of line, so queries that don't select `content` stay fast.
ALTER TABLE resumes ALTER COLUMN content SET STORAGE EXTERNAL;
