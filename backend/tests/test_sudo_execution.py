"""QA-005/QA-006: non-interactive sudo and the missing-password warning.

The sudo scanner's own matching rules are covered in ``test_sudo_scan.py``. This
module covers the part that decides *what gets executed*: which transport a step
uses, whether a password is delivered, and what an operator is told when one is
missing.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest

# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.models.enums import HostRunStatus, StepRunStatus
from app.models.execution import ExecutionStepRun
from app.services import execution_state, sudo
from app.services.execution_runner import ExecutionRunner, _shell_single_quote
from app.services.execution_state import HostSpec, StepSpec, create_execution


class _Transcript:
    """Minimal transcript double: records the operator-visible text."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.footers: list[str] = []

    def note(self, message: str) -> None:
        self.notes.append(message)

    def footer(self, message: str) -> None:
        self.footers.append(message)


class _Conn:
    """Stands in for an SSH connection; the probe answer is pinned, not dialled."""


class _Token:
    event = None
    cancelled = False


HOST = HostSpec(hostname="web-01", port=22, credential_id=42, credential_name="prod")
CREDENTIAL = 42


def _step(script: str, interpreter: str = "bash") -> StepSpec:
    return StepSpec(
        template_name="t",
        template_version=1,
        script=script,
        interpreter=interpreter,
        timeout_seconds=30,
    )


def _seed_execution(*, script: str) -> int:
    """Persist one PENDING execution with a single host, and return its id.

    A real credential row is required: reading host specs resolves
    ``credential_id`` from it, and the settle path is reached only after that
    resolution succeeds.
    """
    import asyncssh

    from app.db.session import get_session_factory
    from app.models.credential import SshCredential
    from app.services import key_store, sessions

    session = get_session_factory()()
    try:
        owner_id = sessions.create_user(
            f"qa-sudo-{uuid.uuid4().hex[:8]}", "qa-sudo-password-1"
        ).id
        record = SshCredential(
            name="qa-sudo",
            username="tester",
            default_port=22,
            owner_id=owner_id,
        )
        session.add(record)
        session.flush()
        key_store.store_private_key(
            record.id,
            asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode(),
            settings=get_settings(),
        )
        session.commit()
        credential_id = record.id
    finally:
        session.close()

    return create_execution(
        workflow_name="qa-sudo",
        steps=[_step(script)],
        hosts=[
            HostSpec(
                hostname="web-01",
                port=22,
                credential_id=credential_id,
                credential_name="qa-sudo",
            )
        ],
    )


def _transport(script, *, nopasswd: bool, password, interpreter: str = "bash",
               execution_id: int = 1, host_run_id: int = 1):
    """Run the real transport with the sudo probe answer and password pinned.

    Pinning the probe is the only substitution: the decision under test is what
    the runner does with "passwordless: yes/no" and "password: set/unset", so
    probing would only add an SSH round trip without changing the assertion.
    """
    runner = ExecutionRunner(settings=get_settings())
    runner._sudo_nopasswd[(execution_id, host_run_id)] = nopasswd
    runner._sudo_password_loaded.setdefault(execution_id, set()).add(CREDENTIAL)
    if password is not None:
        runner._sudo_passwords.setdefault(execution_id, {})[CREDENTIAL] = password
    transcript = _Transcript()

    async def call():
        return await runner._plain_transport(
            execution_id, host_run_id, _step(script, interpreter), 0, script,
            transcript, HOST, _Conn(), _Token(),
        )

    return asyncio.run(call()), transcript


# --------------------------------------------------------------------------- #
# Transport selection
# --------------------------------------------------------------------------- #


def test_script_without_sudo_is_unchanged() -> None:
    script = "echo hello\nls -l\n"
    result, _ = _transport(script, nopasswd=False, password=None)
    assert result == ("bash -s", script)


