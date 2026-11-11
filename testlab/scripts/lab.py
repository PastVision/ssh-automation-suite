#!/usr/bin/env python3
"""SSH Integration Test Lab control utility.

Standard library only. Every subcommand drives `docker compose` from the
testlab/ directory, so the lab stays entirely independent of the application.

Usage:
    python scripts/lab.py keys            # generate the lab key pair
    python scripts/lab.py up              # start 20 hosts
    python scripts/lab.py down            # stop and remove them
    python scripts/lab.py health          # container + endpoint health
    python scripts/lab.py verify-auth     # public-key auth against all hosts
    python scripts/lab.py exec -- <cmd>   # run a command on every host
    python scripts/lab.py results         # per-host connectivity table
    python scripts/lab.py simulate <case> # failure scenarios
    python scripts/lab.py preflight       # check Docker availability only

Access model: no SSH port is published to the Windows host. All client work
runs in a throwaway container attached to the lab network (see `_ssh_run`).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent.parent
KEYS_DIR = LAB_DIR / "keys"
PRIVATE_KEY = KEYS_DIR / "id_lab"
PUBLIC_KEY = KEYS_DIR / "id_lab.pub"
LAB_USER = "tester"
HOST_COUNT = 20
# Must stay in sync with docker-compose.yml (image: / networks.name:).
COMPOSE_PROJECT = "ssh-lab"
SERVICE_IMAGE = "ssh-lab/ssh-server:local"
NETWORK = "ssh-lab-net"


def host_name(index: int) -> str:
    """1-based container hostname: ssh-host-01 .. ssh-host-20."""
    return f"ssh-host-{index:02d}"


ALL_HOSTS = [host_name(i) for i in range(1, HOST_COUNT + 1)]


class LabError(RuntimeError):
    """A lab operation failed."""


# --------------------------------------------------------------------------- #
# Docker plumbing
# --------------------------------------------------------------------------- #

def _run(cmd: list[str], *, check: bool = True, capture: bool = True,
         timeout: int | None = 300) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        cmd, cwd=LAB_DIR, text=True, timeout=timeout,
        capture_output=capture, check=False,
    )
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise LabError(f"command failed ({result.returncode}): {' '.join(cmd)}\n{detail}")
    return result


def docker_available() -> tuple[bool, str]:
    """Return (available, reason). Never raises."""
    if shutil.which("docker") is None:
        return False, "the `docker` CLI is not on PATH"
    probe = _run(["docker", "info", "--format", "{{.ServerVersion}}"], check=False, timeout=30)
    if probe.returncode != 0:
        detail = (probe.stderr or probe.stdout or "").strip().splitlines()
        if detail:
            return False, f"the Docker daemon is not reachable ({detail[-1]})"
        return False, "the Docker daemon is not reachable"
    compose = _run(["docker", "compose", "version"], check=False, timeout=30)
    if compose.returncode != 0:
        return False, "`docker compose` (v2) is not available"
    return True, f"Docker {probe.stdout.strip()} with compose"


def require_docker() -> None:
    ok, reason = docker_available()
    if not ok:
        raise LabError(
            f"Docker is required but unavailable: {reason}.\n"
            "This lab runs 20 real OpenSSH containers; there is no fallback "
            "environment. Install Docker Desktop (with the WSL2 backend) or "
            "Docker Engine, start it, then re-run."
        )


def compose(*args: str, check: bool = True,
            timeout: int | None = 600) -> subprocess.CompletedProcess[str]:
    return _run(["docker", "compose", *args], check=check, timeout=timeout)


def compose_client(*args: str, check: bool = False,
                   timeout: int | None = 300) -> subprocess.CompletedProcess[str]:
    """Run a compose command with the `client` profile enabled.

    `lab-client` sits behind that profile so a plain `up` never starts it, but
    `run` must target it explicitly, which requires enabling the profile here.

    NOTE: not used for SSH execution. `docker compose run` prints
    "Container … Creating/Created" progress to *stderr*, which would be
    captured as if it were remote stderr and corrupt execution logs.
    `ssh_exec` uses `docker run` instead, whose streams stay clean.
    """
    return _run(
        ["docker", "compose", "--profile", "client", *args],
        check=check, timeout=timeout,
    )


# --------------------------------------------------------------------------- #
# Keys
# --------------------------------------------------------------------------- #

def ensure_keys(force: bool = False) -> tuple[Path, Path]:
    """Create the lab key pair if absent. Never regenerates by default.

    These keys live only in testlab/keys, which is git-ignored, and are never
    read by the application.
    """
    KEYS_DIR.mkdir(parents=True, exist_ok=True)
    if PRIVATE_KEY.exists() and PUBLIC_KEY.exists() and not force:
        return PRIVATE_KEY, PUBLIC_KEY
    if PRIVATE_KEY.exists() and not force:
        raise LabError(
            f"{PRIVATE_KEY} exists but {PUBLIC_KEY.name} is missing. "
            "Refusing to overwrite a private key; pass --force to regenerate."
        )
    for stale in (PRIVATE_KEY, PUBLIC_KEY):
        stale.unlink(missing_ok=True)

    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        raise LabError("`ssh-keygen` is not on PATH (install the OpenSSH client)")
    _run([
        ssh_keygen, "-q", "-t", "ed25519", "-N", "", "-C", "ssh-lab-integration-test",
        "-f", str(PRIVATE_KEY),
    ], check=True)

    try:
        os.chmod(PRIVATE_KEY, 0o600)
    except OSError:  # pragma: no cover - Windows ACLs
        pass
    return PRIVATE_KEY, PUBLIC_KEY


def _read_public_key() -> str:
    if not PUBLIC_KEY.exists():
        raise LabError("lab public key missing; run `python scripts/lab.py keys` first")
    return PUBLIC_KEY.read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #

def cmd_keys(args: argparse.Namespace) -> int:
    private, public = ensure_keys(force=args.force)
    print(f"private key : {private}")
    print(f"public key  : {public}")
    print(f"fingerprint : {_fingerprint(public)}")
    return 0


def _fingerprint(public_key: Path) -> str:
    ssh_keygen = shutil.which("ssh-keygen")
    if ssh_keygen is None:
        return "(ssh-keygen unavailable)"
    out = _run([ssh_keygen, "-l", "-f", str(public_key)], check=False)
    return out.stdout.strip() if out.returncode == 0 else "(unreadable)"


def cmd_up(args: argparse.Namespace) -> int:
    require_docker()
    ensure_keys()
    files = ["-f", "docker-compose.yml"]
    if args.loopback:
        files += ["-f", "compose.loopback.yml"]
        print("note: publishing SSH on 127.0.0.1:2201-2220 (loopback only)")

    print(f"starting {HOST_COUNT} hosts…")
    compose(*files, "up", "-d", "--build", timeout=1800)
    return 0 if wait_for_healthy(timeout=args.timeout) else 1


def cmd_down(args: argparse.Namespace) -> int:
    require_docker()
    cmd = ["-f", "docker-compose.yml", "down", "--remove-orphans"]
    if args.volumes:
        cmd.append("-v")
    compose(*cmd, check=False, timeout=600)
    print("lab stopped" + ("" if args.volumes else " (host-key volumes kept)"))
    return 0


def wait_for_healthy(timeout: int = 180) -> bool:
    """Poll `compose ps` until every service reports healthy."""
    deadline = time.monotonic() + timeout
    last: dict[str, str] = {}
    while time.monotonic() < deadline:
        statuses = container_status()
        last = statuses
        if len(statuses) >= HOST_COUNT and all(
            statuses.get(h) == "healthy" for h in ALL_HOSTS
        ):
            print(f"all {HOST_COUNT} hosts healthy")
            return True
        time.sleep(3)
    unhealthy = {h: last.get(h, "missing") for h in ALL_HOSTS if last.get(h) != "healthy"}
    print(f"timed out waiting for health; not healthy: {unhealthy}", file=sys.stderr)
    return False


def container_status() -> dict[str, str]:
    """Map hostname -> health/state string. Empty dict if lab is not running."""
    out = compose("ps", "--format", "json", check=False, timeout=120)
    if out.returncode != 0 or not out.stdout.strip():
        return {}
    statuses: dict[str, str] = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        # Compose emits one JSON object per line (v2.21+); tolerate a JSON array too.
        name = (record.get("Service") or record.get("Name") or "").strip()
        health = (record.get("Health") or record.get("State") or "").strip().lower()
        if name:
            statuses[name] = health
    return statuses


# --------------------------------------------------------------------------- #
# SSH execution (inside the lab network)
# --------------------------------------------------------------------------- #

def ssh_exec(
    hostname: str,
    command: str,
    *,
    private_key: Path | None = None,
    timeout: int = 60,
    user: str = LAB_USER,
    use_key: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Execute `command` over SSH on one host from inside the lab network.

    A disposable container on ssh-lab-net is the client, so no SSH port is ever
    published to the Windows host. The private key is mounted read-only and only
    into that throwaway container.

    Returns the ssh client's CompletedProcess. The remote exit status is
    propagated by ssh; 255 means the connection or authentication failed.
    """
    key = private_key or PRIVATE_KEY
    if use_key and not key.exists():
        raise LabError(f"private key not found: {key} (run `python scripts/lab.py keys`)")
    username = getpass.getuser()
    # `docker run` (not `compose run`) keeps stdout/stderr exactly as the remote
    # process wrote them. `compose run` writes "Creating…/Created" progress to
    # stderr, which would contaminate captured execution logs.
    #
    # lab-ssh-client copies the key to a 0600 file inside the container. A
    # bind-mounted key keeps Windows permissions (0777), which OpenSSH rejects.
    key_arg = f"/etc/lab/keys/{key.name}" if use_key else "-"
    return _run(
        [
            "docker", "run", "--rm", "-i",
            "--network", NETWORK,
            "--label", f"lab.owner={username}",
            "--label", "lab.client=true",
            "-v", f"{KEYS_DIR}:/etc/lab/keys:ro",
            "--entrypoint", "lab-ssh-client",
            SERVICE_IMAGE,
            key_arg,
            f"{user}@{hostname}",
            command,
        ],
        check=False, timeout=timeout,
    )


