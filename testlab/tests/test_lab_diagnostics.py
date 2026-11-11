"""Regression tests for the diagnostic reporting of `ssh_exec` probes.

The lab's transport (`docker run`) is deliberately clean, but a future change to
`docker compose run` would put Compose lifecycle chatter on the captured stderr
channel. These tests pin the reporting contract so that:

* a successful probe reports "ok" and keeps both streams,
* a failed probe keeps the *complete* streams and summarises the actionable line,
* Compose lifecycle messages are never presented as an SSH/auth error.

No Docker daemon is required: `ssh_exec` is replaced with a fake result.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

LAB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB_DIR / "scripts"))

import lab  # noqa: E402

COMPOSE_CHATTER = (
    " Container ssh-lab-lab-client-run-e6f243e368a0 Creating \n"
    " Container ssh-lab-lab-client-run-e6f243e368a0 Created\n"
)


def _completed(returncode: int, stdout: str = "", stderr: str = ""):
    return subprocess.CompletedProcess(
        args=["docker", "run"], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture()
def fake_ssh(monkeypatch):
    """Replace ssh_exec with a stub returning a caller-supplied result."""

    def install(result):
        monkeypatch.setattr(lab, "ssh_exec", lambda *a, **k: result)

    return install


# --------------------------------------------------------------------------- #
# Successful probe
# --------------------------------------------------------------------------- #

def test_successful_probe_reports_ok_and_keeps_streams(fake_ssh) -> None:
    fake_ssh(_completed(0, stdout="", stderr=""))

    result = lab._probe("ssh-host-01")

    assert result.ok is True
    assert result.exit_code == 0
    assert result.detail == "ok"
    assert result.stdout == ""
    assert result.stderr == ""


def test_successful_probe_preserves_remote_output(fake_ssh) -> None:
    """`true` normally prints nothing, but nothing may be silently dropped."""
    fake_ssh(_completed(0, stdout="remote-stdout\n", stderr="remote-stderr\n"))

    result = lab._probe("ssh-host-01")

    assert result.ok is True
    assert result.stdout == "remote-stdout\n"
    assert result.stderr == "remote-stderr\n"


# --------------------------------------------------------------------------- #
# Failed probe
# --------------------------------------------------------------------------- #

def test_failed_probe_preserves_complete_diagnostics(fake_ssh) -> None:
    """Long stderr must be stored in full, not clipped to a preview."""
    long_line = "Permission denied (publickey)." + " " + "x" * 500
    stderr = f"debug: connecting to ssh-host-01\n{long_line}\nlast-line\n"
    fake_ssh(_completed(255, stdout="partial-out\n", stderr=stderr))

    result = lab._probe("ssh-host-01")

    assert result.ok is False
    assert result.exit_code == 255
    assert result.stderr == stderr, "full stderr must be preserved"
    assert len(result.stderr) > 200, "regression: stderr was truncated"
    assert result.stdout == "partial-out\n"


def test_failed_probe_detail_is_the_actionable_first_line(fake_ssh) -> None:
    fake_ssh(_completed(255, stderr="Warning: Permanently added\nPermission denied (publickey).\n"))

    result = lab._probe("ssh-host-01")

    assert result.detail == "Warning: Permanently added"
    assert result.detail != "ok"


def test_failed_probe_falls_back_to_stdout_when_stderr_is_empty(fake_ssh) -> None:
    """An entrypoint can exit non-zero with the reason on stdout only."""
    fake_ssh(_completed(255, stdout="lab-ssh-client: key not found: /etc/lab/keys/id_lab\n"))

    result = lab._probe("ssh-host-01")

    assert result.ok is False
    assert result.detail == "lab-ssh-client: key not found: /etc/lab/keys/id_lab"


def test_failed_probe_without_output_gives_actionable_hint(fake_ssh) -> None:
    fake_ssh(_completed(255))

    result = lab._probe("ssh-host-01")

    assert result.ok is False
    assert "exit 255" in result.detail
    assert "health" in result.detail, "operator needs a next step"


# --------------------------------------------------------------------------- #
# Compose lifecycle noise must never look like an auth failure
# --------------------------------------------------------------------------- #

def test_compose_noise_is_not_reported_as_ssh_error(fake_ssh) -> None:
    fake_ssh(_completed(255, stderr=COMPOSE_CHATTER + "Permission denied (publickey).\n"))

    result = lab._probe("ssh-host-01")

    assert result.detail == "Permission denied (publickey)."
    assert "Container" not in result.detail
    assert "ssh-lab-lab-client-run" not in result.detail


def test_compose_noise_alone_does_not_become_the_summary(fake_ssh) -> None:
    """Compose chatter with no real error must not be presented as one."""
    fake_ssh(_completed(255, stderr=COMPOSE_CHATTER))

    result = lab._probe("ssh-host-01")

    assert "Container" not in result.detail
    assert "Creating" not in result.detail
    assert "exit 255" in result.detail


def test_compose_noise_is_stripped_but_full_stream_still_stored(fake_ssh) -> None:
    fake_ssh(_completed(255, stderr=COMPOSE_CHATTER + "Connection refused\n"))

    result = lab._probe("ssh-host-01")

    assert "Container" in result.stderr, "raw stderr is preserved verbatim"
    assert result.detail == "Connection refused"


@pytest.mark.parametrize(
    "line",
    [
        " Container ssh-lab-lab-client-run-abc Creating ",
        " Container ssh-lab-lab-client-run-abc Created",
        " Network ssh-lab-net Creating",
        " Volume ssh-lab-data Removed",
        ' time="2026-11-11T12:00:00Z" level=warning msg="no such service"',
    ],
)
def test_compose_noise_patterns_are_filtered(line: str) -> None:
    assert lab._strip_compose_noise(line) == ""


@pytest.mark.parametrize(
    "line",
    [
        "Permission denied (publickey).",
        "ssh: connect to host ssh-host-01 port 22: Connection refused",
        "lab-ssh-client: key not found: /etc/lab/keys/id_lab",
        "tester@ssh-host-01: Permission denied (publickey,password).",
    ],
)
def test_real_errors_survive_noise_filtering(line: str) -> None:
    assert lab._strip_compose_noise(line) == line


# --------------------------------------------------------------------------- #
# Transport must stay unchanged
# --------------------------------------------------------------------------- #

def test_probe_still_uses_ssh_exec_which_uses_docker_run() -> None:
    text = (LAB_DIR / "scripts" / "lab.py").read_text(encoding="utf-8")
    body = text[text.find("def ssh_exec("):text.find("\n@dataclass", text.find("def ssh_exec("))]
    assert '"docker", "run"' in body
    assert "compose_client" not in body
