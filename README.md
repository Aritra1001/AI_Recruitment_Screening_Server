# AI Recruitment Screening

FastAPI backend (`app/`) + React portal (`frontend/`) for managing job openings and
candidate resumes.

## Roles

| Role        | Jobs                                   | Resumes                                              | Screening                                   |
|-------------|----------------------------------------|------------------------------------------------------|---------------------------------------------|
| `admin`     | read; create, update, delete\*         | upload, view, download, score; delete any\*          | send shortlisted resumes; manage domains    |
| `recruiter` | read only (approved jobs only)         | upload, view, download, score; delete own uploads\*  | send shortlisted resumes                    |
| `manager`   | read only (approved jobs only)         | view, download, read scores                          | interview / reject for their domains' jobs  |

\* Only once another admin approves it (see [Approvals](#approvals)).

The API enforces this (`403` otherwise); the portal hides the actions and routes a role
cannot use.

## Approvals

Creating, editing or deleting a job, and deleting a resume (by anyone), doesn't happen
straight away: it becomes an approval request (`approval_requests`, `app/approvals.py`) and
every **other** active admin gets an email with the change and a link to
`<PORTAL_URL>/approvals/<id>`. That page (sign-in required; laid out for phones) shows the
change and has **Approve** / **Reject** buttons with an optional comment. The first admin to
decide wins; the requester is emailed the outcome.

- **Until approved nothing changes for recruiters.** A new job is saved unpublished
  (`jobs.is_published = false`) and is invisible to recruiters (`404`); an edit waits in the
  request while the job keeps its current values; a job or resume to be deleted stays (a resume
  can still be scored).
- **Admins see the status.** `JobRead.approval` (`pending` / `approved`, plus the pending
  request) is only filled in for admins; recruiters always get `null`. A resume's
  `pending_deletion` is shown to admins and to whoever asked for it.
- **Nobody approves their own request** (`403`; also a CHECK constraint). With no other active
  admin the change is refused (`409`), so at least two admin accounts are needed.
- **One pending request per job / resume.** Another edit or a delete is refused (`409`) until
  it is approved, rejected, withdrawn or expired (a partial unique index makes this race-free).
  Approving locks the request row, so two admins approving at once can't both apply it.
- **Withdrawing.** The requester can withdraw their own pending request (e.g. to fix a typo).
- **Expiry.** A request nobody decides on expires after `APPROVAL_EXPIRY_HOURS` (default 48)
  and the requester is emailed. A background sweep checks every 10 minutes (and at start-up);
  the endpoints that use pending requests also check first.
- Rejecting, withdrawing or expiring a **new** job deletes the unpublished job. The request
  itself is kept as the audit trail (`job_id` / `resume_id` become null once the target is gone).

Endpoints: `GET /approvals?status=pending|approved|rejected|expired|cancelled|all` (admins),
`GET /approvals/{id}` (admins, or the requester), `POST /approvals/{id}/approve`,
`POST /approvals/{id}/reject` (admins; body `{"comment": "..."}` optional),
`POST /approvals/{id}/cancel` (the requester). `POST /jobs`, `PATCH /jobs/{id}`,
`DELETE /jobs/{id}` and `DELETE /resumes/{id}` return `202` with the job / resume and its
pending request. A `PATCH` that changes nothing returns `200` and creates no request.

### Email (Azure Communication Services)

1. In the Azure portal create a **Communication Services** resource and an **Email
   Communication Services** resource. Add a domain to the latter (the free
   *Azure managed domain* is quickest) and **connect** it to the Communication Services resource.
2. In `.env` set `ACS_CONNECTION_STRING` (Communication Services → *Keys*),
   `EMAIL_SENDER_ADDRESS` (the domain's MailFrom address, e.g. `DoNotReply@<guid>.azurecomm.net`)
   and `PORTAL_URL` (where the portal is hosted, so the link works from a phone).

Without `ACS_CONNECTION_STRING` nothing is sent; each email is logged as `EMAIL_SKIPPED`.
Emails are sent after the response, so a failed email (`EMAIL_FAILED` in `logs/app.log`)
never fails the change. Azure-managed domains have low sending limits and may land in spam;
connect your own domain for real use.

## Resumes

- **Every resume is uploaded for one job** (`resumes.job_id`) and is only ever scored against
  that job. `POST /resumes` is multipart with `file` and a required `job_id` (a job the uploader
  can see: recruiters only approved ones). In the portal, upload from the job's page
  (**Upload resumes**) or pick the job on the upload page.
- `GET /resumes?job_id=<id>` lists one job's resumes; without it, every resume the user can see
  (recruiters don't see resumes uploaded for a job that is still awaiting approval).
- Deleting a job deletes the resumes uploaded for it (and their scores).
- PDF or DOCX, up to `MAX_RESUME_MB` (default 10) each. The file's first bytes must match
  its extension; the same file can't be uploaded twice for the same job (checked by SHA-256).
  It can be uploaded again for a different job.
- The file itself is stored in the database (`resumes.content`, `BYTEA`), next to its
  metadata, so a database backup covers everything and deleting the row deletes the file.
  The column is loaded only by the download endpoint (`GET /resumes/{id}/file`); listing and
  viewing resumes never read it.
- Extraction isn't built yet: every resume stays `uploaded` ("Not extracted"). The fields
  the extraction step must fill are defined by `ExtractedResume` in `app/schemas.py` and
  stored in `resumes.extracted`; the detail page renders them once `status = 'extracted'`.

## Resume scoring

The `resume-scorer` agent in Microsoft Foundry scores an extracted resume against a job
(`app/resume_scoring.py`). Set `AZURE_AI_PROJECT_ENDPOINT` and `SCORING_AGENT_VERSION` in
`.env`; authentication is Entra ID (`az login` locally, with the Azure AI User role on the
project; a managed identity when deployed).

- `POST /jobs/{job_id}/scores` (body `{"resume_ids": [...]}`, or no body for every extracted
  resume uploaded for the job) starts scoring in the background (`status = scoring`). Resumes
  uploaded for another job are refused (`409`). Any signed-in user can score. One already
  scoring is skipped.
- The agent isn't fully deterministic, so the same input is never sent twice: each score keeps
  `input_hash` (SHA-256 of the agent name, `SCORING_AGENT_VERSION` and the exact input), and a
  resume whose job, resume and agent version are unchanged gets its stored score back without
  calling the agent. Editing the job, re-extracting the resume or changing the agent version
  re-scores it; `"force": true` re-scores anyway. Keep the version pinned: with it empty, a new
  agent version isn't noticed.
- `GET /jobs/{job_id}/scores` lists the scores of the job's own resumes, best first;
  `GET /jobs/{job_id}/scores/{resume_id}` returns one.
- The agent scores six criteria (0-100) with a rationale and evidence each. The backend clamps
  the scores, reconciles matched/missing skills with the job's lists, and computes the weighted
  `overall_score` (`WEIGHTS`) and `recommendation` (`strong_fit` 80+, `good_fit` 65+,
  `partial_fit` 50+, else `not_a_fit`). The weights, agent version and model are stored with
  each score.
- The candidate's name is not sent to the agent. Results are stored in `resume_scores`, which
  is deleted along with its job or resume.
- `tests/fixtures/scoring/` holds test cases in the exact shape the backend sends, for trying
  the agent in the Foundry playground.

## Domains and screening

The hiring flow after scoring: a recruiter shortlists the resumes that meet the job's minimum
score, and a manager of the job's domain decides who gets an interview.

```
uploaded → extracted → scored ─(score ≥ job's minimum)→ sent to manager → interview | rejected
```

- **Domains** (`domains`, e.g. Software, Data Engineering) are managed by admins on the
  **Domains** page (`/domains`): add, rename, delete (refused while jobs use it) and assign
  managers (`domain_managers`; a domain can have several, a manager can cover several). These
  take effect straight away, without a second admin's approval.
- **Each job has a domain and a minimum score to shortlist** (`jobs.domain_id`,
  `jobs.shortlist_min_score`, default 60). Both are set in the job form and, like every job
  field, changed through the approval flow. Jobs created before domains existed have none until
  an admin edits them; their resumes can't be sent until then.
- **Sending** (`POST /jobs/{job_id}/screenings`, body `{"resume_ids": [...], "note": "..."}`,
  admins and recruiters): every resume must be uploaded for the job, have a **current** score
  (re-score it if the job or resume changed since) of at least the job's minimum, and not have
  been sent before; the domain must have an active manager. All or nothing: otherwise `409`
  listing every problem. The domain's managers are emailed (one email per send).
- **Deciding** (`POST /screenings/{id}/decision`, body `{"decision": "interview" | "rejected",
  "comment": "..."}`): only a manager of the job's domain, once; a comment is required to reject.
  The recruiter who sent it is emailed the decision and comment.
- `GET /screenings?status=pending|interview|rejected|all&job_id=&resume_id=` lists them; managers
  only see their domains' jobs, admins and recruiters see all. Portal: **Screening**
  (`/screening`, with a count of what's waiting for a manager) and `/screening/{id}` (where the
  manager's email links; laid out for phones). On a job's page the **Stage** column shows each
  candidate's status, and the ones that meet the minimum can be selected and sent.
- The domain isn't stored on the screening: it is the job's, so moving a job to another domain
  hands its pending candidates to that domain's managers.
- Managers can't upload, extract, score or delete resumes (`403`).

## Accounts

- **Sign up** (`POST /auth/signup`, or "Create an account" in the portal): anyone can register.
  New accounts are always `recruiter`; the request has no role field.
- **Sign in with Google** (`POST /auth/google`, or the Google button on the sign-in and sign-up
  pages): set `GOOGLE_CLIENT_ID` in the backend `.env` and `VITE_GOOGLE_CLIENT_ID` in the
  frontend `.env` to the client ID of a "Web application" OAuth client from Google Cloud
  Console, whose Authorized JavaScript origins include the portal's origin (for local
  development both `http://localhost:5173` and `http://localhost`). The backend verifies the
  Google ID token and then:
  - signs in the account already linked to that Google account;
  - otherwise links an existing account with the same (verified) email, which keeps its role
    and password;
  - otherwise creates a `recruiter` account without a password (it can only sign in with
    Google until `app.create_user` sets one).

  Set `GOOGLE_ALLOWED_DOMAIN=company.com` to accept only that Workspace domain's accounts.
  Without `GOOGLE_CLIENT_ID` the endpoint answers `503` and the button is hidden.
- **Promote to admin or manager**: `python -m app.create_user --email <email> --name "<name>" --role admin`
  (or `--role manager`; re-running it for an existing email updates the role and resets the
  password). Then assign managers to their domains on the Domains page.

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
psql -U postgres -d recruitment -f db/migrations/005_create_resume_scores.sql
psql -U postgres -d recruitment -f db/migrations/006_create_approval_requests.sql
psql -U postgres -d recruitment -f db/migrations/007_resumes_belong_to_a_job.sql
psql -U postgres -d recruitment -f db/migrations/008_domains_and_screening.sql
psql -U postgres -d recruitment -f db/migrations/009_google_sign_in.sql

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

### Hosting (e.g. Vercel for the portal, Render for the API)

- **Portal**: deploy `frontend/` as a static site (build `npm run build`, output `dist`) with
  `VITE_API_BASE_URL=https://<api>.onrender.com`. Deep links such as the email's
  `/approvals/<id>` need every path served by `index.html`: `vercel.json` does that on Vercel;
  on a Render static site add a rewrite rule `/*` → `/index.html`.
- **API**: a Render web service running `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
  with the `.env` values as environment variables. Set `PORTAL_URL` to the portal's URL and add
  it to `CORS_ORIGINS`.
- Render's free instances sleep when idle, so the first request after a while is slow and the
  expiry sweep only runs while it is awake (expired requests are still caught on the next use).

## Logs

Written to `LOG_DIR` (default `logs/`), rotated at 5 MB, 5 backups kept:

- `logs/jobs_audit.log`: every job create / update / delete request and the change once it
  is approved (`approved_by=`), plus lists and reads, with the user. Updates record each
  changed field's old and new value. Deletes record a snapshot of the deleted job.
- `logs/resumes_audit.log`: every resume upload, rejected upload (wrong type, too large,
  duplicate), list, view, file download and delete, with the user.
- `logs/scores_audit.log`: every scoring request, result (overall score, recommendation,
  time taken), failure, list and read, with the user.
- `logs/approvals_audit.log`: every approval request, approval, rejection (with comment),
  withdrawal and expiry, with who asked and who decided.
- `logs/app.log`: everything above plus sign-ups, logins (success/failure) and access-denied events.

```
JOB_UPDATE_REQUESTED job_id=713e... request_id=9c1f... user=admin@company.com changes={'title': {'old': 'Senior Python Developer', 'new': 'Lead Python Developer'}}
JOB_UPDATED job_id=713e... user=admin@company.com approved_by=admin2@company.com changes={'title': {...}}
JOB_DELETED job_id=713e... user=admin@company.com approved_by=admin2@company.com deleted={'title': 'Lead Python Developer', ...}
ACCESS_DENIED user=rec@company.com role=recruiter action=DELETE /jobs/713e...
```

Updating or deleting a job and deleting a resume all ask for confirmation first. The job
update dialog shows each field's current and new value; the approval page shows the same.
