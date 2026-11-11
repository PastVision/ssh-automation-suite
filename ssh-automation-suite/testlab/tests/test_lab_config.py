"""Static (Docker-free) validation of the SSH Integration Test Lab.

These tests assert the properties of the lab *artifacts* — Dockerfile, compose
file, sshd config, entrypoint — and require no Docker daemon, so they run on any
machine and in CI. They are the guardrail for the requirements that are easy to
regress silently: 20 hosts, publickey-only auth, a dedicated network, and no
SSH port exposed to the host.

The real end-to-end behaviour is covered by tests/test_lab_integration.py,
which skips cleanly when Docker is absent.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

LAB_DIR = Path(__file__).resolve().parent.parent
COMPOSE = LAB_DIR / "docker-compose.yml"
LOOPBACK = LAB_DIR / "compose.loopback.yml"
DOCKERFILE = LAB_DIR / "Dockerfile"
SSHD_CONFIG = LAB_DIR / "sshd_config.lab"
ENTRYPOINT = LAB_DIR / "entrypoint.sh"

EXPECTED_HOSTS = [f"ssh-host-{i:02d}" for i in range(1, 21)]


def _read(path: Path) -> str:
    assert path.exists(), f"missing lab artifact: {path}"
    return path.read_text(encoding="utf-8")


def _try_yaml():
    try:
        import yaml
    except ModuleNotFoundError:  # pragma: no cover - optional dependency
        return None
    return yaml


# --------------------------------------------------------------------------- #
# Artifacts exist
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "path", [COMPOSE, DOCKERFILE, SSHD_CONFIG, ENTRYPOINT, LOOPBACK],
)
def test_artifact_exists(path: Path) -> None:
    assert path.is_file(), f"{path.name} is missing"
    assert path.stat().st_size > 0, f"{path.name} is empty"


def test_lab_is_separate_from_the_application() -> None:
    """The lab must not reference backend/ or frontend/ source or data."""
    for path in (COMPOSE, DOCKERFILE, ENTRYPOINT):
        text = _read(path)
        for forbidden in ("../backend", "../frontend", "backend/app", "frontend/src"):
            assert forbidden not in text, f"{path.name} references {forbidden}"


# --------------------------------------------------------------------------- #
# Twenty hosts with predictable names
# --------------------------------------------------------------------------- #

def test_compose_declares_exactly_twenty_ssh_services() -> None:
    text = _read(COMPOSE)
    declared = re.findall(r"^  (ssh-host-\d{2}):\s*$", text, flags=re.MULTILINE)
    assert declared == EXPECTED_HOSTS, f"unexpected host services: {declared}"


def test_each_service_sets_matching_hostname_and_container_name() -> None:
    yaml = _try_yaml()
    if yaml is None:  # pragma: no cover - fallback when PyYAML is unavailable
        text = _read(COMPOSE)
        for host in EXPECTED_HOSTS:
            assert f"hostname: {host}" in text
            assert f"container_name: ssh-lab-{host}" in text
        return

    data = yaml.safe_load(_read(COMPOSE))
    services = data["services"]
    for host in EXPECTED_HOSTS:
        svc = services[host]
        assert svc["hostname"] == host
        assert svc["container_name"] == f"ssh-lab-{host}"
        assert svc["environment"]["LAB_HOSTNAME"] == host


def test_separate_host_key_volume_per_service() -> None:
    """Each container needs its own host keys, so identities cannot collide."""
    yaml = _try_yaml()
    if yaml is None:  # pragma: no cover
        pytest.skip("PyYAML unavailable")
    services = yaml.safe_load(_read(COMPOSE))["services"]
    seen: set[str] = set()
    for host in EXPECTED_HOSTS:
        volumes = services[host]["volumes"]
        key_vols = [v for v in volumes if "lab-host-keys" in str(v)]
        assert len(key_vols) == 1, f"{host} must mount exactly one host-key volume"
        name = str(key_vols[0]).split(":")[0]
        assert name not in seen, f"volume {name} reused across hosts"
        seen.add(name)
    assert len(seen) == 20


# --------------------------------------------------------------------------- #
# Network isolation
# --------------------------------------------------------------------------- #

def test_dedicated_network_declared() -> None:
    yaml = _try_yaml()
    if yaml is None:  # pragma: no cover
        assert "ssh-lab-net" in _read(COMPOSE)
        return
    data = yaml.safe_load(_read(COMPOSE))
    network = data["networks"]["ssh-lab"]
    assert network["name"] == "ssh-lab-net"
    assert network["driver"] == "bridge"

    for host in EXPECTED_HOSTS:
        assert "ssh-lab" in data["services"][host]["networks"]


def test_base_compose_publishes_no_ssh_port_to_the_host() -> None:
    """Requirement: do not expose SSH ports to the Windows host."""
    yaml = _try_yaml()
    if yaml is None:  # pragma: no cover - text fallback
        text = _read(COMPOSE)
        assert "ports:" not in text
        return

    services = yaml.safe_load(_read(COMPOSE))["services"]
    for name, svc in services.items():
        assert not svc.get("ports"), f"{name} publishes ports in the base compose file"


def test_loopback_override_only_binds_localhost() -> None:
    """If ports are enabled at all, they must never bind a public interface."""
    text = _read(LOOPBACK)
    bindings = re.findall(r'"([^"]+:\d+:22)"', text)
    assert bindings, "loopback override declares no port bindings"
    for binding in bindings:
        assert binding.startswith("127.0.0.1:"), f"{binding} is not loopback-only"
    ports = sorted(int(b.split(":")[1]) for b in bindings)
    assert ports == list(range(2201, 2221))


# --------------------------------------------------------------------------- #
# Authentication hardening
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "directive",
    [
        "PubkeyAuthentication yes",
        "PasswordAuthentication no",
        "KbdInteractiveAuthentication no",
        "PermitEmptyPasswords no",
        "AuthenticationMethods publickey",
        "PermitRootLogin no",
        "AllowUsers tester",
    ],
)
def test_sshd_config_hardening(directive: str) -> None:
    assert directive in _read(SSHD_CONFIG), f"missing directive: {directive}"


def test_password_auth_is_never_enabled() -> None:
    text = _read(SSHD_CONFIG)
    assert not re.search(r"^PasswordAuthentication\s+yes", text, flags=re.MULTILINE)


def test_entrypoint_installs_only_the_public_key() -> None:
    """Only id_lab.pub may enter a container; the private half must stay out."""
    text = _read(ENTRYPOINT)
    assert "id_lab.pub" in text
    assert "BEGIN OPENSSH PRIVATE KEY" not in text


def _lab_key_mounts(svc: dict) -> list[str]:
    """Mount sources that target the lab key directory (empty if none)."""
    out = []
    for mount in svc.get("volumes", []):
        parts = str(mount).split(":")
        if len(parts) >= 2 and parts[1].startswith("/etc/lab/keys"):
            out.append(parts[0])
    return out


def test_server_containers_mount_only_the_public_key() -> None:
    """No server container may see the private key.

    Only mounts targeting /etc/lab/keys are considered, so the per-host host-key
    volumes are correctly ignored. Accepting `./keys` (the whole directory) here
    would hand the private key to 20 containers.
    """
    yaml = _try_yaml()
    if yaml is None:  # pragma: no cover
        pytest.skip("PyYAML unavailable")
    services = yaml.safe_load(_read(COMPOSE))["services"]

    checked = 0
    for name, svc in services.items():
        if name == "lab-client":
            continue
        for source in _lab_key_mounts(svc):
            assert source == "./keys/id_lab.pub", (
                f"{name} mounts {source!r} at /etc/lab/keys; only the public key is allowed"
            )
            checked += 1
    assert checked == 20, f"expected 20 public-key mounts, found {checked}"


def test_client_mounts_keys_read_only() -> None:
    """The disposable client is the only place the private key appears."""
    yaml = _try_yaml()
    if yaml is None:  # pragma: no cover
        pytest.skip("PyYAML unavailable")
    svc = yaml.safe_load(_read(COMPOSE))["services"]["lab-client"]
    mounts = [str(v) for v in svc.get("volumes", [])]
    assert "./keys:/etc/lab/keys:ro" in mounts, mounts


def test_testlab_keys_directory_is_git_ignored() -> None:
    ignore = LAB_DIR / ".gitignore"
    assert ignore.exists(), "testlab/.gitignore must exist to keep lab keys out of git"
    rules = [line.strip() for line in ignore.read_text(encoding="utf-8").splitlines()]
    assert any(rule in ("keys/", "/keys/", "keys", "*key*") for rule in rules), rules


# --------------------------------------------------------------------------- #
# Runtime hygiene
# --------------------------------------------------------------------------- #

def test_no_scenario_failures_during_normal_startup() -> None:
    """Failure simulation must be on demand, never part of container start-up.

    `exit 1` is intentionally allowed: the entrypoint's fail() helper uses it to
    abort on a genuine misconfiguration. What must never appear are the
    scenario effects themselves (artificial sleeps, injected errors).
    """
    entrypoint = _read(ENTRYPOINT)
    for forbidden in ("setsid", "simulate", "sleep 9999", "sleep 300"):
        assert forbidden not in entrypoint, f"entrypoint contains {forbidden!r}"

    # Failure injection lives only in the simulations directory.
    sim = LAB_DIR / "simulations" / "simulate.py"
    assert sim.exists(), "failure simulations must be a separate, opt-in utility"

    # The entrypoint must end by exec'ing sshd in the foreground.
    assert "exec /usr/sbin/sshd" in entrypoint


def test_containers_drop_privileges() -> None:
    text = _read(COMPOSE)
    assert "no-new-privileges:true" in text
    assert "cap_drop:" in text
    assert "privileged: true" not in text
    assert "network_mode: host" not in text


def test_test_account_is_not_locked() -> None:
    """`useradd` defaults the password field to '!' which *locks* the account.

    sshd then refuses every login, including public-key, with
    "User tester not allowed because account is locked". The Dockerfile must
    therefore set a password field that is unusable but does not lock.
    """
    text = _read(DOCKERFILE)
    assert "--password '*'" in text, (
        "the test account must be created with --password '*' (unusable but not "
        "locked); the default '!' makes sshd reject public-key logins"
    )


def test_compose_grants_sys_chroot() -> None:
    """sshd chroots to /run/sshd for privilege separation.

    Without CAP_SYS_CHROOT every connection dies pre-auth with
    'chroot("/run/sshd"): Operation not permitted', which the client only sees
    as a connection reset / exit 255.
    """
    yaml = _try_yaml()
    if yaml is None:  # pragma: no cover
        pytest.skip("PyYAML unavailable")
    services = yaml.safe_load(_read(COMPOSE))["services"]
    for host in EXPECTED_HOSTS:
        caps = services[host]["cap_add"]
        assert "SYS_CHROOT" in caps, f"{host} is missing SYS_CHROOT; sshd cannot run"


def test_ssh_exec_avoids_compose_run_stream_pollution() -> None:
    """`compose run` writes "Container … Created" to stderr.

    That text would be captured as remote stderr and corrupt execution logs, so
    the SSH helper must use `docker run` instead.
    """
    text = _read(LAB_DIR / "scripts" / "lab.py")
    start = text.find("def ssh_exec(")
    body = text[start:text.find("\n@dataclass", start)]
    assert '"docker", "run"' in body, "ssh_exec should use `docker run` for clean streams"
    assert "compose_client" not in body, "ssh_exec must not use compose_client"


def test_dockerfile_normalises_line_endings() -> None:
    """A Windows checkout can add CRLF, which breaks bash and sshd."""
    text = _read(DOCKERFILE)
    assert "sed -i 's/\\r$//'" in text


def test_client_wrapper_fixes_key_permissions() -> None:
    """A bind-mounted key keeps Windows mode 0777, which OpenSSH refuses.

    The client wrapper must copy the key to a 0600 file inside the container;
    without this every connection fails with UNPROTECTED PRIVATE KEY FILE.
    """
    wrapper = LAB_DIR / "lab-ssh-client.sh"
    assert wrapper.exists(), "lab-ssh-client.sh is missing"
    text = _read(wrapper)
    assert "install -m 0600" in text
    assert "/tmp/.lab_client_key" in text


def test_client_is_profile_gated_and_starts_with_the_lab() -> None:
    """A plain `up` must not start a 21st container."""
    yaml = _try_yaml()
    if yaml is None:  # pragma: no cover
        pytest.skip("PyYAML unavailable")
    services = yaml.safe_load(_read(COMPOSE))["services"]
    assert "client" in services["lab-client"].get("profiles", [])
    for host in EXPECTED_HOSTS:
        assert "profiles" not in services[host]


def test_lab_utility_refuses_to_run_without_docker() -> None:
    """The lab must report a missing Docker rather than substituting anything."""
    script = _read(LAB_DIR / "scripts" / "lab.py")
    assert "def docker_available()" in script
    assert "def require_docker()" in script
    assert "no fallback" in script.lower()
