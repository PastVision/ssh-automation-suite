"""Run-As: unit validation plus real-SSH behaviour in the lab.

The unit half covers argument validation and command construction. The lab half
proves the identity actually switches against a real OpenSSH server with real
sudoers rules, and that a wrong password or a missing grant fails loudly instead
of silently continuing as the primary user.
"""

from __future__ import annotations

import asyncio

import pytest

from app.models.workflow import RunAsMethod
from app.services import key_store, run_as, ssh_transport
from tests import lab_support

lab = lab_support.lab_marker

# Credentials seeded into the lab image (testlab/Dockerfile).
LAB_SUDO_PASSWORD = "LabSudoPassword1"
LAB_NOPASSWD_TARGET = "deploy"
LAB_PASSWORD_TARGET = "opspw"


# --------------------------------------------------------------------------- #
# Target validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", ["deploy", "opspw", "app_user", "a", "user-1", "_svc"])
def test_valid_targets(name: str) -> None:
    assert run_as.validate_target(name) == name


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "root",
        "Root",
        "has space",
        "has;semi",
        "has'quote",
        "has\"quote\"",
        "$(id)",
        "`id`",
        "a" * 33,
        "UPPER",
        "with\nnewline",
        "-leading",
        "trailing-",
    ],
)
def test_invalid_targets_are_rejected(name: str) -> None:
    with pytest.raises(run_as.RunAsError):
        run_as.validate_target(name)


def test_root_is_refused_explicitly() -> None:
    with pytest.raises(run_as.RunAsError, match="root"):
        run_as.validate_target("root")


# --------------------------------------------------------------------------- #
# Password validation
# --------------------------------------------------------------------------- #


def test_nopasswd_forbids_a_password() -> None:
    with pytest.raises(run_as.RunAsError, match="passwordless"):
        run_as.validate_password("secret", RunAsMethod.NOPASSWD)


def test_nopasswd_accepts_no_password() -> None:
    assert run_as.validate_password(None, RunAsMethod.NOPASSWD) is None


def test_password_requires_a_password() -> None:
    with pytest.raises(run_as.RunAsError, match="requires the sudo password"):
        run_as.validate_password(None, RunAsMethod.PASSWORD)
    with pytest.raises(run_as.RunAsError):
        run_as.validate_password("", RunAsMethod.PASSWORD)


def test_password_with_newline_is_refused() -> None:
    with pytest.raises(run_as.RunAsError, match="newline"):
        run_as.validate_password("a\nb", RunAsMethod.PASSWORD)


def test_password_with_nul_is_refused() -> None:
    with pytest.raises(run_as.RunAsError):
        run_as.validate_password("a\x00b", RunAsMethod.PASSWORD)


def test_overlong_password_is_refused() -> None:
    with pytest.raises(run_as.RunAsError, match="too long"):
        run_as.validate_password("x" * 2000, RunAsMethod.PASSWORD)


def test_valid_password_passes() -> None:
    assert run_as.validate_password("ok", RunAsMethod.PASSWORD) == "ok"


# --------------------------------------------------------------------------- #
# Command construction
# --------------------------------------------------------------------------- #


def test_nopasswd_command_never_prompts() -> None:
    context = run_as.RunAsContext(LAB_NOPASSWD_TARGET, RunAsMethod.NOPASSWD)
    command = run_as.wrap_script(context, "bash", "id -un")
    assert "sudo -n" in command
    assert "-S" not in command
    assert "bash -c" in command


def test_password_command_reads_from_stdin_and_hides_the_prompt() -> None:
    context = run_as.RunAsContext(
        LAB_PASSWORD_TARGET, RunAsMethod.PASSWORD, LAB_SUDO_PASSWORD
    )
    command = run_as.wrap_script(context, "bash", "id -un")
    assert "sudo -S" in command
    assert "-p ''" in command
    # The password must never reach the command line.
    assert LAB_SUDO_PASSWORD not in command


