"""Execution logs live on the filesystem; only paths are persisted."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.log_store import ExecutionLogStore, LogPathError, sanitize_hostname


def test_hostname_is_sanitized() -> None:
    assert sanitize_hostname("web-1.example.com") == "web-1.example.com"
    # Path separators and traversal segments never survive.
    escaped = sanitize_hostname("../../etc/passwd")
    assert "/" not in escaped and "\\" not in escaped and ".." not in escaped
    assert escaped == "etc_passwd"
    assert sanitize_hostname("a/b\\c") == "a_b_c"
    assert sanitize_hostname("   ") == "host"


def test_log_paths_stay_inside_execution_dir(settings) -> None:
    store = ExecutionLogStore(settings)
    path = store.host_log_path(1, 2, "../../escape")

    assert path.parent == store.execution_dir(1)
    assert ".." not in path.name


def test_write_and_read_host_log(settings) -> None:
    store = ExecutionLogStore(settings)
    path = store.append_host_log(1, 5, "db-1", "line one")
    store.append_host_log(1, 5, "db-1", "line two")

    assert path.read_text(encoding="utf-8") == "line one\nline two\n"
    assert store.read_host_log(1, 5, "db-1") == "line one\nline two\n"
    assert store.read_host_log(1, 99, "missing") is None


def test_relative_path_inside_backend(settings) -> None:
    from app.core.config import BACKEND_DIR

    store = ExecutionLogStore(settings)
    relative = store.relative(BACKEND_DIR / "logs" / "executions" / "3" / "4-h.log")

    assert relative == "logs/executions/3/4-h.log"
    assert not Path(relative).is_absolute()
    assert "\\" not in relative


def test_relative_path_outside_backend_stays_absolute(settings) -> None:
    """Test data dirs live outside the repo; those paths must not be mangled."""
    store = ExecutionLogStore(settings)
    relative = store.relative(store.host_log_path(3, 4, "h"))

    assert Path(relative).is_absolute()


def test_manifest_is_json(settings) -> None:
    import json

    store = ExecutionLogStore(settings)
    path = store.write_manifest(7, {"workflow": "nightly"})

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["workflow"] == "nightly"
    assert "written_at" in payload


def test_delete_execution_dir(settings) -> None:
    store = ExecutionLogStore(settings)
    store.append_host_log(9, 1, "h", "x")
    store.delete_execution_dir(9)

    assert not store.execution_dir(9).exists()


def test_all_log_paths_resolve_inside_execution_log_dir(settings) -> None:
    """Defense in depth: containment is asserted on resolved paths."""
    store = ExecutionLogStore(settings)
    root = settings.execution_log_dir.resolve()

    hostile = [
        "../../escape",
        "..\\..\\windows",
        "/etc/passwd",
        "C:\\Windows\\System32\\drivers\\etc\\hosts",
        "./../..",
        "%2e%2e/%2e%2e/etc",
        "nul\x00byte",
        "a/../../b",
        "",
    ]
    for hostname in hostile:
        log_path = store.host_log_path(1, 2, hostname)
        assert log_path.resolve().is_relative_to(root), hostname
        assert store.execution_dir(1).resolve().is_relative_to(root)


def test_containment_guard_rejects_escaping_path(settings, monkeypatch) -> None:
    """The guard must actually fire, not merely pass for sanitized input."""
    store = ExecutionLogStore(settings)
    outside = settings.log_dir.parent / "elsewhere"

    with pytest.raises(LogPathError):
        store._contained(outside)

    # A path that resolves out of the root via a traversal segment is rejected too.
    with pytest.raises(LogPathError):
        store._contained(settings.execution_log_dir / ".." / ".." / "escaped.log")


def test_symlinked_host_log_outside_root_is_rejected(settings) -> None:
    """A symlink planted inside the log dir must not become an escape hatch."""
    store = ExecutionLogStore(settings)
    directory = store.prepare_execution_dir(5)
    target = settings.log_dir.parent / "outside-target"
    target.mkdir(parents=True, exist_ok=True)
    link = directory / "link.log"

    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):  # pragma: no cover - Windows without privilege
        pytest.skip("symlinks unavailable in this environment")

    with pytest.raises(LogPathError):
        store._contained(link)
