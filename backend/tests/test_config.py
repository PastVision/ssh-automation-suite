"""Configuration is env-overridable and paths are absolute and separate."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings, get_settings


def test_env_overrides_are_applied(settings) -> None:
    assert settings.environment == "test"
    assert settings.data_dir.is_absolute()
    assert settings.log_dir.is_absolute()
    assert settings.data_dir != settings.log_dir


def test_database_url_derived_from_data_dir(settings) -> None:
    assert settings.database_url in (None, "")
    assert settings.resolved_database_url == f"sqlite:///{settings.sqlite_path.as_posix()}"


def test_execution_log_dir_defaults_to_logs_executions(settings) -> None:
    assert settings.execution_log_dir == settings.log_dir / "executions"


def test_ensure_directories_creates_tree(settings) -> None:
    for directory in (
        settings.data_dir,
        settings.log_dir,
        settings.execution_log_dir,
        settings.private_key_dir,
    ):
        assert directory.is_dir()


def test_cors_origins_parse_from_comma_separated(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SSHSUITE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("SSHSUITE_LOG_DIR", str(tmp_path / "l"))
    monkeypatch.setenv("SSHSUITE_CORS_ORIGINS", "http://a.test, http://b.test")
    get_settings.cache_clear()

    assert get_settings().cors_origins == ["http://a.test", "http://b.test"]
    get_settings.cache_clear()


def test_api_prefix_is_normalised(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SSHSUITE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("SSHSUITE_LOG_DIR", str(tmp_path / "l"))
    monkeypatch.setenv("SSHSUITE_API_PREFIX", "api/v1/")
    get_settings.cache_clear()

    assert get_settings().api_prefix == "/api/v1"
    get_settings.cache_clear()


def test_concurrency_limit_out_of_range_rejected(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("SSHSUITE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("SSHSUITE_LOG_DIR", str(tmp_path / "l"))
    monkeypatch.setenv("SSHSUITE_MAX_CONCURRENT_HOST_WORKFLOWS", "0")
    get_settings.cache_clear()

    with pytest.raises(ValueError):
        Settings()
    get_settings.cache_clear()


def test_relative_path_helper(settings) -> None:
    from app.core.config import BACKEND_DIR

    relative = settings.relative_path(BACKEND_DIR / "logs" / "executions" / "1" / "a.log")

    assert relative == "logs/executions/1/a.log"
    assert Path(relative).is_absolute() is False


def test_relative_secret_key_file_is_anchored_to_backend_dir(
    monkeypatch, tmp_path, tmp_path_factory
) -> None:
    """A relative key path must not depend on the process working directory.

    If it did, launching from a different directory would generate a fresh key and
    silently make every stored credential undecryptable.
    """
    from app.core.config import BACKEND_DIR

    monkeypatch.setenv("SSHSUITE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("SSHSUITE_LOG_DIR", str(tmp_path / "l"))
    monkeypatch.setenv("SSHSUITE_SECRET_KEY_FILE", "nested/secret.key")

    elsewhere = tmp_path_factory.mktemp("cwd")
    monkeypatch.chdir(elsewhere)
    get_settings.cache_clear()

    resolved = get_settings().resolved_secret_key_file

    assert resolved.is_absolute()
    assert resolved == BACKEND_DIR / "nested" / "secret.key"
    assert elsewhere not in resolved.parents

    get_settings.cache_clear()


def test_relative_data_and_log_dirs_are_anchored_to_backend_dir(
    monkeypatch, tmp_path_factory
) -> None:
    """Relative storage paths must not depend on the process working directory.

    If they did, a launch from the repository root would place the database and
    execution logs beside it instead of under ``backend/``, splitting runtime
    state in two and putting the logs outside the ignored ``backend/`` tree.
    """
    from app.core.config import BACKEND_DIR

    monkeypatch.setenv("SSHSUITE_DATA_DIR", "data")
    monkeypatch.setenv("SSHSUITE_LOG_DIR", "logs")
    # Drop any pinned key file so this exercises the "unset -> data_dir/secret.key"
    # derivation rather than an override that would mask it.
    monkeypatch.delenv("SSHSUITE_SECRET_KEY_FILE", raising=False)

    elsewhere = tmp_path_factory.mktemp("cwd")
    monkeypatch.chdir(elsewhere)
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.data_dir == BACKEND_DIR / "data"
    assert settings.log_dir == BACKEND_DIR / "logs"
    assert settings.execution_log_dir == BACKEND_DIR / "logs" / "executions"
    assert settings.resolved_secret_key_file == BACKEND_DIR / "data" / "secret.key"
    assert elsewhere not in settings.data_dir.parents
    assert elsewhere not in settings.log_dir.parents
    get_settings.cache_clear()


def test_absolute_data_dir_override_is_preserved(monkeypatch, tmp_path) -> None:
    """An explicitly absolute path must be used verbatim, never re-anchored."""
    absolute = tmp_path / "elsewhere" / "d"

    monkeypatch.setenv("SSHSUITE_DATA_DIR", str(absolute))
    get_settings.cache_clear()

    assert get_settings().data_dir == absolute.resolve()
    get_settings.cache_clear()


def test_absolute_secret_key_file_is_left_alone(monkeypatch, tmp_path) -> None:
    explicit = tmp_path / "custom" / "keyfile"
    monkeypatch.setenv("SSHSUITE_DATA_DIR", str(tmp_path / "d"))
    monkeypatch.setenv("SSHSUITE_LOG_DIR", str(tmp_path / "l"))
    monkeypatch.setenv("SSHSUITE_SECRET_KEY_FILE", str(explicit))
    get_settings.cache_clear()

    assert get_settings().resolved_secret_key_file == explicit.resolve()

    get_settings.cache_clear()
