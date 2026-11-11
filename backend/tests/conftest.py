"""Test fixtures.

Every test gets its own temporary data/log directories and its own SQLite file so
nothing touches the developer's real ``backend/data`` or ``backend/logs``.

Test databases are produced by the real Alembic migrations, not by
``Base.metadata.create_all``. ``migrated_database`` builds one migrated file per
session and each test receives a copy of it, so the suite exercises the schema a
deployment actually gets while still paying for ``upgrade head`` only once.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core import security
from app.core.config import Settings, get_settings
from app.db import session as db_session
from app.services import concurrency


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
