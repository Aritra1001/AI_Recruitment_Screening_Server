-- Sign in with Google. Apply after 008.
--
-- Users who sign up through Google have no password, and a user is matched to their Google
-- account by Google's permanent account id (`sub`) rather than by email, which can change.

ALTER TABLE users ALTER COLUMN password_hash DROP NOT NULL;
ALTER TABLE users ADD COLUMN IF NOT EXISTS google_sub VARCHAR(255) UNIQUE;