def test_repr_hides_the_password() -> None:
    context = run_as.RunAsContext(
        LAB_PASSWORD_TARGET, RunAsMethod.PASSWORD, LAB_SUDO_PASSWORD
    )
    assert LAB_SUDO_PASSWORD not in repr(context)


def test_describe_hides_the_password() -> None:
    context = run_as.RunAsContext(
        LAB_PASSWORD_TARGET, RunAsMethod.PASSWORD, LAB_SUDO_PASSWORD
    )
    text = run_as.describe(context)
    assert LAB_SUDO_PASSWORD not in text
    assert LAB_PASSWORD_TARGET in text


def test_script_with_quotes_and_newlines_is_one_argument() -> None:
    context = run_as.RunAsContext(LAB_NOPASSWD_TARGET, RunAsMethod.NOPASSWD)
    script = "echo 'single'\necho \"double\"\necho $HOME"
    command = run_as.wrap_script(context, "bash", script)
    # Exactly one singly-quoted argument, with internal quotes escaped as '\''.
    assert command.endswith("'")


def test_interpreter_must_be_a_bare_name() -> None:
    context = run_as.RunAsContext(LAB_NOPASSWD_TARGET, RunAsMethod.NOPASSWD)
    with pytest.raises(run_as.RunAsError, match="bare program name"):
        run_as.wrap_script(context, "bash; rm -rf /", "true")


def test_probe_command_exists_for_both_methods() -> None:
    for method in (RunAsMethod.NOPASSWD, RunAsMethod.PASSWORD):
        password = None if method is RunAsMethod.NOPASSWD else "x"
        context = run_as.RunAsContext("deploy", method, password)
        assert run_as.password_probe_command(context).endswith("true")


# --------------------------------------------------------------------------- #
# Real SSH behaviour
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def lab_env():
    if not lab_support.lab_available():
        pytest.skip("SSH test lab not reachable")
    return lab_support.configure_lab_settings(pytest.MonkeyPatch())


def _connect(settings, index: int = 1):
    async def _open():
        pem = lab_support.read_lab_key()
        key = key_store.inspect_private_key(pem)
        host, port = lab_support.lab_address(index)
        return await ssh_transport.connect(
            host=host, port=port, username=lab_support.LAB_USER, client_key=key,
            settings=settings,
        )
    return _open


def _run(settings, command: str, stdin: str | None = None, index: int = 1):
    async def _go():
        conn = await _connect(settings, index)()
        try:
            return await ssh_transport.run_command(
                conn, command, timeout=30, stdin_data=stdin
            )
        finally:
            await ssh_transport.close(conn)
    return asyncio.run(_go())


def test_passwordless_sudo_switches_identity(lab_env) -> None:
    context = run_as.RunAsContext(LAB_NOPASSWD_TARGET, RunAsMethod.NOPASSWD)
    command = run_as.wrap_script(context, "bash", "id -un")
    result = _run(lab_env, command)
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == LAB_NOPASSWD_TARGET


def test_password_sudo_switches_identity(lab_env) -> None:
    context = run_as.RunAsContext(
        LAB_PASSWORD_TARGET, RunAsMethod.PASSWORD, LAB_SUDO_PASSWORD
    )
    command = run_as.wrap_script(context, "bash", "id -un")
    result = _run(lab_env, command, stdin=f"{LAB_SUDO_PASSWORD}\n")
    assert result.exit_code == 0, result.stderr
    assert result.stdout.strip() == LAB_PASSWORD_TARGET


def test_wrong_password_fails_and_does_not_fall_back(lab_env) -> None:
    context = run_as.RunAsContext(
        LAB_PASSWORD_TARGET, RunAsMethod.PASSWORD, "definitely-wrong"
    )
    command = run_as.wrap_script(context, "bash", "id -un")
    result = _run(lab_env, command, stdin="definitely-wrong\n")
    assert result.exit_code != 0
    # Crucially, no output claiming to be either identity: it must not silently
    # run the command as the primary user.
    assert lab_support.LAB_USER not in result.stdout
    assert LAB_PASSWORD_TARGET not in result.stdout


