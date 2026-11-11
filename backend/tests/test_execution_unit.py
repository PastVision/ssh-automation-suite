"""Unit tests for the execution engine's security and state logic.

These use no network. They assert the properties that a real SSH connection would
take too long to exercise repeatedly: host-key policy, private-key containment,
timeout/cancel bookkeeping, and the log layout.
"""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.models.enums import ExecutionStatus, HostRunStatus, StepRunStatus
from app.services import execution_state, ssh_transport
from app.services.execution_log import STDERR_CLOSE, STDERR_OPEN, HostTranscript
from app.services.execution_state import HostSpec, StepSpec

# --------------------------------------------------------------------------- #
# Host-key policy
# --------------------------------------------------------------------------- #

def test_host_key_verification_is_on_by_default(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default must never accept arbitrary host keys."""
    monkeypatch.delenv("SSHSUITE_SSH_ALLOW_UNKNOWN_HOST_KEYS", raising=False)
    resolved_settings = Settings(_env_file=None, environment="test")
    resolved = ssh_transport.build_known_hosts(resolved_settings)
    assert resolved is ssh_transport._USE_DEFAULT_KNOWN_HOSTS
    assert not resolved_settings.ssh_allow_unknown_host_keys


def test_known_hosts_file_is_used_when_configured(settings: Settings, tmp_path) -> None:
    known_hosts = tmp_path / "known_hosts"
    known_hosts.write_text("", encoding="utf-8")
    configured = Settings(_env_file=None, ssh_known_hosts_file=known_hosts, environment="test")
    assert ssh_transport.build_known_hosts(configured) == str(known_hosts)


def test_unknown_host_keys_can_only_be_allowed_outside_production(
    settings: Settings,
) -> None:
    allowed = Settings(_env_file=None, ssh_allow_unknown_host_keys=True, environment="test")
    assert ssh_transport.build_known_hosts(allowed) is ssh_transport._ACCEPT_ANY_HOST_KEY

    with pytest.raises(ValueError, match="production"):
        Settings(_env_file=None, ssh_allow_unknown_host_keys=True, environment="production")


def test_disabling_verification_is_never_the_default_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A regression guard: the sentinel must not be reachable without opt-in."""
    monkeypatch.delenv("SSHSUITE_SSH_ALLOW_UNKNOWN_HOST_KEYS", raising=False)
    default = Settings(_env_file=None, environment="test")
    assert default.ssh_allow_unknown_host_keys is False
    assert ssh_transport.build_known_hosts(default) is not ssh_transport._ACCEPT_ANY_HOST_KEY


# --------------------------------------------------------------------------- #
# Private key containment
# --------------------------------------------------------------------------- #

def test_key_errors_never_embed_private_key_material() -> None:
    """A malformed key must not have its PEM body echoed into an error message."""
    pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "SECRETMATERIALSHOULDNOTAPPEAR\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
    )
    exc = ValueError(f"could not parse {pem}")
    rendered = ssh_transport._describe(exc)
    assert "PRIVATE KEY" not in rendered
    assert "SECRETMATERIAL" not in rendered


def test_connect_error_messages_do_not_leak_key_text(settings: Settings) -> None:
    """SshConnectError text is stored in SQLite and logs, so it must be clean."""
    assert "PRIVATE KEY" not in str(ssh_transport.SshConnectError("boom"))


def test_host_spec_carries_no_key_material() -> None:
    spec = HostSpec(
        hostname="example", port=22, credential_id=1, credential_name="c", username="u"
    )
    serialised = repr(spec)
    assert "PRIVATE KEY" not in serialised
    assert "BEGIN" not in serialised


# --------------------------------------------------------------------------- #
# Execution state transitions
# --------------------------------------------------------------------------- #

def _credential_row(settings) -> int:
    """Insert a credential row (no key needed for state-only tests)."""
    from app.db.session import get_session_factory
    from app.models.credential import SshCredential

    session = get_session_factory()()
    try:
        from tests.lab_support import ensure_test_owner
        record = SshCredential(
            name="lab",
            username="tester",
            default_port=22,
            owner_id=ensure_test_owner(),
        )
        session.add(record)
        session.commit()
        return record.id
    finally:
        session.close()


