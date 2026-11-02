# AI Recruitment Screening

FastAPI backend (`app/`) + React portal (`frontend/`) for managing job openings and
candidate resumes.

## Roles

| Role        | Jobs                         | Resumes                                     |
|-------------|------------------------------|---------------------------------------------|
| `admin`     | create, read, update, delete | upload, view, download, delete any          |
| `recruiter` | read only                    | upload, view, download, delete own uploads  |

The API enforces this (`403` otherwise); the portal hides the actions and routes a role
cannot use.

## Resumes

- PDF or DOCX, up to `MAX_RESUME_MB` (default 10) each. The file's first bytes must match
  its extension; the same file can't be uploaded twice (checked by SHA-256).
- The file itself is stored in the database (`resumes.content`, `BYTEA`), next to its
  metadata, so a database backup covers everything and deleting the row deletes the file.
  The column is loaded only by the download endpoint (`GET /resumes/{id}/file`); listing and
  viewing resumes never read it.
- Extraction isn't built yet: every resume stays `uploaded` ("Not extracted"). The fields
  the extraction step must fill are defined by `ExtractedResume` in `app/schemas.py` and
  stored in `resumes.extracted`; the detail page renders them once `status = 'extracted'`.

## Accounts

- **Sign up** (`POST /auth/signup`, or "Create an account" in the portal): anyone can register.
  New accounts are always `recruiter`; the request has no role field.
- **Promote to admin**: `python -m app.create_user --email <email> --name "<name>" --role admin`
  (re-running it for an existing email updates the role and resets the password).

## Backend

```bash
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env            # then set DATABASE_URL and JWT_SECRET

# apply migrations in order
psql -U postgres -d recruitment -f db/migrations/001_create_jobs.sql
psql -U postgres -d recruitment -f db/migrations/002_create_users.sql
psql -U postgres -d recruitment -f db/migrations/003_create_resumes.sql
psql -U postgres -d recruitment -f db/migrations/004_store_resume_files_in_db.sql

# create users (password is prompted for)
python -m app.create_user --email admin@company.com --name "Jane Admin" --role admin
python -m app.create_user --email rec@company.com   --name "Rob Recruiter" --role recruiter

uvicorn app.main:app --reload --port 8000
pytest
```

API docs: http://localhost:8000/docs (use **Authorize** with a token from `POST /auth/login`).

## Frontend

```bash
cd ../frontend
npm install
npm run dev        # http://localhost:5173, proxies /api -> http://localhost:8000
npm run build      # production build in frontend/dist
```

For a deployed API, set `VITE_API_BASE_URL` at build time and add the portal's origin to
`CORS_ORIGINS` in the backend `.env`.

## Logs

Written to `LOG_DIR` (default `logs/`), rotated at 5 MB, 5 backups kept:

- `logs/jobs_audit.log`: every job create / list / read / update / delete, with the user.
  Updates record each changed field's old and new value. Deletes record a snapshot of the
  deleted job.
- `logs/resumes_audit.log`: every resume upload, rejected upload (wrong type, too large,
  duplicate), list, view, file download and delete, with the user.
- `logs/app.log`: everything above plus sign-ups, logins (success/failure) and access-denied events.

```
JOB_UPDATED job_id=713e... user=admin@company.com changes={'title': {'old': 'Senior Python Developer', 'new': 'Lead Python Developer'}}
JOB_DELETED job_id=713e... user=admin@company.com deleted={'title': 'Lead Python Developer', ...}
ACCESS_DENIED user=rec@company.com role=recruiter action=DELETE /jobs/713e...
```

Updating or deleting a job and deleting a resume all ask for confirmation first. The job
update dialog shows each field's current and new value.
