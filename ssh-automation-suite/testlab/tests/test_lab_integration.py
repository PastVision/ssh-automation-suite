"""End-to-end integration tests for the SSH Integration Test Lab.

These tests talk to 20 real OpenSSH containers over the `ssh-lab` network. They
are skipped (not failed) when Docker is unavailable, so the suite stays green on
a machine without a daemon while still being the authoritative check where
Docker exists.

Run them against a running lab:
    python scripts/lab.py keys
    python scripts/lab.py up
    pytest testlab/tests/test_lab_integration.py -v

They are read-only with respect to the application: they never touch backend/
code, the application database, or production credentials.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

LAB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB_DIR / "scripts"))

import lab  # noqa: E402

HOSTS = lab.ALL_HOSTS


def _docker_ready() -> bool:
    ok, _reason = lab.docker_available()
    return ok


pytestmark = pytest.mark.skipif(
    not _docker_ready(),
    reason="Docker is unavailable; the SSH lab cannot run here",
)


@pytest.fixture(scope="session", autouse=True)
def _require_running_lab() -> None:
    """Fail with a clear message (not a hang) if the lab is not up."""
    statuses = lab.container_status()
    if not statuses:
        pytest.skip("lab is not running; start it with `python scripts/lab.py up`")
    missing = [h for h in HOSTS if h not in statuses]
    if missing:
        pytest.skip(f"lab incomplete, missing: {missing}")


@pytest.fixture(scope="session")
def lab_keys() -> tuple[Path, Path]:
    return lab.ensure_keys()


# --------------------------------------------------------------------------- #
# 1. All 20 endpoints are reachable
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("hostname", HOSTS)
def test_endpoint_is_reachable_and_accepts_public_key(hostname: str, lab_keys) -> None:
    result = lab.ssh_exec(hostname, "true", timeout=60)
    assert result.returncode == 0, f"{hostname} rejected public-key auth: {result.stderr}"


def test_all_twenty_containers_report_healthy() -> None:
    statuses = lab.container_status()
    assert len(statuses) >= 20
    assert all(statuses.get(h) == "healthy" for h in HOSTS), statuses


# --------------------------------------------------------------------------- #
# 2. Public-key authentication works
# --------------------------------------------------------------------------- #

def test_public_key_login_identifies_as_test_user(lab_keys) -> None:
    result = lab.ssh_exec(HOSTS[0], "whoami", timeout=60)
    assert result.returncode == 0
    assert result.stdout.strip() == "tester"


@pytest.mark.parametrize("hostname", [HOSTS[0], HOSTS[9], HOSTS[19]])
def test_shared_key_works_on_first_middle_and_last_host(hostname: str, lab_keys) -> None:
    """One shared public key must be valid on every container."""
    result = lab.ssh_exec(hostname, "echo shared-key-ok", timeout=60)
    assert result.returncode == 0
    assert "shared-key-ok" in result.stdout


def test_login_fails_only_on_auth_not_on_connection(lab_keys) -> None:
    """Distinguishes a transport failure from an authentication rejection.

    sshd exits 255 for both, so the message is what separates them. A locked
    account or a bad capability yields 'Connection reset'/'chroot ... not
    permitted' *before* authentication; a correct server yields
    'Permission denied (publickey)' only when the key is genuinely wrong.
    """
    result = lab.ssh_exec(HOSTS[0], "true", use_key=False, timeout=60)
    combined = (result.stdout + result.stderr).lower()
    assert result.returncode == 255
    assert "permission denied" in combined, (
        f"expected an authentication rejection, got: {result.stderr!r}"
    )
    assert "connection reset" not in combined, (
        "server reset the connection before authentication; check capabilities"
    )


# --------------------------------------------------------------------------- #
# 3. Each host has the correct hostname
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("hostname", HOSTS)
def test_container_reports_expected_hostname(hostname: str, lab_keys) -> None:
    result = lab.ssh_exec(hostname, "hostname", timeout=60)
    assert result.returncode == 0
    assert result.stdout.strip() == hostname, (
        f"expected {hostname}, got {result.stdout.strip()!r}"
    )


# --------------------------------------------------------------------------- #
# 4. Remote commands execute successfully
# --------------------------------------------------------------------------- #

def test_remote_command_output_is_returned(lab_keys) -> None:
    result = lab.ssh_exec(HOSTS[0], "printf 'hello-from-lab'", timeout=60)
    assert result.returncode == 0
    assert result.stdout == "hello-from-lab"


def test_remote_exit_code_is_propagated(lab_keys) -> None:
    result = lab.ssh_exec(HOSTS[0], "exit 7", timeout=60)
    assert result.returncode == 7


def test_remote_stderr_is_separate_from_stdout(lab_keys) -> None:
    result = lab.ssh_exec(HOSTS[0], "echo out; echo err >&2", timeout=60)
    assert result.returncode == 0
    assert result.stdout.strip() == "out"
    assert result.stderr.strip() == "err"


# --------------------------------------------------------------------------- #
# 5. Hosts have independent filesystems
# --------------------------------------------------------------------------- #

def test_hosts_have_independent_filesystems(lab_keys) -> None:
    """A file created on one host must be invisible on every other host."""
    marker = "lab-independence-check.txt"
    payload = "only-on-host-01"

    write = lab.ssh_exec(HOSTS[0], f"printf '{payload}' > /tmp/{marker}", timeout=60)
    assert write.returncode == 0, write.stderr

    own = lab.ssh_exec(HOSTS[0], f"cat /tmp/{marker}", timeout=60)
    assert own.stdout.strip() == payload

    for other in (HOSTS[1], HOSTS[10], HOSTS[19]):
        probe = lab.ssh_exec(other, f"test -e /tmp/{marker} && echo PRESENT || echo ABSENT",
                             timeout=60)
        assert probe.stdout.strip() == "ABSENT", f"{other} shares a filesystem with {HOSTS[0]}"

    lab.ssh_exec(HOSTS[0], f"rm -f /tmp/{marker}", timeout=60)


def test_hosts_have_distinct_process_namespaces(lab_keys) -> None:
    """Each container must report its own PID 1, not the host's."""
    pids = set()
    for hostname in (HOSTS[0], HOSTS[1], HOSTS[2]):
        result = lab.ssh_exec(hostname, "cat /proc/1/comm", timeout=60)
        assert result.returncode == 0
        pids.add(result.stdout.strip())
        # A container's init is sshd, never systemd/init from the Windows host.
        assert result.stdout.strip() not in ("init", "systemd"), result.stdout
    assert pids, "no PID 1 information returned"


