#!/usr/bin/env python3
"""Failure-scenario utilities for the SSH Integration Test Lab.

These simulate the failure modes the executor must handle in later milestones.
Nothing here runs during a normal `docker compose up`: standard containers start
clean, and every scenario is triggered on demand.

Scenarios
    auth-failure     connect with a purpose-built invalid key -> key rejected
    wrong-user       connect as a user that does not exist -> rejected
    unreachable      connect to a name that resolves nowhere -> connection error
    exit-1           remote command exits 1 -> non-zero status propagates
    long-running     remote command sleeps -> client-side timeout is honoured
    large-stdout     ~5 MB of stdout -> no truncation or corruption
    large-stderr     ~5 MB of stderr -> captured separately from stdout
    all              run every scenario and print a summary

Usage:
    python simulations/simulate.py all
    python simulations/simulate.py exit-1
    python simulations/simulate.py long-running --seconds 30 --timeout 10
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

LAB_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAB_DIR / "scripts"))

import lab  # noqa: E402  (sibling utility, resolved above)

TARGET = lab.host_name(1)
SIZE_BYTES = 5 * 1024 * 1024


def _result_line(name: str, ok: bool, detail: str) -> str:
    return f"[{'PASS' if ok else 'FAIL'}] {name:<14} {detail}"


def _ensure_invalid_key() -> Path:
    """A throwaway key that the lab hosts will reject. Never used elsewhere."""
    bad = lab.KEYS_DIR / "id_lab_invalid"
    pub = bad.with_suffix(".pub")
    if not bad.exists():
        lab.KEYS_DIR.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "invalid-key",
             "-f", str(bad)],
            check=True, capture_output=True, text=True,
        )
        pub.unlink(missing_ok=True)
    return bad


def scenario_auth_failure() -> tuple[bool, str]:
    bad_key = _ensure_invalid_key()
    result = lab.ssh_exec(TARGET, "true", private_key=bad_key, timeout=60)
    rejected = result.returncode != 0
    detail = (result.stderr or result.stdout or "").strip().splitlines()
    return rejected, f"exit={result.returncode} {detail[-1] if detail else ''}"[:110]


def scenario_wrong_user() -> tuple[bool, str]:
    result = lab.ssh_exec(TARGET, "true", user="no-such-user", timeout=60)
    rejected = result.returncode != 0
    detail = (result.stderr or "").strip().splitlines()
    return rejected, f"exit={result.returncode} {detail[-1] if detail else ''}"[:110]


def scenario_unreachable() -> tuple[bool, str]:
    result = lab.ssh_exec("no-such-host.invalid", "true", timeout=60)
    failed = result.returncode != 0
    detail = (result.stderr or "").strip().splitlines()
    return failed, f"exit={result.returncode} {detail[-1] if detail else ''}"[:110]


def scenario_exit_1() -> tuple[bool, str]:
    result = lab.ssh_exec(TARGET, "exit 1", timeout=60)
    return result.returncode == 1, f"exit={result.returncode} (expected 1)"


def scenario_long_running(seconds: int, timeout: int) -> tuple[bool, str]:
    started = time.monotonic()
    try:
        lab.ssh_exec(TARGET, f"sleep {seconds}", timeout=timeout)
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - started
        return True, f"client timed out after {elapsed:.1f}s (remote sleep {seconds}s)"
    elapsed = time.monotonic() - started
    return False, f"completed in {elapsed:.1f}s; expected a timeout"


def scenario_large_stdout() -> tuple[bool, str]:
    result = lab.ssh_exec(
        TARGET,
        f"head -c {SIZE_BYTES} /dev/zero | tr '\\0' 'A'",
        timeout=180,
    )
    size = len(result.stdout)
    ok = result.returncode == 0 and size >= SIZE_BYTES
    return ok, f"exit={result.returncode} stdout={size} bytes (expected >= {SIZE_BYTES})"


def scenario_large_stderr() -> tuple[bool, str]:
    result = lab.ssh_exec(
        TARGET,
        f"head -c {SIZE_BYTES} /dev/zero | tr '\\0' 'E' >&2",
        timeout=180,
    )
    size = len(result.stderr)
    ok = result.returncode == 0 and size >= SIZE_BYTES and len(result.stdout) < 1024
    return ok, (f"exit={result.returncode} stderr={size} bytes "
                f"stdout={len(result.stdout)} (streams kept separate)")


SCENARIOS = {
    "auth-failure": lambda a: scenario_auth_failure(),
    "wrong-user": lambda a: scenario_wrong_user(),
    "unreachable": lambda a: scenario_unreachable(),
    "exit-1": lambda a: scenario_exit_1(),
    "long-running": lambda a: scenario_long_running(a.seconds, a.timeout),
    "large-stdout": lambda a: scenario_large_stdout(),
    "large-stderr": lambda a: scenario_large_stderr(),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lab failure simulations")
    parser.add_argument("scenario", choices=[*SCENARIOS, "all"])
    parser.add_argument("--seconds", type=int, default=120, help="remote sleep duration")
    parser.add_argument("--timeout", type=int, default=10, help="client timeout seconds")
    args = parser.parse_args(argv)

    try:
        lab.require_docker()
        lab.ensure_keys()
    except lab.LabError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    failures = 0
    for name in names:
        try:
            ok, detail = SCENARIOS[name](args)
        except lab.LabError as exc:
            ok, detail = False, f"lab error: {exc}"
        print(_result_line(name, ok, detail))
        failures += 0 if ok else 1

    print(f"\n{len(names) - failures}/{len(names)} scenarios behaved as expected")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
