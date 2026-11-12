"""Test fixtures.

Every test gets its own temporary data/log directories and its own SQLite file so
nothing touches the developer's real ``backend/data`` or ``backend/logs``.

Test databases are produced by the real Alembic migrations, not by
``Base.metadata.create_all``. ``migrated_database`` builds one migrated file per
session and each test receives a copy of it, so the suite exercises the schema a
deployment actually gets while still paying for ``upgrade head`` only once.

Isolation is enforced at *import* time, not just per test. Three things resolve
settings before or outside any fixture: ``app.main`` builds a module-level
``app = create_app()`` when it is imported, ``migrations/env.py`` calls
``get_settings()`` every time Alembic runs, and a few modules read settings during
collection. A per-test fixture cannot cover those, so the process environment is
pointed at a throwaway session directory below -- before any ``app`` module is
imported. Without this, a plain ``pytest`` run creates the developer's real
``backend/data`` directory and may write a database into it.
"""

from __future__ import annotations

import atexit
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

# --- Process-wide isolated runtime paths ---------------------------------- #
# Must run before importing anything under ``app``. A session ``mkdtemp`` (not a
# pytest fixture) is used precisely because fixture setup happens too late: the
# first ``from app.main import ...`` already built an app from real settings.
_SESSION_RUNTIME = Path(tempfile.mkdtemp(prefix="sshsuite-test-runtime-"))
atexit.register(shutil.rmtree, _SESSION_RUNTIME, ignore_errors=True)