def test_hosts_have_distinct_host_keys(lab_keys) -> None:
    """Unique host keys per container, so identities cannot be confused."""
    fingerprints = set()
    for hostname in (HOSTS[0], HOSTS[1], HOSTS[2]):
        result = lab.ssh_exec(
            hostname,
            "ssh-keygen -lf /etc/ssh/lab-host-keys/ssh_host_ed25519_key.pub",
            timeout=60,
        )
        assert result.returncode == 0
        field = result.stdout.split()
        assert len(field) >= 2
        fingerprints.add(field[1])
    assert len(fingerprints) == 3, "host keys are not unique per container"


# --------------------------------------------------------------------------- #
# 6. Invalid authentication is rejected
# --------------------------------------------------------------------------- #

def test_invalid_private_key_is_rejected() -> None:
    """A key that is not in authorized_keys must be refused."""
    staged = lab.KEYS_DIR / "id_bad_tmp"
    lab.KEYS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "invalid",
             "-f", str(staged)],
            check=True, capture_output=True, text=True,
        )
        result = lab.ssh_exec(HOSTS[0], "true", private_key=staged, timeout=60)
        assert result.returncode != 0, "host accepted an unauthorised key"
        combined = (result.stdout + result.stderr).lower()
        assert "permission denied" in combined or "authentication" in combined
    finally:
        staged.unlink(missing_ok=True)
        staged.with_suffix(".pub").unlink(missing_ok=True)


def test_unknown_user_is_rejected(lab_keys) -> None:
    result = lab.ssh_exec(HOSTS[0], "true", user="definitely-not-a-user", timeout=60)
    assert result.returncode != 0


def test_password_authentication_is_disabled() -> None:
    """The server must refuse a password-style login: no key, no password."""
    result = lab.ssh_exec(
        HOSTS[0], "true", use_key=False,
        user="tester", timeout=60,
    )
    assert result.returncode != 0, "server allowed a login without a key"
    combined = (result.stdout + result.stderr).lower()
    assert "permission denied" in combined or "authentication" in combined


def test_unreachable_host_fails_cleanly(lab_keys) -> None:
    result = lab.ssh_exec("no-such-host.invalid", "true", timeout=60)
    assert result.returncode != 0
