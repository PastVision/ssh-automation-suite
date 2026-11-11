"""Filesystem storage for execution transcripts.

SQLite holds execution *metadata*; the byte stream of a run lives here:

    logs/executions/<execution_id>/manifest.json
    logs/executions/<execution_id>/<host_run_id>-<hostname>.log

Kept for the future executor milestone; the helpers already define the layout so
log paths recorded in the database are stable from day one.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from app.core.config import Settings, get_settings

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class LogPathError(ValueError):
    """Raised when a computed log path would fall outside the execution-log root."""


def sanitize_hostname(hostname: str) -> str:
    """Hostnames reach the filesystem, so strip separators and traversal."""
    cleaned = _UNSAFE.sub("_", hostname.strip())
    return cleaned.strip("._") or "host"


class ExecutionLogStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def _contained(self, path: Path) -> Path:
        """Assert ``path`` resolves inside the execution-log root.

        Sanitization already strips traversal, but this is the authoritative
        check: it operates on fully resolved paths, so symlinks and any future
        caller-supplied filename scheme are covered too.
        """
        root = self.settings.execution_log_dir.resolve()
        resolved = path.resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise LogPathError(f"Refusing log path outside {root}: {resolved}") from exc
        return path

    def execution_dir(self, execution_id: int) -> Path:
        return self._contained(self.settings.execution_log_dir / str(execution_id))

    def prepare_execution_dir(self, execution_id: int) -> Path:
        directory = self.execution_dir(execution_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def host_log_path(self, execution_id: int, host_run_id: int, hostname: str) -> Path:
        path = self.execution_dir(execution_id) / f"{host_run_id}-{sanitize_hostname(hostname)}.log"
        return self._contained(path)

    def relative(self, path: Path) -> str:
        """Path form persisted in SQLite (relative to the backend directory)."""
        return self.settings.relative_path(path)

    def write_manifest(self, execution_id: int, payload: dict) -> Path:
        directory = self.prepare_execution_dir(execution_id)
        path = directory / "manifest.json"
        body = {**payload, "written_at": datetime.now().astimezone().isoformat()}
        path.write_text(json.dumps(body, indent=2, default=str), encoding="utf-8")
        return path

    def append_host_log(
        self, execution_id: int, host_run_id: int, hostname: str, text: str
    ) -> Path:
        path = self.host_log_path(execution_id, host_run_id, hostname)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            if text and not text.endswith("\n"):
                handle.write("\n")
        return path

    def read_host_log(self, execution_id: int, host_run_id: int, hostname: str) -> str | None:
        path = self.host_log_path(execution_id, host_run_id, hostname)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8", errors="replace")

    def delete_execution_dir(self, execution_id: int) -> None:
        directory = self.execution_dir(execution_id)
        if not directory.exists():
            return
        for child in directory.iterdir():
            child.unlink(missing_ok=True)
        directory.rmdir()


def get_log_store() -> ExecutionLogStore:
    return ExecutionLogStore()