def _execution(settings) -> tuple[int, int]:
    credential_id = _credential_row(settings)
    execution_id = execution_state.create_execution(
        workflow_name="unit",
        steps=[
            StepSpec("a", 1, "echo a", "bash", 10),
            StepSpec("b", 1, "echo b", "bash", 10),
        ],
        hosts=[
            HostSpec("127.0.0.1", 1, credential_id, "lab", username="tester"),
        ],
    )
    host_run_id = execution_state.load_host_specs(execution_id)[0][0]
    return execution_id, host_run_id


def test_execution_is_created_pending_with_snapshots(migrated_db, settings) -> None:
    execution_id, host_run_id = _execution(settings)
    execution = execution_state.get_execution(execution_id)

    assert execution.status is ExecutionStatus.PENDING
    assert execution.host_count == 1
    assert [s.script for s in execution.snapshots] == ["echo a", "echo b"]
    assert len(execution.host_runs[0].step_runs) == 2
    assert all(
        step.status is StepRunStatus.PENDING for step in execution.host_runs[0].step_runs
    )


def test_snapshot_freezes_script_text(migrated_db, settings) -> None:
    """Editing the source template later must not change a created execution."""
    credential_id = _credential_row(settings)
    execution_id = execution_state.create_execution(
        workflow_name="frozen",
        steps=[StepSpec("t", 3, "original-script", "bash", 10)],
        hosts=[HostSpec("127.0.0.1", 1, credential_id, "lab", username="tester")],
    )
    steps = execution_state.load_steps(execution_id)
    assert steps[0].script == "original-script"
    assert steps[0].template_version == 3


def test_finishing_a_host_skips_its_pending_steps(migrated_db, settings) -> None:
    execution_id, host_run_id = _execution(settings)
    execution_state.mark_host_running(host_run_id)
    first = execution_state.step_run_id(host_run_id, 0)
    execution_state.finish_step(
        first, status=StepRunStatus.FAILED, exit_code=9, duration_ms=12
    )

    execution_state.finish_host(
        host_run_id,
        status=HostRunStatus.FAILED,
        error_message="step 0 failed",
        failed_step_position=0,
    )

    execution = execution_state.get_execution(execution_id)
    steps = execution.host_runs[0].step_runs
    assert steps[0].status is StepRunStatus.FAILED
    assert steps[0].exit_code == 9
    assert steps[0].duration_ms == 12
    assert steps[1].status is StepRunStatus.SKIPPED, "fail-fast must skip the rest"


def test_finishing_a_host_can_mark_steps_cancelled(migrated_db, settings) -> None:
    execution_id, host_run_id = _execution(settings)
    execution_state.finish_host(
        host_run_id, status=HostRunStatus.FAILED, cancelled_steps=True
    )
    execution = execution_state.get_execution(execution_id)
    assert all(
        step.status is StepRunStatus.CANCELLED
        for step in execution.host_runs[0].step_runs
    )


def test_execution_counters_are_derived_from_host_rows(migrated_db, settings) -> None:
    execution_id, host_run_id = _execution(settings)
    execution_state.finish_host(host_run_id, status=HostRunStatus.SUCCEEDED)
    execution_state.finish_execution(execution_id, status=ExecutionStatus.SUCCEEDED)

    execution = execution_state.get_execution(execution_id)
    assert execution.succeeded_host_count == 1
    assert execution.failed_host_count == 0
    assert execution.finished_at is not None


def test_execution_cannot_start_twice(migrated_db, settings) -> None:
    execution_id, _ = _execution(settings)
    execution_state.mark_execution_running(execution_id)
    with pytest.raises(execution_state.InvalidExecutionState):
        execution_state.mark_execution_running(execution_id)


def test_missing_execution_raises(migrated_db, settings) -> None:
    with pytest.raises(execution_state.ExecutionNotFound):
        execution_state.load_steps(9999)


# --------------------------------------------------------------------------- #
# Transcript layout
# --------------------------------------------------------------------------- #

