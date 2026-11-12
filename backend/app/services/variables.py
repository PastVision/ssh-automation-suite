"""Workflow variable rendering.

Substitutes ``{{name}}`` references in a script body with values discovered on the
*current* host. This module is pure: it takes a script and a mapping and returns a
string, so it can be exhaustively tested without a connection, a database or a
loop.

Why not ``str.replace``
-----------------------
A naive global replacement is wrong in both directions. It would corrupt a value
that happens to contain shell syntax, and it cannot know whether the reference
sits in a bare word, a double-quoted string, a single-quoted string, or a comment
-- each of which needs different escaping. Rendering therefore scans the script
once, tracks the shell quoting context, and escapes each value for exactly the
context it lands in.

Contexts
--------
``NONE``    bare word: the value is wrapped in single quotes.
``DOUBLE``  inside ``"..."``: ``\\``, ``"``, ``$`` and backtick are escaped.
``SINGLE``  inside ``'...'``: only ``'`` needs breaking out of, as ``'\\''``.
``COMMENT`` after an unquoted ``#``: nothing is escaped -- a comment never runs.

Two properties are structural rather than enforced by a check:

* **No recursion.** The output is built as a new string; inserted text is never
  re-scanned, so a value that itself contains ``{{x}}`` is emitted literally.
* **Shell variables are untouched.** ``$HOME``, ``${PATH}`` and ``$USER`` are
  ordinary characters here. ``$`` is never a sigil this module understands, so it
  cannot be mistaken for ``${...}`` interpolation.

Undefined names, malformed braces and variable references inside a heredoc body
all raise ``RenderError`` instead of being guessed at.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from app.models.enums import VariableSource

# A name must be a valid shell identifier so a value can never be confused with
# syntax, and so the workflow editor can present it next to `$HOME` safely.
NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# ``{{ name }}`` with optional inner whitespace. Deliberately narrow: no
# attribute access, no function calls, no expressions.
REFERENCE_PATTERN = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

MAX_NAME_LENGTH = 64
MAX_VARIABLES = 64
MAX_VALUE_CHARS = 64 * 1024
MAX_RENDERED_CHARS = 1024 * 1024
# A static value is typed by the operator, not captured from a host, so it is
# bounded more tightly than a discovery result: it exists to parameterise a
# script, not to carry a file.
MAX_STATIC_VALUE_CHARS = 8 * 1024
MAX_DISCOVERY_COMMAND_CHARS = 8 * 1024

_HEREDOC_PATTERN = re.compile(r"<<(-?)(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\2")


class RenderError(ValueError):
    """The script cannot be rendered safely. Fails the host; never guesses."""


@dataclass(frozen=True)
class RenderResult:
    text: str
    substituted: tuple[str, ...]


def validate_name(name: str) -> str:
    """Return the name unchanged, or raise ``RenderError`` explaining why not."""
    cleaned = (name or "").strip()
    if not cleaned:
        raise RenderError("A variable name cannot be empty.")
    if len(cleaned) > MAX_NAME_LENGTH:
        raise RenderError(
            f"Variable name {cleaned!r} is longer than {MAX_NAME_LENGTH} characters."
        )
    if not NAME_PATTERN.match(cleaned):
        raise RenderError(
            f"Variable name {cleaned!r} is not valid: use letters, digits and "
            "underscore, starting with a letter or underscore, at most "
            f"{MAX_NAME_LENGTH} characters."
        )
    return cleaned


def validate_unique(names: Sequence[str]) -> None:
    seen: set[str] = set()
    for raw in names:
        name = validate_name(raw)
        if name in seen:
            raise RenderError(f"Variable {name!r} is defined more than once.")
        seen.add(name)
    if len(seen) > MAX_VARIABLES:
        raise RenderError(
            f"A workflow may define at most {MAX_VARIABLES} variables (found {len(seen)})."
        )


@dataclass(frozen=True)
class VariableDefinition:
    """One variable as the editor submitted it, before validation.

    A plain value object rather than the ORM row so the same rules apply to a
    workflow save, an execution freeze and a test, and so validation has no
    database dependency.
    """

    name: str
    source: VariableSource
    discovery_command: str = ""
    static_value: str | None = None
    timeout_seconds: int = 30


def validate_definition(definition: VariableDefinition) -> VariableDefinition:
    """Validate one variable and return it normalised.

    The two sources have deliberately disjoint requirements: a command-backed
    variable must have a command and no static value, and a user-defined one must
    have a value and no command. Rejecting the unused field rather than ignoring
    it is what stops a variable from silently changing source when an editor
    clears the wrong box.
    """
    name = validate_name(definition.name)
    command = (definition.discovery_command or "").strip()
    static = definition.static_value

    if definition.source is VariableSource.COMMAND_OUTPUT:
        if not command:
            raise RenderError(
                f"Variable {name!r} uses command output, so it needs a discovery "
                "command."
            )
        if len(command) > MAX_DISCOVERY_COMMAND_CHARS:
            raise RenderError(
                f"Variable {name!r} has a discovery command longer than "
                f"{MAX_DISCOVERY_COMMAND_CHARS} characters."
            )
        if static:
            raise RenderError(
                f"Variable {name!r} uses command output and cannot also carry a "
                "static value."
            )
        static = None
    else:
        if static is None:
            raise RenderError(
                f"Variable {name!r} is user-defined, so it needs a value."
            )
        if len(static) > MAX_STATIC_VALUE_CHARS:
            raise RenderError(
                f"Variable {name!r} has a value longer than "
                f"{MAX_STATIC_VALUE_CHARS} characters."
            )
        if command:
            raise RenderError(
                f"Variable {name!r} is user-defined, so it cannot also have a "
                "discovery command."
            )
        command = ""

    if not 1 <= definition.timeout_seconds <= 3600:
        raise RenderError(
            f"Variable {name!r} needs a timeout between 1 and 3600 seconds."
        )

    return VariableDefinition(
        name=name,
        source=definition.source,
        discovery_command=command,
        static_value=static,
        timeout_seconds=definition.timeout_seconds,
    )


def validate_definitions(definitions: Sequence[VariableDefinition]) -> list[VariableDefinition]:
    """Validate a whole variable list: per-variable rules plus uniqueness."""
    validated = [validate_definition(definition) for definition in definitions]
    validate_unique([definition.name for definition in validated])
    return validated


def _quote_for_bare(value: str) -> str:
    """Single-quote a value so the shell sees exactly one word, unexpanded."""
    return "'" + value.replace("'", "'\\''") + "'"


def _quote_for_single(value: str) -> str:
    """Escape for insertion *inside* an existing single-quoted string."""
    return value.replace("'", "'\\''")


def _quote_for_double(value: str) -> str:
    """Escape for insertion *inside* an existing double-quoted string."""
    out: list[str] = []
    for char in value:
        if char in ('\\', '"', '$', "`"):
            out.append("\\")
        out.append(char)
    return "".join(out)


def render(script: str, values: Mapping[str, str]) -> RenderResult:
    """Render ``script`` with ``values``, escaping per shell context.

    Raises ``RenderError`` for an undefined name, a malformed ``{{...}}``, or a
    reference inside a heredoc body.
    """
    if not script:
        return RenderResult(text="", substituted=())

    out: list[str] = []
    substituted: list[str] = []
    # Quote state of the *current* position in the script, not of the output.
    state = "none"
    index = 0
    length = len(script)
    # A ``#`` starts a comment only at the beginning of a *word* (start of line or
    # after unquoted whitespace/operator), not mid-word as in ``a#b``.
    at_word_start = True
    heredoc_terminator: str | None = None
    heredoc_strip_tabs = False

    while index < length:
        char = script[index]

        # -- heredoc body: consume whole lines, refusing substitutions -------- #
        if heredoc_terminator is not None:
            end = script.find("\n", index)
            line = script[index:] if end == -1 else script[index:end]
            candidate = line.lstrip("\t") if heredoc_strip_tabs else line
            closing = candidate == heredoc_terminator
            if not closing and "{{" in line:
                raise RenderError(
                    "Variable references are not supported inside a heredoc body. "
                    "Assign the value to a shell variable first, then use that "
                    "variable in the heredoc."
                )
            out.append(char if end == -1 else script[index:end + 1])
            if closing:
                heredoc_terminator = None
            at_word_start = True
            index = length if end == -1 else end + 1
            continue

        if char == "\n":
            state = "none"
            at_word_start = True
            out.append(char)
            index += 1
            continue

        if state == "comment":
            if script.startswith("{{", index):
                rendered, index = _render_at(script, index, values, substituted, "comment")
                out.append(rendered)
                continue
            out.append(char)
            index += 1
            continue

        if char == "\\" and state in ("none", "double"):
            out.append(script[index:index + 2])
            index += 2
            at_word_start = False
            continue

        if state == "none":
            if char == "#" and at_word_start:
                state = "comment"
                out.append(char)
                index += 1
                continue
            if char == "'":
                state = "single"
                out.append(char)
                index += 1
                at_word_start = False
                continue
            if char == '"':
                state = "double"
                out.append(char)
                index += 1
                at_word_start = False
                continue
            if char == "<" and script.startswith("<<", index):
                match = _HEREDOC_PATTERN.match(script, index)
                if match is not None:
                    heredoc_strip_tabs = bool(match.group(1))
                    heredoc_terminator = match.group(3)
                    out.append(match.group(0))
                    index = match.end()
                    at_word_start = False
                    continue
            if script.startswith("{{", index):
                rendered, index = _render_at(script, index, values, substituted, "none")
                out.append(rendered)
                at_word_start = False
                continue
        elif state == "single":
            if char == "'":
                state = "none"
                out.append(char)
                index += 1
                at_word_start = False
                continue
            if script.startswith("{{", index):
                rendered, index = _render_at(script, index, values, substituted, "single")
                out.append(rendered)
                continue
        elif state == "double":
            if char == '"':
                state = "none"
                out.append(char)
                index += 1
                at_word_start = False
                continue
            if script.startswith("{{", index):
                rendered, index = _render_at(script, index, values, substituted, "double")
                out.append(rendered)
                continue

        out.append(char)
        index += 1
        at_word_start = char in " \t;|&()<>"
    else:
        pass

    text = "".join(out)
    if len(text) > MAX_RENDERED_CHARS:
        raise RenderError(
            f"The rendered script exceeds the {MAX_RENDERED_CHARS}-character limit."
        )
    return RenderResult(text=text, substituted=tuple(substituted))


def _render_at(
    script: str,
    index: int,
    values: Mapping[str, str],
    substituted: list[str],
    context: str,
) -> tuple[str, int]:
    """Render one ``{{name}}`` occurrence and return ``(text, next_index)``."""
    match = REFERENCE_PATTERN.match(script, index)
    if match is None:
        close = script.find("}}", index)
        snippet = script[index:close + 2] if close != -1 else script[index:index + 40]
        raise RenderError(
            f"Malformed variable reference {snippet!r}: expected '{{{{name}}}}' with a "
            "name of letters, digits and underscore."
        )
    name = match.group(1)
    if name not in values:
        available = ", ".join(sorted(values)) or "(none defined)"
        raise RenderError(
            f"Variable {{{{{name}}}}} is not defined for this workflow. "
            f"Defined variables: {available}."
        )
    value = values[name]
    if len(value) > MAX_VALUE_CHARS:
        raise RenderError(
            f"Variable {name!r} produced {len(value)} characters, above the "
            f"{MAX_VALUE_CHARS}-character limit."
        )
    if name not in substituted:
        substituted.append(name)
    if context == "single":
        return _quote_for_single(value), match.end()
    if context == "double":
        return _quote_for_double(value), match.end()
    if context == "comment":
        # A comment never executes, so no shell escaping is needed -- but a value
        # containing a newline would end the comment and make the rest of the
        # value execute as a command. There is no way to quote around that, so it
        # is refused instead of emitted.
        if "\n" in value or "\r" in value:
            raise RenderError(
                f"Variable {name!r} contains a newline and cannot be used inside a "
                "shell comment, because the value could terminate the comment."
            )
        return value, match.end()
    return _quote_for_bare(value), match.end()


def referenced_names(script: str) -> set[str]:
    """Names referenced by a script, for the editor's validation preview.

    Best-effort and non-raising: this only powers a hint, and it never decides
    whether a run is allowed.
    """
    return {match.group(1) for match in REFERENCE_PATTERN.finditer(script or "")}