@dataclass
class HostResult:
    host: str
    ok: bool
    detail: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    duration: float = 0.0
    extra: dict[str, str] = field(default_factory=dict)


def _parallel(hosts: list[str], fn, workers: int = 10) -> list[HostResult]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(fn, h): h for h in hosts}
        return [f.result() for f in concurrent.futures.as_completed(futures)]


def _probe(host: str) -> HostResult:
    started = time.monotonic()
    try:
        result = ssh_exec(host, "true", timeout=60)
    except LabError as exc:
        return HostResult(host, False, detail=str(exc))
    except subprocess.TimeoutExpired:
        return HostResult(host, False, detail="timeout")
    elapsed = time.monotonic() - started
    return HostResult(
        host,
        result.returncode == 0,
        detail="ok" if result.returncode == 0 else (result.stderr or "").strip()[:200],
        exit_code=result.returncode,
        duration=elapsed,
    )


def cmd_verify_auth(args: argparse.Namespace) -> int:
    require_docker()
    results = _parallel(ALL_HOSTS, _probe, workers=args.workers)
    failures = [r for r in results if not r.ok]
    _print_results(sorted(results, key=lambda r: r.host))
    print(f"\npublic-key auth: {len(results) - len(failures)}/{len(results)} hosts OK")
    return 0 if not failures else 1