def test_transcript_keeps_streams_separate(settings) -> None:
    """stdout and stderr must be individually identifiable, never blended.

    Because the two remote streams are concurrent, the writer emits a stderr
    *block* (delimited by markers) whenever stderr output arrives, then returns to
    verbatim stdout. The guarantee is that every stderr byte is inside a marked
    block and no stdout byte ever is — not that the whole file is partitioned in
    two halves.
    """
    with HostTranscript(
        execution_id=1, host_run_id=2, hostname="h", settings=settings
    ) as transcript:
        transcript.step_start(0, "t", 30)
        transcript.write_stdout("out-1\n")
        transcript.write_stderr("err-1\n")
        transcript.write_stdout("out-2\n")

    text = transcript.path.read_text(encoding="utf-8")
    blocks = _stderr_blocks(text)

    assert blocks == ["err-1\n"], f"stderr blocks were {blocks!r}"
    outside = _strip_stderr_blocks(text)
    assert "out-1" in outside and "out-2" in outside
    assert "err-1" not in outside, "stderr must never appear outside a marked block"
    assert "out-1" not in blocks[0] and "out-2" not in blocks[0]


def _stderr_blocks(text: str) -> list[str]:
    blocks = []
    remainder = text
    while STDERR_OPEN in remainder:
        _, after_open = remainder.split(STDERR_OPEN, 1)
        body, remainder = after_open.split(STDERR_CLOSE, 1)
        blocks.append(body.lstrip("\n"))
    return blocks


def _strip_stderr_blocks(text: str) -> str:
    out = []
    remainder = text
    while STDERR_OPEN in remainder:
        before, after_open = remainder.split(STDERR_OPEN, 1)
        _, remainder = after_open.split(STDERR_CLOSE, 1)
        out.append(before)
    out.append(remainder)
    return "".join(out)


def test_transcript_preserves_missing_trailing_newline(settings) -> None:
    with HostTranscript(
        execution_id=3, host_run_id=4, hostname="h", settings=settings
    ) as transcript:
        transcript.write_stdout("no-newline")

    text = transcript.path.read_text(encoding="utf-8")
    assert "no-newline" in text
    assert "no-newline\n" not in text.split("[[engine]]")[0]


def test_transcript_does_not_translate_newlines(settings) -> None:
    """CRLF from the remote must be written verbatim, not normalised."""
    with HostTranscript(
        execution_id=5, host_run_id=6, hostname="h", settings=settings
    ) as transcript:
        transcript.write_stdout("line1\r\nline2\r\n")

    raw = transcript.path.read_bytes()
    assert b"line1\r\nline2\r\n" in raw


def test_transcript_path_is_inside_the_execution_directory(settings) -> None:
    with HostTranscript(
        execution_id=7, host_run_id=8, hostname="../../escape", settings=settings
    ) as transcript:
        transcript.write_stdout("x")

    assert transcript.path.parent == settings.execution_log_dir / "7"
    assert ".." not in transcript.path.name


# --------------------------------------------------------------------------- #
# Command timeout / cancellation bookkeeping
# --------------------------------------------------------------------------- #

def test_run_command_falls_back_to_no_stdin_when_omitted() -> None:
    """Guard the signature the runner relies on."""
    import inspect

    params = inspect.signature(ssh_transport.run_command).parameters
    assert params["stdin_data"].default is None
    assert params["timeout"].default is inspect.Parameter.empty


def test_cancel_token_starts_clear_and_sets_reason() -> None:
    from app.services.execution_runner import CancelToken

    token = CancelToken()
    assert not token.cancelled
    token.cancel("because")
    assert token.cancelled
    assert token.reason == "because"


def test_script_command_uses_the_declared_interpreter() -> None:
    from app.services.execution_runner import _script_command

    step = StepSpec("t", 1, "echo hi", "python3", 10)
    assert _script_command(step) == "python3 -s"


def test_script_command_defaults_to_bash() -> None:
    from app.services.execution_runner import _script_command

    assert _script_command(StepSpec("t", 1, "echo hi", "", 10)) == "bash -s"


# --------------------------------------------------------------------------- #
# API schema validation
# --------------------------------------------------------------------------- #

def test_interpreter_rejects_shell_injection() -> None:
    from app.schemas.execution import StepInput

    with pytest.raises(ValueError):
        StepInput(name="x", script="x", interpreter="bash; rm -rf /")

    assert StepInput(name="x", script="x", interpreter="bash").interpreter == "bash"


def test_execution_request_requires_hosts_and_steps() -> None:
    from pydantic import ValidationError

    from app.schemas.execution import ExecutionCreate

    with pytest.raises(ValidationError):
        ExecutionCreate(credential_id=1, hosts=[], steps=[])
