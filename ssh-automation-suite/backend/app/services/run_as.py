"""Run-As: switching the remote execution identity through ``sudo``.

Design
------
Run-As is a *context block* in the workflow's step order, not a template. The
primary SSH connection is always opened with the user's own credential; Run-As
changes which Unix user the remote *commands* run as.

The password channel
--------------------
A password is never put in a command line, an environment variable, a file, or a
log line. Two separate mechanisms exist, and only one ever carries a secret:

``nopasswd``
    ``sudo -n -u TARGET ...`` -- ``-n`` means "never prompt". If the sudoers rule
    is missing, sudo fails immediately instead of hanging on a prompt.

``password``
    ``sudo -S -p '' -u TARGET ...`` with the password written to the process's
    **stdin** and then EOF. ``-S`` reads from stdin; ``-p ''`` suppresses the
    prompt text so no interactive prompt can ever reach the transcript.

    stdin is the only channel used because the remote command itself is what
    consumes it. That is also why a password block cannot carry the child
    template on stdin at the same time -- see ``wrap_script``, which uses a
    shell heredoc on the command line instead so stdin stays free for the
    password. Without that separation the password would be read by ``bash``
    as script text, or the script would be read as a password.

Which password sudo wants
-------------------------
Empirically verified against OpenSSH + sudo 1.9.13: ``sudo -u TARGET`` authenticates
the **invoking** user and then switches identity via sudoers. The target's own
password is never consulted. The lexically separate "target password" a user might
expect is therefore not a thing sudo accepts, and storing one would be storing a
credential that cannot work. The block's password is documented and used as the
primary user's sudo password, which is exactly what ``sudo`` verifies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.models.workflow import RunAsMethod

# Same constraint as a Unix account name, so it can never be shell syntax.
# A trailing hyphen is excluded because `useradd` rejects such names, so a value
# that passes here could still never exist as an account.
USERNAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]{0,31}(?<!-)$")

# Accounts whose use as a Run-As target would escalate past the sudoers grant
# rather than switch to a peer identity.
_FORBIDDEN_TARGETS = frozenset({"root"})

MAX_PASSWORD_LENGTH = 1024


class RunAsError(ValueError):
    """The Run-As block cannot be executed safely."""


@dataclass(frozen=True)
class RunAsContext:
    """One activated Run-As block for one host attempt."""

    target_username: str
    method: RunAsMethod
    # Resolved at run time from the workflow block; never stored on a snapshot,
    # never logged, never serialised into an API response.
    password: str | None = field(default=None, repr=False)


def validate_target(username: str) -> str:
    """Validate a Run-As target name. Rejects anything unusable or privileged."""
    cleaned = (username or "").strip()
    if not cleaned:
        raise RunAsError("A Run-As target username is required.")
    if not USERNAME_PATTERN.match(cleaned):
        raise RunAsError(
            f"Run-As target {cleaned!r} is not a valid Unix username: use lowercase "
            "letters, digits, underscore and hyphen, starting with a letter or "
            "underscore, at most 32 characters."
        )
    if cleaned in _FORBIDDEN_TARGETS:
        raise RunAsError(
            "Run-As to 'root' is not supported: use an unprivileged target account "
            "and grant only what it needs in sudoers."
        )
    return cleaned


def validate_password(password: str | None, method: RunAsMethod) -> str | None:
    """Check that a password is present exactly when the method needs one."""
    if method is RunAsMethod.NOPASSWD:
        # A password with a NOPASSWD target is a configuration mistake: it would
        # be silently ignored, and the user would believe it was in force.
        if password:
            raise RunAsError(
                "A password cannot be supplied for a passwordless Run-As block."
            )
        return None
    if not password:
        raise RunAsError(
            "This Run-As block requires the sudo password for the primary SSH user."
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise RunAsError("The Run-As password is too long.")
    if "\n" in password or "\r" in password or "\x00" in password:
        # A newline would terminate the password line early and the remainder
        # would be read as a second password attempt (or, worse, as command
        # input). There is no way to quote around this on stdin.
        raise RunAsError(
            "The Run-As password contains a newline or NUL character, which cannot "
            "be sent on the password channel."
        )
    return password


def _sudo_prefix(context: RunAsContext) -> str:
    """The ``sudo`` invocation for this context.

    The target name is validated before this runs, so it is a bare word; it is
    still quoted so the construction is safe even if validation is ever relaxed.
    """
    target = "'" + context.target_username.replace("'", "'\\''") + "'"
    if context.method is RunAsMethod.NOPASSWD:
        return f"sudo -n -u {target}"
    # -S: read the password from stdin. -p '': never print a prompt, so no
    # interactive text can reach the transcript.
    return f"sudo -S -p '' -u {target}"


def password_probe_command(context: RunAsContext) -> str | None:
    """A command that validates the Run-As transition itself, or None.

    Used to prove the identity switch *before* running any child template, so a
    failure is attributable to authentication or sudoers rather than to a child
    script. It does not rely on sudo's credential cache: that cache is scoped to
    the session leader, and each executed command here is its own session (verified
    against the lab -- a ``sudo -n`` following an authenticated ``sudo -S`` in the
    *next* command still failed).

    Because there is no cache to reuse, every password-mode command sends the
    password again; this probe is therefore a real, observable transition attempt
    with no side effect (``true``).
    """
    return f"{_sudo_prefix(context)} true"


def wrap_script(context: RunAsContext, interpreter: str, script: str) -> str:
    """Return the command line that runs ``script`` as the Run-As identity.

    The script is passed as a *single-quoted argument* to the interpreter rather
    than on stdin, because stdin is the password channel. Two properties matter
    and both were verified against the real lab:

    * The shell sees exactly one argument, so the body keeps its own quoting,
      ``$VAR`` expansion, backticks and newlines exactly as written. The template
      is still executed by the requested interpreter -- this is a transport detail,
      not a change in semantics.
    * stdin stays completely free, so sudo's ``-S`` reads the password and nothing
      else. The earlier heredoc form silently redirected sudo's own stdin, which
      made the script body look like a password.
    """
    program = (interpreter or "bash").strip() or "bash"
    if not program.replace("-", "").replace(".", "").isalnum():
        raise RunAsError(f"Interpreter {interpreter!r} is not a bare program name.")
    # Re-validate rather than trust construction: this function is the single
    # place a target name becomes shell text, so it must not depend on a caller
    # having validated first.
    validate_target(context.target_username)
    return f"{_sudo_prefix(context)} {program} -c {_single_quote(script)}"


def _single_quote(value: str) -> str:
    """POSIX single-quote: safe for any content, including newlines and quotes."""
    return "'" + value.replace("'", "'\\''") + "'"


def describe(context: RunAsContext) -> str:
    """A log-safe description. Contains no credential material."""
    return f"Run-As {context.target_username!r} ({context.method.value})"
