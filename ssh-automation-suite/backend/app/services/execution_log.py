"""Per-host transcript writer.

A host's log is one file, because the whole transcript for one host is what an
operator reads top to bottom. stdout and stderr stay separately identifiable
inside it: remote stdout is written verbatim, remote stderr is wrapped in
explicit markers. The two streams are genuinely concurrent, so no layout can
recover their true interleaving; markers at least guarantee they are never
silently blended, and that a reader can tell which bytes were which.

Local SSH-client diagnostics never reach this writer. Transport failures are
recorded as ``[[engine]]`` annotations by the runner, so a remote stderr line can
never be mistaken for a client-side complaint or vice versa.

The file is opened once per host and appended to as output arrives, so large
output streams to disk instead of accumulating in memory.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.core.config import Settings, get_settings
from app.services.log_store import ExecutionLogStore

STDERR_OPEN = "----- stderr -----"
STDERR_CLOSE = "----- end stderr -----"
TERMINATOR = "\n"


class HostTranscript:
    """Append-only transcript for one host workflow."""

    def __init__(
        self,
        *,
        execution_id: int,
        host_run_id: int,
        hostname: str,
        settings: Settings | None = None,
    ) -> None:
        self.execution_id = execution_id
        self.host_run_id = host_run_id
        self.hostname = hostname
        self._settings = settings or get_settings()
        self._store = ExecutionLogStore(self._settings)
        self._path = self._store.host_log_path(execution_id, host_run_id, hostname)
        self._handle = None
        self._in_stderr = False
        self._at_line_start = True

    @property
    def path(self) -> Path:
        return self._path

    @property
    def relative_path(self) -> str:
        return self._store.relative(self._path)

    def __enter__(self) -> HostTranscript:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # newline="" writes exactly the bytes received. Translation would rewrite
        # CRLF output coming from the remote and alter the transcript.
        self._handle = self._path.open("a", encoding="utf-8", errors="replace", newline="")
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        self._raw(
            f"===== host {self.hostname} (host_run {self.host_run_id}) started {stamp} =====\n"
        )
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self._close_stderr()
        finally:
            if self._handle is not None:
                self._handle.flush()
                self._handle.close()
                self._handle = None
        return False

    # -- internals --------------------------------------------------------- #

    def _raw(self, text: str) -> None:
        if self._handle is None:
            raise RuntimeError("HostTranscript used outside its context manager")
        if not text:
            return
        self._handle.write(text)
        self._at_line_start = text.endswith(TERMINATOR)

    def _start_line_if_needed(self) -> None:
        if not self._at_line_start:
            self._raw(TERMINATOR)

    def _header(self, text: str) -> None:
        self._start_line_if_needed()
        self._raw(f"===== {text} =====\n")

    def _close_stderr(self) -> None:
        if self._in_stderr:
            self._start_line_if_needed()
            self._raw(STDERR_CLOSE + "\n")
            self._in_stderr = False

    # -- public writers ---------------------------------------------------- #

    def note(self, text: str) -> None:
        """Record an engine-side event: connect failure, timeout, skip, reason."""
        self._close_stderr()
        self._start_line_if_needed()
        self._raw(f"[[engine]] {text}\n")

    def step_start(self, position: int, template_name: str, timeout_seconds: int) -> None:
        self._close_stderr()
        self._header(f"step {position} '{template_name}' start (timeout {timeout_seconds}s)")

    def step_end(
        self,
        position: int,
        *,
        status: str,
        exit_code: int | None,
        duration_ms: int | None,
    ) -> None:
        self._close_stderr()
        code = "unknown" if exit_code is None else str(exit_code)
        self._start_line_if_needed()
        self._raw(f"[[engine]] step {position} {status} exit={code} duration={duration_ms}ms\n")

    def write_stdout(self, chunk: str) -> None:
        self._close_stderr()
        self._raw(chunk)

    def write_stderr(self, chunk: str) -> None:
        if not self._in_stderr:
            self._start_line_if_needed()
            self._raw(STDERR_OPEN + "\n")
            self._in_stderr = True
            self._at_line_start = True
        self._raw(chunk)

    def footer(self, summary: str) -> None:
        self._close_stderr()
        self._header(f"host {self.hostname} finished: {summary}")


def read_host_transcript(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8", errors="replace")