def test_passwordless_sudo_keeps_stdin_transport() -> None:
    """A NOPASSWD host must keep the original shape: no password needed."""
    result, _ = _transport("sudo systemctl restart nginx", nopasswd=True, password=None)
    assert result == ("bash -s", "sudo systemctl restart nginx")


def test_password_sudo_moves_program_off_stdin() -> None:
    """The program cannot stay on stdin when stdin is the password channel."""
    result, _ = _transport("sudo whoami", nopasswd=False, password="s3cret")
    assert result is not None
    command, stdin_data = result
    assert command.startswith("bash -c ")
    assert "sudo -S whoami" in command
    assert stdin_data.strip().splitlines() == ["s3cret"]


def test_password_never_appears_in_the_command() -> None:
    """The password goes on stdin only; transcripts must not be able to leak it."""
    result, _ = _transport("sudo whoami", nopasswd=False, password="hunter2-very-secret")
    assert result is not None
    command, stdin_data = result
    assert "hunter2-very-secret" not in command
    assert "hunter2-very-secret" in stdin_data


def test_one_password_line_per_invocation() -> None:
    """A host with sudo timestamps disabled reads a line per sudo call."""
    script = "sudo echo one\nsudo echo two\nsudo echo three\n"
    result, _ = _transport(script, nopasswd=False, password="s3cret")
    assert result is not None
    _, stdin_data = result
    assert stdin_data == "s3cret\n" * 3


def test_custom_interpreter_is_respected() -> None:
    result, _ = _transport("sudo whoami", nopasswd=False, password="s3cret",
                           interpreter="sh")
    assert result is not None
    assert result[0].startswith("sh -c ")


# --------------------------------------------------------------------------- #
# Missing password (QA-006)
# --------------------------------------------------------------------------- #


def test_missing_password_does_not_proceed(migrated_db) -> None:
    """Must not run into the predictable 'a terminal is required' failure."""
    result, _ = _transport("sudo whoami", nopasswd=False, password=None)
    assert result is None


def test_missing_password_warning_names_the_host_and_the_remedy(migrated_db) -> None:
    _, transcript = _transport("sudo whoami", nopasswd=False, password=None)
    assert len(transcript.notes) == 1
    warning = transcript.notes[0]
    assert "web-01" in warning
    assert "Sudo password not configured" in warning
    assert "passwordless" in warning.lower()


def test_missing_password_fails_the_host_instead_of_leaving_it_running(
    migrated_db,
) -> None:
    """The step must not be abandoned mid-run.

    The transport decides before any command is sent, so if it simply returns
    there is no later code path to settle the host: the row would stay RUNNING
    and the execution would never finish.
    """
    execution_id = _seed_execution(script="sudo whoami")
    host_run_id, _ = execution_state.load_host_specs(execution_id)[0]
    _transport(
        "sudo whoami", nopasswd=False, password=None,
        execution_id=execution_id, host_run_id=host_run_id,
    )
    assert execution_state.host_status(host_run_id) is HostRunStatus.FAILED

    # The step that could not be transported must read as failed, not skipped:
    # "skipped" would send an operator looking for the step that came before.
    session = get_session_factory()()
    try:
        step_statuses = session.scalars(
            select(ExecutionStepRun.status)
            .where(ExecutionStepRun.host_run_id == host_run_id)
            .order_by(ExecutionStepRun.position)
        ).all()
    finally:
        session.close()
    assert step_statuses == [StepRunStatus.FAILED]


def test_missing_password_is_not_reported_when_sudo_is_passwordless() -> None:
    """The warning must not fire for hosts that need no password at all."""
    _, transcript = _transport("sudo whoami", nopasswd=True, password=None)
    assert not any("not configured" in n for n in transcript.notes)


def test_non_sudo_script_never_loads_a_password() -> None:
    """A run that never touches sudo must not decrypt a secret."""
    runner = ExecutionRunner(settings=get_settings())
    transcript = _Transcript()

    async def call():
        return await runner._plain_transport(
            1, 1, _step("echo hi"), 0, "echo hi", transcript, HOST, _Conn(), _Token()
        )

    assert asyncio.run(call()) == ("bash -s", "echo hi")
    assert runner._sudo_passwords == {}