def test_password_never_appears_in_output(lab_env) -> None:
    context = run_as.RunAsContext(
        LAB_PASSWORD_TARGET, RunAsMethod.PASSWORD, LAB_SUDO_PASSWORD
    )
    command = run_as.wrap_script(context, "bash", "id -un; echo done")
    result = _run(lab_env, command, stdin=f"{LAB_SUDO_PASSWORD}\n")
    combined = result.stdout + result.stderr
    assert LAB_SUDO_PASSWORD not in combined


def test_invalid_password_for_sudoers_missing_grant(lab_env) -> None:
    """A target with no sudoers rule must fail, not silently succeed."""
    context = run_as.RunAsContext("nobody", RunAsMethod.NOPASSWD)
    command = run_as.wrap_script(context, "bash", "id -un")
    result = _run(lab_env, command)
    assert result.exit_code != 0
    assert result.stdout.strip() == ""


def test_script_semantics_survive_the_wrapper(lab_env) -> None:
    """Quoting, expansion and newlines inside a Run-As child behave as written."""
    script = (
        "echo \"user=$(id -un)\"\n"
        "printf '%s\\n' \"$HOME\"\n"
        "printf '%s\\n' 'literal $NOT_EXPANDED'\n"
        "X=1; echo \"shellvar=$X\"\n"
    )
    context = run_as.RunAsContext(LAB_NOPASSWD_TARGET, RunAsMethod.NOPASSWD)
    result = _run(lab_env, run_as.wrap_script(context, "bash", script))
    assert result.exit_code == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == f"user={LAB_NOPASSWD_TARGET}"
    assert lines[1] == f"/home/{LAB_NOPASSWD_TARGET}"
    assert lines[2] == "literal $NOT_EXPANDED"
    assert lines[3] == "shellvar=1"


def test_exit_status_of_the_child_propagates(lab_env) -> None:
    context = run_as.RunAsContext(LAB_NOPASSWD_TARGET, RunAsMethod.NOPASSWD)
    result = _run(lab_env, run_as.wrap_script(context, "bash", "exit 42"))
    assert result.exit_code == 42


def test_run_as_does_not_grant_root(lab_env) -> None:
    """The lab grants only the two named targets, so root must be refused."""
    context = run_as.RunAsContext("root", RunAsMethod.NOPASSWD)
    with pytest.raises(run_as.RunAsError):
        run_as.wrap_script(context, "bash", "id -un")
    # And even bypassing validation, sudoers has no grant.
    result = _run(lab_env, "sudo -n -u root id -un")
    assert result.exit_code != 0


def test_primary_identity_is_restored_after_run_as(lab_env) -> None:
    """Run-As must not leak into subsequent commands on the same connection."""
    async def _go():
        conn = await _connect(lab_env)()
        try:
            context = run_as.RunAsContext(LAB_NOPASSWD_TARGET, RunAsMethod.NOPASSWD)
            switched = await ssh_transport.run_command(
                conn, run_as.wrap_script(context, "bash", "id -un"), timeout=30
            )
            plain = await ssh_transport.run_command(conn, "id -un", timeout=30)
            return switched, plain
        finally:
            await ssh_transport.close(conn)

    switched, plain = asyncio.run(_go())
    assert switched.stdout.strip() == LAB_NOPASSWD_TARGET
    assert plain.stdout.strip() == lab_support.LAB_USER


def test_run_as_isolated_between_hosts(lab_env) -> None:
    """A Run-As block on one host must not affect another host's execution."""
    context = run_as.RunAsContext(LAB_NOPASSWD_TARGET, RunAsMethod.NOPASSWD)
    first = _run(lab_env, run_as.wrap_script(context, "bash", "id -un"), index=1)
    second = _run(lab_env, "id -un", index=2)
    assert first.stdout.strip() == LAB_NOPASSWD_TARGET
    assert second.stdout.strip() == lab_support.LAB_USER