# Force rather than default: a developer with SSHSUITE_DATA_DIR exported for a
# real run must still get an isolated suite, and inheriting it silently would
# point every test at their live database.
os.environ["SSHSUITE_DATA_DIR"] = str(_SESSION_RUNTIME / "data")
os.environ["SSHSUITE_LOG_DIR"] = str(_SESSION_RUNTIME / "logs")
os.environ["SSHSUITE_SECRET_KEY_FILE"] = str(_SESSION_RUNTIME / "data" / "secret.key")
os.environ["SSHSUITE_DATABASE_URL"] = ""
os.environ["SSHSUITE_ENVIRONMENT"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app.core import security  # noqa: E402
from app.core.config import BACKEND_DIR, Settings, get_settings  # noqa: E402
from app.db import session as db_session  # noqa: E402
from app.services import concurrency  # noqa: E402


# --- Real-runtime baseline ------------------------------------------------ #
# Captured here, at conftest import, for two reasons. It precedes every test
# module import (so also the first ``from app.main import ...``, which builds an
# app and calls ``ensure_directories``), and it uses the ``BACKEND_DIR`` constant
# rather than ``get_settings()``, so neither a changed working directory nor a
# cleared/repopulated settings cache can move what is being compared.
def _snapshot_real_runtime() -> dict[str, tuple[str, int, int]]:
    """Every entry under the real data/log dirs -> (kind, size, mtime_ns).

    Directories are recorded as well as files. ``ensure_directories`` only ever
    calls ``mkdir``, so a leak can create empty ``data/keys`` or ``logs/executions``
    trees that hold no files; tracking files alone would miss exactly the shape of
    the original bug.
    """
    snapshot: dict[str, tuple[str, int, int]] = {}
    for root in (BACKEND_DIR / "data", BACKEND_DIR / "logs"):
        if not root.exists():
            continue
        snapshot[str(root)] = ("dir", 0, root.stat().st_mtime_ns)
        for path in sorted(root.rglob("*")):
            stat = path.stat()
            kind = "dir" if path.is_dir() else "file"
            snapshot[str(path)] = (kind, stat.st_size, stat.st_mtime_ns)
    return snapshot


_REAL_RUNTIME_BASELINE = _snapshot_real_runtime()


def _runtime_diff(
    before: dict[str, tuple[str, int, int]], after: dict[str, tuple[str, int, int]]
) -> tuple[list[str], list[str], list[str]]:
    """Created, deleted and modified paths between two runtime snapshots."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    modified = sorted(name for name in set(before) & set(after) if before[name] != after[name])
    return added, removed, modified


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    # Clear every ambient SSHSUITE_* variable first. Without this, a developer who
    # exported e.g. SSHSUITE_SSH_ALLOW_UNKNOWN_HOST_KEYS for the test lab would
    # silently change what these tests assert — the settings must depend only on
    # what this fixture sets.
    for name in [key for key in os.environ if key.startswith("SSHSUITE_")]:
        monkeypatch.delenv(name, raising=False)

    monkeypatch.setenv("SSHSUITE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SSHSUITE_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("SSHSUITE_DATABASE_URL", "")
    monkeypatch.setenv("SSHSUITE_ENVIRONMENT", "test")
    monkeypatch.setenv("SSHSUITE_SECRET_KEY_FILE", str(tmp_path / "data" / "secret.key"))

    get_settings.cache_clear()
    db_session.reset_engine()
    security.reset_fernet_cache()
    concurrency.reset_limiter()

    # Build from the environment only. A developer's backend/.env is deliberately
    # ignored here, so tests cannot inherit local SSH or credentials settings.
    from app.core.config import Settings as _Settings

    resolved = _Settings(_env_file=None)
    resolved.ensure_directories()
    yield resolved

    get_settings.cache_clear()
    db_session.reset_engine()
    security.reset_fernet_cache()
    concurrency.reset_limiter()


@pytest.fixture(scope="session")
def migrated_template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One database built by the real migration chain, shared read-only.

    Built once per session because ``upgrade head`` costs ~0.6s while copying the
    result costs ~3ms. Tests must never write to the returned file: they receive
    a copy (see ``migrated_db``). This fixture is also what the migration tests
    inspect, so there is a single definition of "the schema the app expects".
    """
    from tests import migration_support

    directory = tmp_path_factory.mktemp("migrated-template")
    return migration_support.build_migrated_template(directory / "template.db")


@pytest.fixture()
def migrated_db(settings: Settings, migrated_template_db: Path) -> Settings:
    """A fully migrated, per-test database at the settings' default path.

    The schema comes from Alembic (via the session template) rather than from ORM
    metadata, so a migration that fails to create a column, index or trigger fails
    the tests that depend on it instead of passing on a metadata-built lookalike.
    """
    from tests import migration_support

    migration_support.copy_database(migrated_template_db, settings.sqlite_path)
    db_session.reset_engine()
    return settings


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    from app.main import create_app

    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def auth_settings(settings: Settings) -> Settings:
    """Settings suitable for cookie-authenticated tests.

    ``cookie_secure`` is off so Starlette's test client (plain ``http://testserver``)
    will store and resend the session cookie. The production guard for this flag is
    covered separately by a configuration test.
    """
    settings.cookie_secure = False
    return settings


@pytest.fixture()
def app_client(auth_settings: Settings, migrated_db: Settings) -> Iterator[TestClient]:
    """An unauthenticated client on a migrated, fully wired app."""
    from app.main import create_app

    app = create_app(auth_settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def users(app_client: TestClient) -> dict[str, int]:
    """Two real accounts with distinct passwords."""
    from app.services import sessions

    alice = sessions.create_user("alice", "alice-password-1", display_name="Alice")
    bob = sessions.create_user("bob", "bob-password-1", display_name="Bob")
    return {"alice": alice.id, "bob": bob.id}


def sign_in(client: TestClient, username: str, password: str) -> str:
    """Log in on ``client`` and return the CSRF token to send as a header.

    A separate ``TestClient`` has a separate cookie jar, which is how the tests
    represent two different browsers.
    """
    response = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def csrf(token: str) -> dict[str, str]:
    return {"X-CSRF-Token": token}


@pytest.fixture(autouse=True)
def _no_real_home_config() -> Iterator[None]:
    """Guard against accidentally reading a developer's ``.env``."""
    previous = os.environ.get("SSHSUITE_DATA_DIR")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("SSHSUITE_DATA_DIR", None)


@pytest.fixture(autouse=True)
def _reset_caches_between_tests() -> Iterator[None]:
    """Drop every cached settings/engine object after each test.

    ``get_settings`` is ``lru_cache``-backed and several helpers (the lab helper,
    the proxy-settings builders, the migration tooling) call ``cache_clear`` and
    then repopulate it with their own values. Without a reset the *next* test
    inherits those settings -- including its data directory -- which is how a test
    can end up pointed at a database it never asked for. Resetting afterwards
    makes each test start from the pinned, isolated environment.
    """
    yield
    get_settings.cache_clear()
    db_session.reset_engine()
    security.reset_fernet_cache()
    concurrency.reset_limiter()


@pytest.fixture(scope="session", autouse=True)
def _real_runtime_is_untouched() -> Iterator[None]:
    """Fail the run if anything wrote to ``backend/data`` or ``backend/logs``.

    The per-test ``settings`` fixture is not sufficient on its own: settings are
    also resolved at import time (``app.main``) and by Alembic (``env.py``), which
    is how a full run used to create the real data directory. The baseline was
    taken when this module was imported, before any test module could import the
    application, so even the very first ``ensure_directories`` call is covered.
    """
    yield

    added, removed, modified = _runtime_diff(_REAL_RUNTIME_BASELINE, _snapshot_real_runtime())
    if not (added or removed or modified):
        return

    detail = "\n".join(
        part
        for part in (
            "\n".join(f"  created {name}" for name in added),
            "\n".join(f"  deleted {name}" for name in removed),
            "\n".join(f"  modified {name}" for name in modified),
        )
        if part
    )
    pytest.fail(
        "The test run wrote to the real backend/data or backend/logs tree. Tests "
        "must resolve paths to a temporary directory; see the import-time "
        "bootstrap at the top of tests/conftest.py.\n" + detail,
        pytrace=False,
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "lab: real SSH integration test that needs the Docker test lab running",
    )
    config.addinivalue_line(
        "markers",
        "migration: exercises the Alembic chain rather than the running application",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip real-SSH tests when the lab is down, unless CI requires it.

    ``SSHSUITE_REQUIRE_LAB=1`` turns the skip into a failure so the integration
    suite cannot silently disappear from a pipeline.
    """
    from tests import lab_support

    if lab_support.lab_available():
        return
    require = os.environ.get("SSHSUITE_REQUIRE_LAB") == "1"
    message = (
        "SSH test lab not reachable on 127.0.0.1:2201. Start it with: "
        "cd testlab && docker compose -f docker-compose.yml -f compose.loopback.yml up -d"
    )
    for item in items:
        if "lab" not in item.keywords:
            continue
        if require:
            item.add_marker(pytest.mark.fail(reason=message))
        else:
            item.add_marker(pytest.mark.skip(reason=message))
