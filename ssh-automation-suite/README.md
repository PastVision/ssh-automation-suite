# SSH Automation Suite

Run reusable shell-script templates over SSH across many hosts, from a small
self-hosted web UI. No Redis, no Celery, no Kubernetes — one FastAPI process, a
SQLite file, and logs on disk.

> **Status: Milestone 1 — foundation only.**
> This milestone establishes the project skeleton, data model, configuration and
> a working dashboard. SSH execution, template editing and workflow
> orchestration are intentionally **not** implemented yet. See
> [Roadmap](#roadmap).

---

## Stack

| Layer     | Technology                                                                 |
| --------- | -------------------------------------------------------------------------- |
| Frontend  | React 19, TypeScript, Vite 8, Tailwind CSS 4, shadcn/ui, React Router, TanStack Query |
| Backend   | Python 3.13, FastAPI, SQLAlchemy 2, Alembic, Pydantic v2                   |
| Storage   | SQLite for metadata, filesystem for execution logs, Fernet for secrets     |
| Planned   | AsyncSSH (SSH transport), APScheduler (scheduling), WebSockets (live logs)  |

---

## Requirements

- Python **3.11+** (developed on 3.13)
- Node.js **20+** and npm (developed on Node 24) — or `pnpm`/`yarn` if you adapt the lockfile
- Windows, macOS or Linux. Commands below show PowerShell first, with POSIX in notes.

---

## Getting started

### 1. Backend

```powershell
cd backend

# Create and populate the virtual environment
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"

# Optional: copy and review configuration
Copy-Item .env.example .env
```

```bash
# macOS / Linux equivalent
cd backend
python3 -m venv .venv
./.venv/bin/python -m pip install -e ".[dev]"
cp .env.example .env
```

Apply database migrations, then start the API:

```powershell
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8010
```

- API: <http://127.0.0.1:8010>
- OpenAPI docs: <http://127.0.0.1:8010/docs>
- Health check: <http://127.0.0.1:8010/api/health>

Everything the app needs (directories, the SQLite file, the Fernet key) is
created on first start.

Create an account before signing in. There is deliberately no HTTP sign-up
route, so accounts are provisioned from the CLI:

```powershell
# Prompts for the password (12 characters minimum, never echoed)
.\.venv\Scripts\python.exe -m app.cli create-user alice --display-name "Alice"

# Change a password later; this also revokes that user's active sessions
.\.venv\Scripts\python.exe -m app.cli set-password alice

.\.venv\Scripts\python.exe -m app.cli list-users
```

Omit `--display-name` if you do not want one. Pass `--password-stdin` to read the
password from a pipeline instead of a prompt.

### 2. Frontend

```powershell
cd frontend
npm install
npm run dev
```

Open <http://localhost:5180>. The dev server proxies `/api` to the backend on
port 8010, so no CORS configuration or hardcoded origin is needed during
development.

---

## Configuration

Settings are read from environment variables prefixed `SSHSUITE_` or from
`backend/.env`. Every value has a safe default; see `backend/.env.example`.

| Variable                                  | Default                | Purpose                                        |
| ----------------------------------------- | ---------------------- | ---------------------------------------------- |
| `SSHSUITE_ENVIRONMENT`                    | `development`          | `development` \| `test` \| `production`        |
| `SSHSUITE_API_PREFIX`                     | `/api`                 | Mount point for all HTTP routes                |
| `SSHSUITE_DATA_DIR`                       | `backend/data`         | SQLite DB, Fernet key, encrypted keys          |
| `SSHSUITE_LOG_DIR`                        | `backend/logs`         | Execution transcripts                          |
| `SSHSUITE_DATABASE_URL`                   | *derived*              | Override the SQLite URL                        |
| `SSHSUITE_SECRET_KEY_FILE`                | `data/secret.key`      | Fernet key file for private-key encryption     |
| `SSHSUITE_MAX_CONCURRENT_HOST_WORKFLOWS`  | `20`                   | Global ceiling on concurrent host workflows    |
| `SSHSUITE_SSH_CONNECT_TIMEOUT_SECONDS`    | `15`                   | TCP/SSH handshake timeout                      |
| `SSHSUITE_SSH_COMMAND_TIMEOUT_SECONDS`    | `300`                  | Default per-step timeout                       |
| `SSHSUITE_SSH_KNOWN_HOSTS_FILE`           | *unset*                | Verify host keys against this file             |
| `SSHSUITE_SSH_ALLOW_UNKNOWN_HOST_KEYS`    | `false`                | Disables verification. Test lab only.          |
| `SSHSUITE_SHUTDOWN_GRACE_SECONDS`         | `30`                   | Grace period for in-flight runs at shutdown    |
| `SSHSUITE_SCHEDULER_TIMEZONE`             | `UTC`                  | Timezone for scheduled workflows               |
| `SSHSUITE_AUTH_MODE`                      | `local`                 | `local` (password accounts) or `proxy`         |
| `SSHSUITE_SESSION_IDLE_TIMEOUT_MINUTES`   | `60`                   | Session dies after this much inactivity        |
| `SSHSUITE_SESSION_ABSOLUTE_TIMEOUT_HOURS` | `12`                   | Hard ceiling on a session's life, active or not |
| `SSHSUITE_COOKIE_SECURE`                  | *environment-dependent* | `Secure` flag on session cookies               |
| `SSHSUITE_CORS_ORIGINS`                   | `http://localhost:5180,http://127.0.0.1:5180` | Comma-separated allowed browser origins |

Paths are resolved relative to the `backend/` directory, never the current
working directory.

`SSHSUITE_COOKIE_SECURE` is on by default outside `development`, where it is off
so a plain-HTTP dev server can hold a session. Setting it to `false` with
`SSHSUITE_ENVIRONMENT=production` is refused at startup: session cookies must not
travel in clear text.

### Authentication

Every `/api` route except the health check and the login/status endpoints
requires a session. Two modes are supported:

- **`local`** (default) — accounts with password hashes, provisioned with
  `app.cli`. The session lives in an `HttpOnly` cookie; state-changing requests
  must also carry the `X-CSRF-Token` header, which is what makes the cookie safe
  against cross-site form posts.
- **`proxy`** — a trusted reverse proxy asserts the identity in a header. Only
  requests whose peer address is inside `SSHSUITE_TRUSTED_PROXY_CIDRS` are
  believed, and production additionally requires `SSHSUITE_TRUST_PROXY_SECRET`
  so the identity header cannot be forged by a co-located process.

All data is owner-scoped: credentials, templates, workflows and executions are
only readable by the account that created them, and asking for somebody else's
record answers `404` rather than `403` so ids cannot be probed. Security-relevant
actions are appended to an audit log that the database itself refuses to update
or delete.

### Host-key verification

Host-key verification is **on by default**. When `SSHSUITE_SSH_KNOWN_HOSTS_FILE`
is unset, asyncssh verifies against the current user's `~/.ssh/known_hosts`, so an
unknown host is rejected rather than silently trusted. Point the variable at a
managed file to control the trust store explicitly.

`SSHSUITE_SSH_ALLOW_UNKNOWN_HOST_KEYS=true` turns verification *off*. It exists
only for the disposable Docker test lab, logs a warning on every connection, and
is **rejected at startup when `SSHSUITE_ENVIRONMENT=production`**, so the unsafe
setting cannot reach a real deployment.

### Execution engine

`app/services/execution_runner.py` runs one workflow against many hosts.

- Each **host** is an independent host workflow: one connection, all steps in
  order, stop at the first failure.
- Host workflows run concurrently, capped by a single process-wide
  `HostWorkflowLimiter` (default 20). A slot is held for the whole host workflow,
  never per step, so the ceiling means what it says.
- Steps run **sequentially per host** and are fed to the remote interpreter on
  stdin, so scripts containing quotes, `$` or newlines run exactly as written.
- **Fail-fast is per host.** A failing step marks it `failed`, marks the remaining
  steps on that host `skipped`, and leaves every other host untouched.
- Cancellation and shutdown are cooperative: in-flight commands are killed, final
  states are persisted, and no step is left `running`.
- Templates are **snapshotted** into `execution_template_snapshots` at creation
  time, so editing a template later cannot change a run already in flight or its
  recorded history.
- Transcripts stream to `logs/executions/<id>/<host_run>-<host>.log`. Remote stderr
  is wrapped in `----- stderr -----` blocks so the two streams stay individually
  identifiable; remote stdout is verbatim. Nothing is buffered whole in memory.

---

## Data model

Ten application tables, created by the initial Alembic revision. Alembic also
creates its own `alembic_version` bookkeeping table, so a migrated database
contains eleven tables in total:

```
ssh_credentials                 username + encrypted key reference + fingerprint
templates                       template identity (name, description)
template_versions               immutable snapshots: content, interpreter, timeout
workflows                       workflow identity (name, description, enabled)
workflow_steps                  ordered (workflow_id, position) -> template_version_id
workflow_hosts                  reusable hostname list per workflow
executions                      one run: workflow, trigger, status, timing
execution_template_snapshots    frozen script content for each step of the run
execution_host_runs             one row per (execution, hostname): status, log path
execution_step_runs             one row per (host_run, step): status, exit code, log path
alembic_version                 Alembic's own migration bookkeeping (not domain data)
```

Design choices worth knowing:

- **Immutable template versions.** Editing a template appends a new version. An
  execution references the exact version each step used, so history stays
  reproducible even after templates change.
- **Logs on disk.** `execution_step_runs.log_path` / `execution_host_runs.log_path`
  point at files under `logs/executions/<execution_id>/`. Log bodies are never
  stored in SQLite.
- **Keys are encrypted.** Private keys are Fernet-encrypted into
  `data/keys/<credential_id>.key`; only a fingerprint lives in the database.
- **The concurrency limit has one owner.** `app/services/concurrency.py` wraps a
  single process-wide `asyncio.Semaphore`, acquired once per host workflow — not
  per step — so 20 hosts can run while steps stay sequential per host.

---

## Project structure

```
ssh-automation-suite/
├── AGENTS.md                  agent/maintainer guidance and invariants
├── backend/
│   ├── alembic.ini
│   ├── pyproject.toml
│   ├── .env.example
│   ├── app/
│   │   ├── main.py            app factory, CORS, router mounting
│   │   ├── core/              config, logging, Fernet encryption
│   │   ├── db/                engine, session, shared column types
│   │   ├── models/            SQLAlchemy ORM models
│   │   ├── schemas/           Pydantic models
│   │   ├── api/routes/        HTTP endpoints
│   │   └── services/          log store, key store, concurrency limiter
│   ├── migrations/            Alembic environment and revisions
│   ├── data/                  SQLite + keys (gitignored)
│   ├── logs/                  execution transcripts (gitignored)
│   └── tests/                 pytest suite
└── frontend/
    ├── vite.config.ts         port 5180, /api -> 8010 proxy, @/ alias
    └── src/
        ├── components/ui/     shadcn/ui primitives
        ├── components/layout/ app shell, sidebar, nav config
        ├── pages/             dashboard + four placeholders
        ├── lib/               API client, shared types
        └── hooks/             React Query hooks
└── testlab/                 SSH integration test lab (independent of the app)
    ├── Dockerfile             one reusable OpenSSH server image
    ├── docker-compose.yml     20 hosts, dedicated network, no host ports
    ├── scripts/lab.py         keys / up / down / health / verify-auth / exec
    ├── simulations/           opt-in failure scenarios
    └── tests/                 artifact checks + live integration tests
`

The 	estlab/ directory is self-contained: it is neither imported by the
application nor able to reach its database or encryption key. See
[testlab/README.md](testlab/README.md).``

---

## API

| Method | Path                              | Description                                         |
| ------ | --------------------------------- | --------------------------------------------------- |
| `GET`  | `/api/health`                     | Liveness + readiness: database, storage paths, scheduler state, concurrency budget. Returns `503` when the database is unreachable. |
| `POST` | `/api/executions`                 | Start an execution. Returns `202` with the new id.  |
| `GET`  | `/api/executions/{id}`            | Execution detail: per-host and per-step status.     |
| `POST` | `/api/executions/{id}/cancel`     | Request cooperative cancellation.                   |

An execution is started by supplying the steps and hosts inline, because template
and workflow CRUD do not exist yet:

```jsonc
POST /api/executions
{
  "workflow_name": "smoke",
  "credential_id": 1,
  "hosts": [{ "hostname": "127.0.0.1", "port": 2201 }],
  "steps": [{ "name": "hello", "script": "echo hello\n" }]
}
```

Read `GET /api/executions/{id}` to see `status` (`pending` -> `running` ->
`succeeded` / `failed` / `cancelled`), and each host's `log_path`. Response bodies
never contain private key material.

---

## Development

### Backend

```powershell
cd backend
.\.venv\Scripts\python.exe -m pytest        # test suite
.\.venv\Scripts\ruff.exe check .            # lint
.\.venv\Scripts\ruff.exe format .           # format
```

### Database changes

Never call `create_all()` in application code. Generate a revision, review it,
then apply it:

```powershell
cd backend
.\.venv\Scripts\alembic.exe revision --autogenerate -m "describe the change"
.\.venv\Scripts\alembic.exe upgrade head
```

### Frontend

```powershell
cd frontend
npm run dev
npm run build     # typecheck (tsc -b) + production bundle
npm run lint
```

### Tests

The backend suite covers configuration, encryption/key storage, the log store,
the concurrency limiter, the health endpoint, the schema (including running the
real Alembic migration against an empty database), and the execution engine's
state machine and security properties. Tests use a temporary data directory, so
they never touch `backend/data/`.

```powershell
.\.venv\Scripts\python.exe -m pytest -v
```

Some tests are **real SSH integration tests** (`-m lab`): they connect to the
Docker test lab and are skipped when it is not running, so the default run stays
fast. Start the lab first to exercise the engine end to end:

```powershell
cd testlab
docker compose -f docker-compose.yml -f compose.loopback.yml up -d
cd ..\backend
.\.venv\Scripts\python.exe -m pytest -m lab -v
```

Set `SSHSUITE_REQUIRE_LAB=1` in CI so a missing lab becomes a failure instead of a
silent skip — otherwise the integration coverage can disappear unnoticed.

The integration suite asserts real behaviour, not mocks: exit codes, fail-fast and
skip ordering, stdout/stderr separation, 2 MB of untruncated output, per-host
isolation, the global concurrency ceiling, cancellation, and shutdown.

---

## Roadmap

| Milestone | Scope |
| --------- | ----- |
| **1 (done)** | Foundation: structure, config, SQLite + migrations, health endpoint, dashboard shell |
| **2 (done)** | SSH credentials, versioned templates, workflows, execution history, log store, SSH test lab |
| **3 (done)** | SSH execution engine: 20-host concurrency, sequential steps, fail-fast per host, cancellation, shutdown |
| 4 | Execution history UI, filesystem log viewer, WebSockets |
| 5 | APScheduler scheduling |
| 6 | End-to-end workflow authoring in the UI |

---

## License

Unlicensed / private project unless a `LICENSE` file is added.