# --------------------------------------------------------------------------- #
# Quoting and semantics
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "script",
    [
        "sudo echo 'single $x'",
        'sudo echo "double $x"',
        "sudo printf '%s\\n' a b",
        "cd /tmp && sudo mkdir -p x",
        "for f in a b; do sudo chown root $f; done",
        "sudo echo one | sudo tee /tmp/x",
    ],
)
def test_rewrite_preserves_structure(script: str) -> None:
    rewritten = sudo.rewrite(script)
    assert "-S" in rewritten
    # Idempotence: a second pass must not add a second flag.
    assert sudo.rewrite(rewritten) == rewritten


def test_single_quote_survives_embedded_quotes() -> None:
    assert _shell_single_quote("it's") == "'it'\\''s'"


def test_quoted_script_round_trips_through_a_real_shell(tmp_path: Path) -> None:
    """The quoted command line must reach the shell byte-for-byte."""
    payload = "echo \"it's a $(test)\"\n"
    command = f"printf %s {_shell_single_quote(payload)}"
    shell = _posix_shell()
    if shell is None:
        pytest.skip("no POSIX shell available to execute the command line")
    proc = _run(shell, command, tmp_path=tmp_path)
    assert proc.returncode == 0
    assert proc.stdout == payload


# --------------------------------------------------------------------------- #
# stdin conflict
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "script,expected",
    [
        ("read -r line\necho \"$line\"\n", "read"),
        ("cat\n", "cat"),
        ("wc -l\n", "wc"),
    ],
)
def test_stdin_consumers_detected(script: str, expected: str) -> None:
    assert sudo.stdin_consumer(script) == expected


@pytest.mark.parametrize(
    "script",
    [
        "echo 'read me'\n",                       # quoted text is not a command
        "cat <<'EOF'\nread x\nEOF\n",             # heredoc body is not code
        "wc -l /var/log/syslog\n",                # file operand, not stdin
        "grep -c foo /etc/passwd\n",
        "echo hi | cat\n",                        # pipeline stage, input from pipe
        "sort < /etc/hosts\n",                    # redirected input
        "sudo cat /etc/motd\n",                   # the sudo'd command reads a file
    ],
)
def test_documented_non_conflicts(script: str) -> None:
    """A false positive here would refuse a template that works fine."""
    assert sudo.stdin_consumer(script) is None


def test_stdin_conflict_is_reported_not_silently_wrong(migrated_db) -> None:
    script = "sudo ls\nwhile read -r l; do echo \"$l\"; done\n"
    result, transcript = _transport(script, nopasswd=False, password="s3cret")
    assert result is None
    assert any("standard input" in n for n in transcript.notes)


def test_stdin_conflict_does_not_fire_when_sudo_is_passwordless() -> None:
    """Passwordless sudo leaves stdin free, so reading it is fine."""
    script = "sudo ls\nwhile read -r l; do echo \"$l\"; done\n"
    result, _ = _transport(script, nopasswd=True, password=None)
    assert result == ("bash -s", script)


# --------------------------------------------------------------------------- #
# End-to-end in a real shell
# --------------------------------------------------------------------------- #

_FAKE_SUDO = r"""#!/bin/bash
nopasswd=${FAKE_SUDO_NOPASSWD:-0}
password=${FAKE_SUDO_PASSWORD:-s3cret}
stdin_flag=0
saw_noninteractive=0
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    -S|--stdin) stdin_flag=1; shift ;;
    -n|--non-interactive) saw_noninteractive=1; shift ;;
    --) shift; args+=("$@"); break ;;
    -u|--user|-g|--group|-p|--prompt|-D|--chdir|-C|--close-from)
      shift 2 ;;
    --user=*|--group=*|--prompt=*|--chdir=*) shift ;;
    -*) shift ;;
    *) args+=("$@"); break ;;
  esac
done
if [ "$nopasswd" = "1" ]; then exec "${args[@]}"; fi
if [ "$saw_noninteractive" = "1" ]; then
  echo "sudo: a password is required" >&2; exit 1
fi
if [ "$stdin_flag" != "1" ]; then
  echo "sudo: a terminal is required to read the password" >&2
  echo "sudo: a password is required" >&2; exit 1
fi
IFS= read -r supplied
if [ "$supplied" != "$password" ]; then
  echo "sudo: incorrect password" >&2; exit 1
fi
exec "${args[@]}"
"""


