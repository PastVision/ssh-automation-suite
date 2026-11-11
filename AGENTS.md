# AGENTS.md

Repository guidance for coding agents working on this project.

## Layout

```
backend/                 FastAPI service (runs on :8010)
  app/core/              settings, logging, Fernet encryption
  app/db/                engine/session, column helpers
  app/models/            SQLAlchemy 2 ORM models
  app/schemas/           Pydantic request/response models
  app/api/routes/        HTTP routers
  app/services/          log store, key store, concurrency limiter
  migrations/            Alembic (hand-written env.py)
  data/                  SQLite DB + Fernet key + encrypted keys  (gitignored)
  logs/                  execution transcripts                      (gitignored)
  tests/                 pytest suite
frontend/                React 19 + Vite 8 + Tailwind 4 (runs on :5180)
  src/components/ui/     shadcn/ui primitives
  src/components/layout/ app shell, sidebar, nav config
  src/pages/             one file per route
  src/lib/               api client, shared types, cn()
```

## Commands

Backend (from `backend/`, using the venv):

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --reload --port 8010
.\.venv\Scripts\python.exe -m pytest
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\ruff.exe check .
```

Frontend (from `frontend/`):

```powershell
npm run dev      # :5180, proxies /api -> :8010
npm run build    # tsc -b && vite build
npm run lint
```

## Invariants — do not break these

1. **Logs on disk, metadata in SQLite.** Execution transcripts are written to
   `logs/executions/<execution_id>/…`; the database stores only the path.
   Never put log bodies in a column.
2. **Private keys are never stored in plaintext.** Uploaded keys are Fernet
   encrypted into `data/keys/<credential_id>.key`. Only the encrypted file and
   a fingerprint may be persisted.
   The Fernet master key in `data/secret.key` is **authoritative**: generate it
   only when the file is absent. If it exists but is empty or malformed, raise
   `KeyFileError` — never silently mint a replacement, because that makes every
   existing credential permanently undecryptable. Relative
   `SSHSUITE_SECRET_KEY_FILE` values are anchored to `BACKEND_DIR`, not the CWD.
3. **Template versions are immutable.** Editing a template appends a new
   `template_version` row. Executions reference the exact version id they ran,
   so history stays reproducible. Never mutate a `template_version` row.
4. **Concurrency limit is global.** `max_concurrent_host_workflows` (default 20)
   caps *host workflows* across all executions. `services/concurrency.py` owns
   the semaphore; acquire exactly one slot per host workflow, never per step.
5. **Sequential steps, fail-fast per host.** Steps run in order on a host; the
   first failure stops that host only. Other hosts keep running.
6. **Paths are derived from `BACKEND_DIR`, never from CWD**, so the app behaves
   identically regardless of where it is launched.

7. **`testlab/` is independent of the application.** It must never import from
   `backend/`, read `backend/data/`, or share a network, volume or credential
   with the app. Its `keys/` directory is git-ignored; only the *public* key
   may enter a container. The base compose file publishes no SSH port to the
   host. Failure scenarios live in `testlab/simulations/` and are opt-in only
   — never wire artificial failures into container start-up.

8. **Host-key verification is on by default.** `ssh_transport.build_known_hosts`
   returns a sentinel meaning "let asyncssh verify against `~/.ssh/known_hosts`"
   unless a file is configured. Only `ssh_allow_unknown_host_keys=True` may
   disable it, it logs a warning, and `Settings` **rejects it outright when
   `environment == "production"`**. Never add a code path that accepts host keys
   without an explicit, logged opt-in.

9. **A run never leaves a row `running`.** Every exit path from a host workflow
   must persist a terminal state: success, failure, cancellation, or the
   unexpected-error safety net in `_run_host`. A host that finishes must also
   settle its `pending` steps (`skipped` for fail-fast, `cancelled` for
   interruption) so the UI can never show progress that will not advance.
   This includes **hard task cancellation**: `shutdown` cancels executions that
   outlive the grace period, and `CancelledError` is a `BaseException`, so the
   per-host `except Exception` guards never see it. `_run_execution` must keep
   its `except asyncio.CancelledError` + `finally` settlement, and
   `_settle_orphan_hosts` must close out any `pending`/`running` host row.

10. **SQLite writes are short and never span an SSH connection.** Each state
    transition opens its own session and commits immediately
    (`execution_state._write_session`). Holding a transaction while awaiting
    network I/O would serialise SQLite and stall every other host workflow.
    Never add a long-lived session to the runner.

11. **Scripts go over stdin, not the command line.** Templates are executed as
    `<interpreter> -s` with the body written to stdin and then closed. Do not
    interpolate script text into a shell command: it breaks on quotes and
    `$`, and invites command injection. The interpreter name is validated as a
    bare program name for the same reason.

12. **Command completion is the process exiting, not the channel closing.**
    `ssh_transport.wait_for_exit` polls `process.exit_status` instead of awaiting
    `process.wait()`. A detached grandchild inheriting stdout keeps the channel
    open, so EOF-based detection blocked a *successful* step for the child's
    lifetime and then reported it as `timed_out`. Drain output after exit for a
    bounded grace (`_EXIT_DRAIN_GRACE_SECONDS`) and never let a lingering child
    make a clean exit look like a failure.

13. **`CommandResult` retention is bounded; streaming is not.** Callbacks receive
    every byte, but the text kept in `CommandResult` is capped at
    `_CAPTURE_LIMIT_CHARS`. Do not append every chunk unconditionally: a step
    printing gigabytes would pin that much memory per in-flight host (times 20).

14. **The runner must *load* every spec kind it reads.** `_run_execution` fills
    `_variable_specs` and `_run_as_passwords` from
    `execution_state.load_variable_specs` / `load_run_as_passwords` before any
    host starts, and `_forget` drops both when the execution ends (a decrypted
    password must not outlive the run). Reading a cache nobody populates fails
    quietly rather than loudly: with the loaders missing, variable discovery
    never ran and every step rendered with empty values, and a password-mode
    Run-As block always sent `None` to `sudo`. Adding a new cached spec means
    adding it here *and* covering the path in
    `tests/test_workflow_execution_integration.py`.

## Conventions

- Python 3.13, SQLAlchemy 2 typed `Mapped[...]` style, `from __future__ import annotations`.
- Ruff with `line-length = 100`; `migrations/versions/` is excluded (generated).
- Never `create_all()` in application code — schema changes go through Alembic.
- Frontend imports use the `@/` alias. No `baseUrl` in tsconfig (removed in TS 7).
- Tailwind v4: tokens live in `src/index.css` under `@theme inline`; there is no
  `tailwind.config.js`, by design.
- The API client uses relative `/api/...` URLs. Do not hardcode a backend origin.
- Audit calls take the `Principal`, not a bare user id: pass
  `actor_id=principal.user_id, actor_username=principal.label,
  session_id=principal.session_id`. A `None` username is only correct for anonymous
  events such as a failed login, and it makes the audit trail unable to answer
  "who tried this?".
- Login must do a **constant** amount of KDF work. Any placeholder hash used on the
  "no such user" path has to be precomputed (`DUMMY_PASSWORD_HASH` in
  `app/services/sessions.py`), never generated inside the branch. Building it there
  made unknown usernames take ~1.8x longer than real ones, which disclosed which
  accounts exist. Do not add per-attempt work to only one branch of that function.

### Templates (`app/services/templates.py`, `app/api/routes/templates.py`)

- A template's body is the ordered `template_steps` of its current
  `template_versions` row. `Template.latest_version` is `max(version)`, not a FK
  (a current-version FK would be circular). Metadata (`name`, `description`) is
  copied onto each version so history shows what was true then.
- **Saves never mutate a version.** `update()` compares the incoming body against
  the latest version's `content_checksum`; unchanged content bumps only
  `row_version`, changed content appends a version. Any future edit path must do
  the same, or history stops meaning anything.
- Writes require **both** the edit lock and a non-stale `row_version`:
  `423` = you do not hold the lock, `409` = somebody saved first. Both are checked
  inside the writing session so neither can go stale between check and write.
- Access is decided by `resource_permissions` grants alone (`permissions.can_view`
  / `can_edit`) - there is no visibility flag in the read path. Setting a template
  to `private` **deletes every grant** (`permissions.revoke_all`) and releases
  locks, because a dormant grant must never silently come back to life when the
  visibility is flipped again.
- Ownership is always read from the row: an `edit` grantee may change content but
  cannot re-share, change visibility, or delete. Those answer `403`; a user with no
  access at all answers `404`, so ids cannot be probed.
- Deleting is blocked while a `workflow_steps` row pins a version
  (`ON DELETE RESTRICT`). Execution snapshots are not a blocker: they keep their
  own script copy and only null the lineage ids.
- `TemplateStep.interpreter` is validated twice (schema and service) because it is
  placed into a remote command line; only a bare program name is accepted.

## Testing the database schema

- **Test databases must come from Alembic, never `Base.metadata.create_all()`.**
  `create_all` builds tables from the models and therefore cannot fail when a
  migration is missing; that is how the M6 execution-rule columns reached
  production-shaped databases without existing there. Fixtures use
  `tests/migration_support.py`: one session-scoped migrated template, copied per
  test with `shutil.copyfile` (~2.6 ms) so the ~0.6 s `alembic upgrade head` runs
  once per session.
- Some schema objects exist **only** in migrations and are invisible to
  metadata: the append-only triggers on `audit_events` and the partial predicate
  on `uq_resource_locks_active`. A `create_all` database silently lacks both, so
  tests that pass on `create_all` can assert behaviour production does not have.
- Schema comparison authority is `tests/test_migrations.py`; `tests/test_schema.py`
  covers runtime constraint enforcement. Keep one authority per claim.
- Alembic's `compare_server_default=True` is unusable here: it compares Python
  `default=` against SQL `server_default=` and reports false drift. Server
  defaults are compared explicitly in `migration_support.server_default_differences`.

### Audit attribution: `ON DELETE RESTRICT`, never `SET NULL`

`audit_events.actor_user_id` is `ON DELETE RESTRICT` (migration `e2c9f4a17b60`).
It was originally `SET NULL`, which cannot coexist with the append-only trigger:
`SET NULL` *is* an `UPDATE`, and the trigger rejects every `UPDATE`, so deleting
an account with audit history aborted with the misleading
`audit_events is append-only`. Policy is immutable history plus account
**deactivation** (`is_active = False`); deletion is refused by the FK, which names
the real reason. Do not reintroduce `SET NULL` and do not weaken the triggers.

That revision rebuilds `audit_events` with `batch_alter_table`, and a SQLite
rebuild **drops triggers** because they live outside the reflected schema. The
migration therefore reads them from `sqlite_master` first, recreates them after,
and aborts the upgrade if either guard is missing afterwards. Any future batch
rebuild of a table carrying triggers must do the same.

## Environment quirks (Windows / PowerShell 5.1)

- Use `.\.venv\Scripts\python.exe` explicitly; `python` may resolve elsewhere.
- `manage.py`-style scripts are unnecessary; invoke uvicorn/alembic directly.
- Redirect long-running server output to a file when backgrounding, otherwise the
  process can die with the parent shell.
