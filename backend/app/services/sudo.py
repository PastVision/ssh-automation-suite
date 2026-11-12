"""Non-interactive ``sudo`` handling for template scripts.

The problem
-----------
A template is executed over an SSH channel that has no controlling terminal.
``sudo`` without a tty fails with::

    sudo: a terminal is required to read the password; either use -S option
          or configure an askpass helper

so a template that works when a human runs it fails under this product. Rewriting
``sudo`` invocations to read the password from stdin (``-S``) fixes that, but the
rewrite must not change what the script *means*.

Why not a string replacement
----------------------------
``sudo`` appears inside quotes, in comments, as part of a longer word
(``sudoedit``), as an argument (``command sudo``), and in heredocs. A
``script.replace("sudo ", "sudo -S ")`` would rewrite all of those, changing
quoted text and breaking ``echo "sudo: ..."``. It would also add a second ``-S``
to a command that already has one.

What this module does instead
-----------------------------
It lexes the script with a small POSIX-shell-aware scanner that understands
single quotes, double quotes, backslash escapes, comments and heredocs, and
tracks *command position* -- the places where a word is the command name rather
than an argument. Only a ``sudo`` that is genuinely the invoked command is
touched. The scanner never executes anything and never evaluates expansions; it
only decides where words begin and end.

Limitations, stated honestly
----------------------------
* Only ``sudo`` is handled. ``doas``, ``pkexec`` and ``su`` have different
  authentication models and are left alone; ``su`` in particular authenticates
  the *target* user's password and cannot be satisfied by the invoking user's.
* Heredoc bodies are skipped, so a ``sudo`` inside a heredoc that is later
  executed by a nested shell is not detected.
* A command assembled at run time (``SUDO="sudo -u x"; $SUDO ls``) cannot be
  detected statically; it is not rewritten.
* ``sudo`` invoked through ``xargs``/``find -exec`` is not rewritten.

These limits make the rewrite conservative: it can miss a case (the script fails
the way it did before) but it never rewrites text that is not a command.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Words that end a command and begin a new one. A `sudo` immediately following
# one of these is in command position.
_COMMAND_SEPARATORS = frozenset(
    {";", "&&", "||", "|", "&", "(", ")", "{", "}", "\n", ";;"}
)

# Reserved words after which the next word is still a command, e.g. `then sudo`,
# `do sudo`, `else sudo`.
_COMMAND_INTRODUCERS = frozenset(
    {"then", "do", "else", "elif", "if", "while", "until", "!", "time", "exec"}
)

# `sudo` options that consume the following word as their value. Needed so the
# scan can tell a sudo *option's* argument from the command sudo will run -- a
# `-S` appearing after `-u alice` belongs to sudo, but one appearing after the
# command word does not.
_SUDO_OPTIONS_WITH_VALUE = frozenset(
    {
        "-u", "--user", "-g", "--group", "-p", "--prompt", "-C", "--close-from",
        "-h", "--host", "-r", "--role", "-t", "--type", "-T", "--command-timeout",
        "-U", "--other-user", "-D", "--chdir", "-R", "--chroot",
    }
)

# Sudo options that already make it non-interactive or are harmless to keep.
_SUDO_FLAGS_NO_VALUE = frozenset(
    {"-S", "--stdin", "-n", "--non-interactive", "-A", "--askpass"}
)

_SUDO_PROGRAM = "sudo"


@dataclass(frozen=True)
class SudoInvocation:
    """One detected ``sudo`` command and what it needs."""

    #: Offset just past the ``sudo`` word, where a ``-S`` would be inserted.
    end_of_word: int
    needs_stdin_flag: bool


@dataclass(frozen=True)
class Analysis:
    invocations: tuple[SudoInvocation, ...]
    requires_password: bool
    reason: str | None


def _scan_words(script: str):
    """Yield ``(word, start, end, in_command_position)`` for each shell word.

    This is deliberately not a full parser. It tracks just enough state to know
    which region of the text is code, which is a quoted literal, and which is a
    comment or heredoc body, so that a word inside a quoted string is never
    mistaken for a command.
    """
    length = len(script)
    index = 0
    command_position = True
    pending_heredocs: list[tuple[str, bool]] = []

    while index < length:
        char = script[index]

        if char in " \t":
            index += 1
            continue

        if char == "\\" and index + 1 < length:
            index += 2
            continue

        if char == "\n":
            index += 1
            # A heredoc body starts on the line after the redirection operator.
            if pending_heredocs:
                index = _skip_heredoc_bodies(script, index, pending_heredocs)
                pending_heredocs = []
            command_position = True
            continue

        if char == "#":
            # Comment to end of line. Only a comment when it starts a word, which
            # it does here because the previous iteration consumed the word.
            while index < length and script[index] != "\n":
                index += 1
            continue

        if char in _COMMAND_SEPARATORS:
            # Longest-match for two-character separators.
            if script.startswith(";;", index):
                index += 2
            elif (
                script.startswith("&&", index)
                or script.startswith("||", index)
            ):
                index += 2
            else:
                index += 1
            command_position = True
            continue

        if char in "<>":
            # Redirection. `<<`/`<<-` open a heredoc whose body must be skipped,
            # otherwise a `sudo` inside the body would be read as a command.
            operator, index = _read_redirection(script, index)
            if operator in ("<<", "<<-"):
                delimiter, index = _read_heredoc_delimiter(script, index)
                if delimiter is not None:
                    pending_heredocs.append((delimiter, operator == "<<-"))
            # A redirection does not change command position: in `>out sudo x`
            # (invalid) and in `sudo x >out`, the command word is unaffected.
            continue

        start = index
        word, index = _read_word(script, index)
        if not word:
            # A bare character we did not classify; step over it so the loop
            # always advances.
            index = start + 1
            continue

        yield word, start, index, command_position

        if command_position and word in _COMMAND_INTRODUCERS:
            # Still command position for the next word.
            continue
        command_position = word in _COMMAND_SEPARATORS


def _read_redirection(script: str, index: int) -> tuple[str, int]:
    """Read a redirection operator starting at ``index``."""
    for operator in ("<<-", "<<<", "<<", ">>", ">&", "<&", ">|", "<", ">"):
        if script.startswith(operator, index):
            return operator, index + len(operator)
    return script[index], index + 1


def _read_word(script: str, index: int) -> tuple[str, int]:
    """Read one shell word, keeping quoted content but dropping the quotes.

    Quote removal matches what a shell does to a *word*, which is what we need in
    order to recognise a command name. It is not used to build the output.
    """
    length = len(script)
    pieces: list[str] = []
    while index < length:
        char = script[index]
        if char in " \t\n;&|(){}<>":
            break
        if char == "\\" and index + 1 < length:
            pieces.append(script[index + 1])
            index += 2
            continue
        if char == "'":
            end = script.find("'", index + 1)
            if end == -1:
                pieces.append(script[index + 1 :])
                index = length
                break
            pieces.append(script[index + 1 : end])
            index = end + 1
            continue
        if char == '"':
            end = index + 1
            while end < length and script[end] != '"':
                if script[end] == "\\" and end + 1 < length:
                    end += 1
                end += 1
            pieces.append(script[index + 1 : end])
            index = min(end + 1, length)
            continue
        pieces.append(char)
        index += 1
    return "".join(pieces), index


def _read_heredoc_delimiter(script: str, index: int) -> tuple[str | None, int]:
    """Read the delimiter after ``<<``, returning ``(delimiter, next_index)``."""
    length = len(script)
    while index < length and script[index] in " \t":
        index += 1
    word, index = _read_word(script, index)
    # A quoted delimiter disables expansion but does not change where the body
    # ends, so quote removal (already done) is sufficient here.
    return (word or None), index


def _skip_heredoc_bodies(
    script: str, index: int, heredocs: list[tuple[str, bool]]
) -> int:
    """Advance past the bodies of the heredocs opened on the previous line."""
    length = len(script)
    for delimiter, strip_tabs in heredocs:
        while index < length:
            newline = script.find("\n", index)
            line_end = length if newline == -1 else newline
            line = script[index:line_end]
            if strip_tabs:
                line = line.lstrip("\t")
            index = min(line_end + 1, length)
            if line == delimiter:
                break
    return index


def analyze(script: str) -> Analysis:
    """Find every ``sudo`` command in ``script`` and decide what it needs.

    ``requires_password`` is True when at least one invocation would prompt: it
    runs without ``-n`` and without a usable non-interactive flag. A script whose
    sudo calls are all explicitly ``-n`` is not reported, because passwordless
    sudo is a legitimate and common configuration.
    """
    invocations: list[SudoInvocation] = []
    words = list(_scan_words(script))

    for position, (word, _start, end, in_command_position) in enumerate(words):
        if not in_command_position or word != _SUDO_PROGRAM:
            continue
        invocations.append(_inspect_sudo(words, position, end))

    requires_password = any(inv.needs_stdin_flag for inv in invocations)
    reason = None
    if requires_password:
        reason = (
            "This template runs sudo without -n, so sudo will ask for a "
            "password."
        )
    return Analysis(
        invocations=tuple(invocations),
        requires_password=requires_password,
        reason=reason,
    )


def _inspect_sudo(
    words: list[tuple[str, int, int, bool]], position: int, end_of_word: int
) -> SudoInvocation:
    """Classify one sudo command: its options, and whether it needs ``-S``."""
    has_stdin = False
    non_interactive = False
    index = position + 1

    while index < len(words):
        word, _word_start, _word_end, _ = words[index]
        if word == "--":
            break
        if not word.startswith("-") or word == "-":
            # First non-option word: this is the command sudo will execute.
            break
        base = word.split("=", 1)[0]
        if base in ("-S", "--stdin"):
            has_stdin = True
        if base in ("-n", "--non-interactive"):
            non_interactive = True
        if word in _SUDO_OPTIONS_WITH_VALUE or (
            base in _SUDO_OPTIONS_WITH_VALUE and "=" not in word
        ):
            # Skip this option *and* its value.
            index += 2
            continue
        index += 1

    return SudoInvocation(
        end_of_word=end_of_word,
        needs_stdin_flag=not has_stdin and not non_interactive,
    )


def rewrite(script: str) -> str:
    """Add ``-S`` to every sudo command that would otherwise prompt.

    Insertions are applied right-to-left so earlier offsets stay valid. Commands
    that already carry ``-S`` or ``-n`` are untouched, as is any text that is not
    a command.
    """
    analysis = analyze(script)
    targets = [inv for inv in analysis.invocations if inv.needs_stdin_flag]
    if not targets:
        return script

    result = script
    for inv in sorted(targets, key=lambda item: item.end_of_word, reverse=True):
        result = result[: inv.end_of_word] + " -S" + result[inv.end_of_word :]
    return result


MISSING_PASSWORD_WARNING = (
    "Sudo password not configured. This command requires elevated privileges, "
    "but no sudo password is configured for this host. Configure the sudo "
    "password in host settings or verify that passwordless sudo is enabled."
)


def missing_password_warning(hostname: str) -> str:
    """The operator-facing warning, naming the host it applies to."""
    return f"{hostname}: {MISSING_PASSWORD_WARNING}"


def requires_sudo_password(script: str) -> bool:
    """Whether ``script`` would need a password to run its sudo commands."""
    return analyze(script).requires_password


def sudo_invocation_count(script: str) -> int:
    """How many sudo commands would read a password from stdin.

    Each one consumes a line, so the caller must supply that many. Counting them
    is what lets one password line serve a script that calls sudo repeatedly
    without guessing at sudo's credential-cache behaviour.
    """
    return sum(1 for inv in analyze(script).invocations if inv.needs_stdin_flag)


# Commands that read standard input whenever they run at all.
_ALWAYS_STDIN = frozenset(
    {"read", "xargs", "fzf", "dialog", "whiptail", "readarray", "mapfile"}
)

# Commands that read standard input only when given no file operand. `cat f` is
# a file read; bare `cat` is a pipe read, which is the case that collides with
# sudo's password channel.
_STDIN_WHEN_NO_OPERAND = frozenset(
    {"cat", "tee", "dd", "head", "tail", "sed", "grep", "awk", "sort", "uniq",
     "wc", "tr", "cut", "paste", "join", "base64", "openssl", "less", "more"}
)


def _simple_commands(script: str):
    """Yield ``(name, operands, span, lead)`` for each simple command.

    Groups the words the scanner yields by command position, so an operand
    belonging to one command is never attributed to the next. Quote- and
    heredoc-aware, because it is built on the same scanner as the rewrite.

    ``span`` is the raw source range of the command, used to notice a
    redirection. ``lead`` is the text between the previous command and this one,
    used to tell a pipeline head from a later stage.
    """
    name: str | None = None
    operands: list[str] = []
    start = 0
    lead = ""
    last_end = 0
    for word, word_start, word_end, command_position in _scan_words(script):
        if command_position:
            if name is not None:
                yield name, operands, script[start:word_start], lead
            name = word.rsplit("/", 1)[-1] if word else None
            operands = []
            lead = script[last_end:word_start]
            start = word_start
            last_end = word_end
            continue
        if name is None:
            continue
        operands.append(word)
        last_end = word_end
    if name is not None:
        yield name, operands, script[start:], lead


def _reads_stdin(name: str, operands: list[str], span: str, lead: str) -> bool:
    """Whether this command would consume the *script's* standard input."""
    if name in _ALWAYS_STDIN:
        return True
    # `dd` and `tee` read stdin unless told otherwise, and they say so with
    # `if=file` rather than a bare operand, so the operand rule below would
    # wrongly clear them.
    if name == "dd":
        return not any(op.startswith("if=") for op in operands)
    if name == "tee":
        return True
    if name not in _STDIN_WHEN_NO_OPERAND:
        return False
    # `| cmd` and `cmd < file` get their input elsewhere, so they never compete
    # with sudo for the script's stdin.
    if lead.strip().endswith("|") or "<" in span:
        return False
    # Only a bare invocation reads stdin; any operand is a file argument.
    # Options are not operands: `wc -l` still reads stdin.
    positional = [op for op in operands if not op.startswith("-")]
    return not positional or "-" in operands


def stdin_consumer(script: str) -> str | None:
    """Return the first command that would read the script's stdin, or None.

    Built on the same quote- and heredoc-aware scanner as the sudo rewrite, so
    ``echo 'read me'`` is not mistaken for a stdin read -- a naive substring
    search reports exactly that, which would refuse a perfectly valid template.

    Pipeline stages, redirections and file operands are all excluded, because
    none of them consume the script's own stdin and flagging them would refuse
    scripts that are perfectly fine.
    """
    for name, operands, span, lead in _simple_commands(script):
        if _reads_stdin(name, operands, span, lead):
            return name
    return None


_SUDO_ERROR_PATTERN = re.compile(
    r"sudo:\s*(a (terminal|password) is required|no tty present)",
    re.IGNORECASE,
)


def looks_like_sudo_auth_failure(stderr: str) -> bool:
    """Whether output shows sudo failed for want of a password or a terminal.

    Used to turn an opaque non-zero exit into an actionable message. Matched
    narrowly: a script that merely *prints* the word sudo must not be diagnosed.
    """
    return bool(_SUDO_ERROR_PATTERN.search(stderr or ""))