def _posix_shell() -> str | None:
    """An interrogable POSIX shell, or None.

    On POSIX hosts this is ``bash``/``sh`` directly. On Windows it is ``wsl.exe``,
    which is how this repository's own development machine reaches a real shell;
    a ``bash.exe`` stub without a distro is not enough, so it is probed.
    """
    if os.name != "nt":
        return shutil.which("bash") or shutil.which("sh")
    wsl = shutil.which("wsl")
    if wsl is None:
        return None
    try:
        probe = subprocess.run(
            [wsl, "-d", "Ubuntu", "--", "bash", "-c", "echo ok"],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return wsl if probe.stdout.strip() == "ok" else None


def _to_shell_path(path: Path) -> str:
    """Translate a host path into the form the shell under test understands."""
    if os.name != "nt":
        return str(path)
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    rest = str(resolved)[len(resolved.drive):].replace("\\", "/")
    return f"/mnt/{drive}{rest}"


def _run(shell: str, command: str, stdin_data: str = "", env_extra=None,
         tmp_path: Path | None = None):
    """Run ``command`` under a real shell, with ``stdin_data`` as its stdin.

    The command goes into a file rather than onto the command line. ``wsl.exe``
    re-parses its arguments, so a nested ``bash -c '...'`` containing quotes and
    ``$(`` is not delivered intact -- and a harness that corrupts the input would
    "prove" a quoting bug that does not exist. A file has no such layer.
    """
    env = dict(os.environ)
    env.update(env_extra or {})
    if os.name != "nt":
        return subprocess.run(
            [shell, "-c", command],
            input=(stdin_data or "").encode(),
            capture_output=True, env=env, timeout=120,
        )

    # wsl.exe does not forward the Windows environment, so the case's variables
    # are exported by the script itself. Without this the stand-in sudo would see
    # an empty FAKE_SUDO_PASSWORD and every case would fail for the wrong reason.
    exports = "".join(
        f"export {key}={_shell_single_quote(str(value))}\n"
        for key, value in (env_extra or {}).items()
    )
    script = (tmp_path or Path(tempfile.gettempdir())) / "runner.sh"
    script.write_text(exports + command + "\n", encoding="utf-8", newline="\n")
    proc = subprocess.run(
        [shell, "-d", "Ubuntu", "--", "bash", _to_shell_path(script)],
        input=(stdin_data or "").encode(),
        capture_output=True, env=env, timeout=120,
    )
    return subprocess.CompletedProcess(
        [shell, "bash", command],
        proc.returncode,
        proc.stdout.decode("utf-8", "replace"),
        proc.stderr.decode("utf-8", "replace"),
    )


def _fake_sudo_path(tmp_path: Path) -> str:
    """Write a ``sudo`` stand-in and return the shell-side path to its directory.

    Installing it beside the test rather than on PATH keeps the assertion honest:
    the transport must work with an ordinary ``sudo`` resolved by name, and the
    stand-in only reproduces the option/stdin behaviour that matters.
    """
    binary = tmp_path / "bin"
    binary.mkdir()
    fake = binary / "sudo"
    fake.write_text(_FAKE_SUDO, encoding="utf-8", newline="\n")
    fake.chmod(0o755)
    return _to_shell_path(binary)


def _with_sudo_dir(command: str, sudo_dir: str) -> str:
    """Prepend the stand-in's directory to PATH in the same shell that runs it."""
    return f"export PATH={_shell_single_quote(sudo_dir)}:\"$PATH\"; {command}"


@pytest.mark.skipif(
    _posix_shell() is None, reason="no POSIX shell available to execute the transport"
)
def test_password_transport_executes_in_a_real_shell(tmp_path: Path) -> None:
    """The generated command line must actually authenticate and run."""
    result, _ = _transport(
        "sudo echo ran-with-password", nopasswd=False, password="s3cret"
    )
    assert result is not None
    command, stdin_data = result
    sudo_dir = _fake_sudo_path(tmp_path)
    proc = _run(
        _posix_shell(),
        _with_sudo_dir(command, sudo_dir),
        stdin_data,
        {"FAKE_SUDO_PASSWORD": "s3cret"},
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ran-with-password" in proc.stdout


@pytest.mark.skipif(
    _posix_shell() is None, reason="no POSIX shell available to execute the transport"
)
def test_passwordless_transport_executes_without_a_password(tmp_path: Path) -> None:
    """A NOPASSWD host keeps ``bash -s`` and needs nothing on stdin."""
    result, _ = _transport(
        "sudo echo ran-without-password", nopasswd=True, password=None
    )
    assert result is not None
    command, stdin_data = result
    assert command == "bash -s"
    sudo_dir = _fake_sudo_path(tmp_path)
    proc = _run(
        _posix_shell(),
        _with_sudo_dir(command, sudo_dir),
        stdin_data or "",
        {"FAKE_SUDO_NOPASSWD": "1"},
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ran-without-password" in proc.stdout


@pytest.mark.skipif(
    _posix_shell() is None, reason="no POSIX shell available to execute the transport"
)
def test_wrong_password_fails_rather_than_hanging(tmp_path: Path) -> None:
    result, _ = _transport("sudo echo nope", nopasswd=False, password="s3cret")
    assert result is not None
    command, stdin_data = result
    sudo_dir = _fake_sudo_path(tmp_path)
    proc = _run(
        _posix_shell(),
        _with_sudo_dir(command, sudo_dir),
        stdin_data,
        {"FAKE_SUDO_PASSWORD": "other"},
        tmp_path,
    )
    assert proc.returncode != 0
    assert "incorrect password" in proc.stderr


@pytest.mark.skipif(
    _posix_shell() is None, reason="no POSIX shell available to execute the transport"
)
def test_multiple_sudo_calls_each_receive_a_password(tmp_path: Path) -> None:
    """Every invocation must get a line, or a timestamps-disabled host fails."""
    script = "sudo echo one\nsudo echo two\nsudo echo three\n"
    result, _ = _transport(script, nopasswd=False, password="s3cret")
    assert result is not None
    command, stdin_data = result
    sudo_dir = _fake_sudo_path(tmp_path)
    proc = _run(
        _posix_shell(),
        _with_sudo_dir(command, sudo_dir),
        stdin_data,
        {"FAKE_SUDO_PASSWORD": "s3cret"},
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["one", "two", "three"]


@pytest.mark.skipif(
    _posix_shell() is None, reason="no POSIX shell available to execute the transport"
)
def test_quoting_and_pipelines_survive_execution(tmp_path: Path) -> None:
    """Quoting, expansion and pipelines must mean what they meant unrewritten."""
    script = (
        "x='a b;$(c)'\n"
        'echo "quoted: $x"\n'
        "sudo echo 'inner $x' | tr a-z A-Z\n"
    )
    result, _ = _transport(script, nopasswd=False, password="s3cret")
    assert result is not None
    command, stdin_data = result
    sudo_dir = _fake_sudo_path(tmp_path)
    proc = _run(
        _posix_shell(),
        _with_sudo_dir(command, sudo_dir),
        stdin_data,
        {"FAKE_SUDO_PASSWORD": "s3cret"},
        tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert "quoted: a b;$(c)" in proc.stdout
    assert "INNER $X" in proc.stdout




