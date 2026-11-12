"""The suite must never touch the developer's real runtime directories.

Written after a full run was found to create ``backend/data`` as a side effect of
importing the application: ``app.main`` builds a module-level app from default
settings, and ``migrations/env.py`` created the app's directories even when
Alembic had been pointed at an unrelated temporary database. Those paths bypass
every per-test fixture, so each one is asserted here directly.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from app.core.config import BACKEND_DIR, get_settings
from tests.conftest import _snapshot_real_runtime


def _real_roots() -> tuple[Path, Path]:
    return BACKEND_DIR / "data", BACKEND_DIR / "logs"


def test_settings_resolve_outside_the_real_runtime_tree() -> None:
    """Whatever a test resolves must not land in ``backend/data`` or ``backend/logs``.

    Checked against both trees rather than against a fixed temp prefix: the point
    is that the resolved location is not the developer's, however it is named.
    """
    settings = get_settings()

    for real in _real_roots():
        assert real not in settings.data_dir.parents
        assert real not in settings.log_dir.parents
        assert real not in settings.sqlite_path.parents
        assert real not in settings.execution_log_dir.parents
        assert real not in settings.private_key_dir.parents


def test_imported_app_uses_the_isolated_settings() -> None:
    """``app.main``'s module-level app is built at import, before any fixture.

    This is the exact path that created the real data directory, so assert on the
    app object that import produced rather than on a freshly built one.
    """
    from app.main import app

    app_settings = app.state.settings
    for real in _real_roots():
        assert real not in app_settings.data_dir.parents
        assert real not in app_settings.sqlite_path.parents


def test_creating_and_closing_the_app_writes_nothing_real(tmp_path) -> None:
    """Starting the app runs ``ensure_directories``; it must stay in temp."""
    from fastapi.testclient import TestClient

    from app.main import create_app

    before = _snapshot_real_runtime()
    with TestClient(create_app(get_settings())) as client:
        assert client.get(f"{get_settings().api_prefix}/health").status_code == 200
    assert _snapshot_real_runtime() == before


def test_alembic_to_an_explicit_url_leaves_real_dirs_alone(tmp_path) -> None:
    """Migrating a temporary database must not create the app's own directories.

    ``env.py`` only needs the app tree when it supplies the default URL; with an
    explicit one the target is elsewhere, and creating ``data/``/``logs/`` there
    made every migration-backed test write into the real checkout.
    """
    from tests import migration_support

    before = _snapshot_real_runtime()
    destination = tmp_path / "explicit.db"
    migration_support.upgrade_to(migration_support.sqlite_url(destination), "head")

    assert destination.exists()
    assert _snapshot_real_runtime() == before


def test_lab_settings_do_not_create_real_directories(monkeypatch, tmp_path) -> None:
    """The lab helper calls ``ensure_directories`` itself -- still must be temp."""
    monkeypatch.setenv("SSHSUITE_DATA_DIR", str(tmp_path / "labdata"))
    monkeypatch.setenv("SSHSUITE_LOG_DIR", str(tmp_path / "lablogs"))

    from tests import lab_support

    before = _snapshot_real_runtime()
    resolved = lab_support.configure_lab_settings(monkeypatch)

    assert resolved.data_dir == tmp_path / "labdata"
    assert _snapshot_real_runtime() == before


def test_runtime_diff_detects_created_modified_and_deleted() -> None:
    """The guard must notice every kind of change, not just new files.

    A guard that cannot fail is worse than none: if ``_runtime_diff`` returned
    nothing for a real change, the session fixture would silently pass while a
    test seeded the developer's database.
    """
    from tests.conftest import _runtime_diff

    before = {"/data/a.db": ("file", 10, 100), "/logs/b.log": ("file", 20, 200)}
    after = {
        "/data/a.db": ("file", 10, 100),   # unchanged
        "/logs/b.log": ("file", 25, 300),  # rewritten
        "/data/c.db": ("file", 30, 400),   # created
    }

    added, removed, modified = _runtime_diff(before, after)

    assert added == ["/data/c.db"]
    assert modified == ["/logs/b.log"]
    assert removed == []

    created, gone, changed = _runtime_diff(after, before)
    assert created == []
    assert gone == ["/data/c.db"]
    assert changed == ["/logs/b.log"]


def test_runtime_diff_detects_an_empty_new_directory() -> None:
    """An empty directory must be a finding, not silence.

    ``ensure_directories`` only calls ``mkdir``. A leak can therefore create
    ``data/keys`` or ``logs/executions`` with nothing inside them, which is why
    the snapshot tracks directories as well as files.
    """
    from tests.conftest import _runtime_diff

    before = {"/data": ("dir", 0, 100)}
    after = {"/data": ("dir", 0, 100), "/logs/executions": ("dir", 0, 200)}

    added, _, _ = _runtime_diff(before, after)
    assert added == ["/logs/executions"]


def test_runtime_diff_is_empty_for_an_untouched_tree() -> None:
    """An unchanged snapshot must produce no findings, or the guard cries wolf."""
    from tests.conftest import _runtime_diff

    same = {"/data/a.db": ("file", 10, 100)}
    assert _runtime_diff(same, dict(same)) == ([], [], [])


def test_import_time_pin_overrides_hostile_exported_values() -> None:
    """Point 4: an exported SSHSUITE_DATA_DIR must not survive into the suite.

    Run in a subprocess because the pin is applied once, when ``conftest`` is
    first imported; by the time a test body executes, re-importing would be a
    no-op. The child is given deliberately hostile values pointing at the real
    directories and must come back with the session temp ones.
    """
    import subprocess
    import sys

    hostile = {
        "SSHSUITE_DATA_DIR": str((BACKEND_DIR / "data").resolve()),
        "SSHSUITE_LOG_DIR": str((BACKEND_DIR / "logs").resolve()),
        "SSHSUITE_SECRET_KEY_FILE": str((BACKEND_DIR / "data" / "secret.key").resolve()),
    }
    env = {**os.environ, **hostile}
    code = (
        "import os, tests.conftest as c;"
        "print(os.environ['SSHSUITE_DATA_DIR']);"
        "print(os.environ['SSHSUITE_LOG_DIR']);"
        "print(c.get_settings().data_dir);"
        "print(c._SESSION_RUNTIME)"
    )

    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=BACKEND_DIR,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    pinned_data, pinned_logs, resolved_data, runtime = result.stdout.strip().splitlines()

    assert Path(pinned_data) != Path(hostile["SSHSUITE_DATA_DIR"])
    assert Path(pinned_logs) != Path(hostile["SSHSUITE_LOG_DIR"])
    assert Path(pinned_data).parent == Path(runtime)
    assert Path(resolved_data) == Path(pinned_data)


def test_baseline_was_taken_before_the_application_was_imported() -> None:
    """Point 1: nothing has drifted since conftest was imported.

    ``app.main`` is imported during collection, after ``conftest`` recorded its
    baseline. If that import had created the real tree, the baseline would differ
    from the live snapshot here. Equality therefore proves the baseline preceded
    the import *and* that the import created nothing.
    """
    from tests.conftest import _REAL_RUNTIME_BASELINE

    assert _REAL_RUNTIME_BASELINE == _snapshot_real_runtime()
    # Every recorded path must live under the checked-out backend directory, which
    # is what makes a comparison across a changed CWD meaningful.
    for path in _REAL_RUNTIME_BASELINE:
        assert Path(path).is_relative_to(BACKEND_DIR)


def test_guard_survives_a_chdir_and_a_settings_cache_clear(tmp_path, monkeypatch) -> None:
    """Point 3: neither a changed CWD nor a cold settings cache moves the guard.

    The snapshot is built from the ``BACKEND_DIR`` constant, so it must resolve to
    the same real paths regardless of the process working directory or of which
    ``Settings`` instance happens to be cached.
    """
    from tests.conftest import _REAL_RUNTIME_BASELINE, _snapshot_real_runtime

    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    get_settings()

    assert _snapshot_real_runtime() == _REAL_RUNTIME_BASELINE
    # And the app's own resolution really did move to temp, not to this tmp_path.
    assert get_settings().data_dir != tmp_path / "data"


def test_sqlite_connections_opened_by_tests_use_temp_files() -> None:
    """A real SQLite handle must be creatable without touching real state."""
    before = _snapshot_real_runtime()
    settings = get_settings()
    connection = sqlite3.connect(str(settings.sqlite_path))
    try:
        connection.execute("SELECT 1").fetchone()
    finally:
        connection.close()
    assert _snapshot_real_runtime() == before