def cmd_exec(args: argparse.Namespace) -> int:
    require_docker()
    command = " ".join(args.command).strip()
    if not command:
        raise LabError("no command given; use `exec -- <command>`")

    def run(host: str) -> HostResult:
        started = time.monotonic()
        try:
            result = ssh_exec(host, command, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            return HostResult(host, False, "timeout", exit_code=None,
                              duration=time.monotonic() - started)
        return HostResult(
            host,
            result.returncode == 0,
            stdout=result.stdout,
            stderr=result.stderr,
            exit_code=result.returncode,
            duration=time.monotonic() - started,
        )

    results = sorted(_parallel(ALL_HOSTS, run, workers=args.workers), key=lambda r: r.host)
    returncode = 0
    for r in results:
        marker = "OK  " if r.ok else "FAIL"
        print(f"[{marker}] {r.host} exit={r.exit_code} {r.duration:.2f}s")
        if r.stdout.strip():
            for line in r.stdout.rstrip().splitlines():
                print(f"         {line}")
        if r.stderr.strip():
            for line in r.stderr.rstrip().splitlines():
                print(f"    err: {line}")
        if not r.ok:
            returncode = 1
    return returncode


def cmd_health(args: argparse.Namespace) -> int:
    require_docker()
    statuses = container_status()
    if not statuses:
        print("lab is not running")
        return 1
    missing = [h for h in ALL_HOSTS if h not in statuses]
    unhealthy = [h for h, s in statuses.items() if s != "healthy"]
    for h in ALL_HOSTS:
        print(f"{h:14} {statuses.get(h, 'missing')}")
    if missing or unhealthy:
        print(f"\nmissing: {missing}\nunhealthy: {unhealthy}", file=sys.stderr)
        return 1
    print(f"\nall {HOST_COUNT} containers healthy")
    return 0


def cmd_results(args: argparse.Namespace) -> int:
    require_docker()
    results = sorted(_parallel(ALL_HOSTS, _probe, workers=args.workers), key=lambda r: r.host)
    _print_results(results)
    ok = sum(1 for r in results if r.ok)
    print(f"\n{ok}/{len(results)} reachable with public-key auth")
    return 0 if ok == len(results) else 1


def _print_results(results: list[HostResult]) -> None:
    print(f"{'HOST':<14} {'RESULT':<8} {'EXIT':<6} {'TIME':<8} DETAIL")
    print("-" * 78)
    for r in results:
        print(f"{r.host:<14} {'OK' if r.ok else 'FAIL':<8} "
              f"{str(r.exit_code):<6} {r.duration:<8.2f} {r.detail[:32]}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SSH Integration Test Lab control utility",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="No SSH port is published to the Windows host; clients run inside "
               "the lab network.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_keys = sub.add_parser("keys", help="generate the lab SSH key pair")
    p_keys.add_argument("--force", action="store_true", help="regenerate even if present")
    p_keys.set_defaults(func=cmd_keys)

    p_up = sub.add_parser("up", help="build and start all 20 hosts")
    p_up.add_argument("--loopback", action="store_true",
                      help="also publish SSH on 127.0.0.1:2201-2220 (loopback only)")
    p_up.add_argument("--timeout", type=int, default=300, help="health wait seconds")
    p_up.set_defaults(func=cmd_up)

    p_down = sub.add_parser("down", help="stop and remove the lab")
    p_down.add_argument("--volumes", action="store_true", help="also delete host-key volumes")
    p_down.set_defaults(func=cmd_down)

    p_health = sub.add_parser("health", help="container health summary")
    p_health.set_defaults(func=cmd_health)

    p_verify = sub.add_parser("verify-auth", help="verify public-key auth on all hosts")
    p_verify.add_argument("--workers", type=int, default=10)
    p_verify.set_defaults(func=cmd_verify_auth)

    p_exec = sub.add_parser("exec", help="run a command on every host")
    p_exec.add_argument("--workers", type=int, default=10)
    p_exec.add_argument("--timeout", type=int, default=120)
    p_exec.add_argument("command", nargs=argparse.REMAINDER)
    p_exec.set_defaults(func=cmd_exec)

    p_results = sub.add_parser("results", help="per-host connectivity table")
    p_results.add_argument("--workers", type=int, default=10)
    p_results.set_defaults(func=cmd_results)

    p_pre = sub.add_parser("preflight", help="check Docker availability")
    p_pre.set_defaults(func=lambda _: _cmd_preflight())

    return parser


def _cmd_preflight() -> int:
    ok, reason = docker_available()
    print(("OK   " if ok else "FAIL ") + reason)
    if ok:
        print(f"compose project : {COMPOSE_PROJECT}")
        print(f"image           : {SERVICE_IMAGE}")
        print(f"hosts           : {ALL_HOSTS[0]} .. {ALL_HOSTS[-1]}")
        print(f"keys            : {KEYS_DIR}")
    else:
        print("\nThe lab cannot run without Docker. No fallback environment is "
              "substituted, by design.")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except LabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
