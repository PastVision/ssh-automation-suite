"""Condition evaluation for execution rules.

This module is deliberately small and completely pure: no I/O, no database, no
shared state, no event loop. ``evaluate`` takes a rule and the text of one
stream and returns an immutable result. Everything that could leak state
between hosts or concurrent executions is therefore absent by construction --
there is nowhere for captured output, a matched anchor or a skip decision to be
kept.

Why there is no regex operator
------------------------------
``re`` is not a regular language matcher: backtracking patterns like
``(a+)+$`` take exponential time on crafted input, and user-supplied patterns
run inside the single event loop that all 20 host workflows share. A pathological
pattern would therefore stall every other host, which is a denial of service
against the whole execution. Python 3.11 has no per-match timeout, so a safe
regex would need a separately bounded subprocess. ``CONTAINS`` and ``EQUALS``
cover the stated use cases without that risk, so regex is deliberately omitted
rather than half-implemented.

Line semantics
--------------
Output is split on ``\\n`` only, and a trailing newline does not create a final
empty line: ``"a\\nb\\n"`` is two lines, not three. ``\\r\\n`` is normalised so
line contents never carry a stray carriage return. A bounded prefix of a stream
can end mid-line, which is exactly why ``EvaluationResult.truncated`` exists and
why the caller must refuse to trust a *negative* result on truncated input.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.models.enums import (
    ConditionLocation,
    MatchOperator,
    OutputStream,
    StepAction,
)

# A condition must be able to inspect *some* output. These caps bound the work a
# single evaluation can do. They are far below the transport capture limit, so a
# stream large enough to hit them is already truncated and will be reported as
# unevaluable rather than silently answered from partial data.
MAX_CONDITION_VALUE_CHARS = 4_000
MAX_ANCHOR_CHARS = 1_000
# An anchor that occurs more than this many times still uses the first match; the
# cap only bounds how much text is scanned for it.
MAX_SCAN_CHARS = 256 * 1024


class ConditionError(ValueError):
    """The rule cannot be evaluated. Fails the host; never guesses a result."""


@dataclass(frozen=True)
class Condition:
    """A validated output condition."""

    stream: OutputStream
    location: ConditionLocation
    operator: MatchOperator
    expected: str
    case_sensitive: bool = False
    # ``None`` means "not applicable" rather than "unset": the location decides
    # which of these is meaningful, and validation enforces that.
    line_number: int | None = None
    anchor: str | None = None


@dataclass(frozen=True)
class EvaluationResult:
    """Why a condition did or did not match, in terms the history can record.

    Every field is plain data so it can be persisted and rendered without
    re-reading the transcript.
    """

    matched: bool
    # ``None`` when the location does not involve an anchor.
    anchor_found: bool | None
    # 1-based line the comparison actually used, when a single line was selected.
    evaluated_line: int | None = None
    # The text that was compared, truncated for storage. Never the whole stream.
    observed: str | None = None
    # True when the inspected stream was a bounded prefix, so a non-match may
    # simply be "not captured yet". The caller must not trust a negative result.
    truncated: bool = False
    # Number of lines actually available in the inspected text.
    line_count: int = 0


def split_lines(text: str) -> list[str]:
    """Split captured output into logical lines.

    ``str.splitlines`` is not used: it also splits on ``\\v``, ``\\f``, ``\\x1c``
    and Unicode separators, so a single byte of binary noise in a log line would
    silently shift every line number after it.
    """
    if not text:
        return []
    normalised = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalised.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _normalise(value: str, case_sensitive: bool) -> str:
    return value if case_sensitive else value.casefold()


def _compare(haystack: str, needle: str, operator: MatchOperator, case_sensitive: bool) -> bool:
    left = _normalise(haystack, case_sensitive)
    right = _normalise(needle, case_sensitive)
    if operator is MatchOperator.EQUALS:
        return left == right
    return right in left


def select_stream(*, stdout: str, stderr: str, stream: OutputStream) -> tuple[str, bool]:
    """Return ``(text, combined_is_lossy)`` for the configured stream.

    ``COMBINED`` is a documented approximation. stdout and stderr are drained by
    two independent SSH channel readers, so their true interleaving is not
    recoverable: the transport sees them as separate streams and only their
    arrival order per stream is known. Concatenating stdout then stderr gives a
    deterministic, reproducible result that matches the order the transcript
    writer emits for each stream, but it is *not* byte-for-byte the order a
    terminal would have shown. Conditions that need true interleaving must match
    on a single stream; ``COMBINED`` is for "the value appears somewhere in this
    step's output".

    The second element is True when this ordering is an approximation, so the
    UI can explain the limitation rather than implying strict fidelity.
    """
    if stream is OutputStream.STDOUT:
        return stdout, False
    if stream is OutputStream.STDERR:
        return stderr, False
    return _join_streams(stdout, stderr), True


def _join_streams(stdout: str, stderr: str) -> str:
    if not stdout:
        return stderr
    if not stderr:
        return stdout
    if not stdout.endswith("\n"):
        stdout += "\n"
    return stdout + stderr


def evaluate(
    condition: Condition,
    *,
    stdout: str,
    stderr: str,
    truncated: bool = False,
) -> EvaluationResult:
    """Evaluate ``condition`` against a step's captured output.

    ``truncated`` says the captured text is a bounded prefix. It never causes a
    silent "no match": the result reports it, and a caller that cares (the
    runner) turns a negative result on truncated input into a hard error.
    """
    text, _lossy = select_stream(stdout=stdout, stderr=stderr, stream=condition.stream)
    if len(text) > MAX_SCAN_CHARS:
        raise ConditionError(
            f"The {condition.stream.value} output is larger than the {MAX_SCAN_CHARS} "
            "characters this condition may scan."
        )

    lines = split_lines(text)

    if condition.location is ConditionLocation.ANYWHERE:
        # A line terminator is a formatting artefact, not data the author can see
        # or type. Comparing it verbatim would make `echo OK` never equal "OK",
        # which is the single most likely way to write this rule by accident.
        # Exactly one terminator is dropped: "OK\n" is the one line "OK", while
        # "OK\n\n" is "OK" plus an empty line and must not compare equal to it.
        text_for_comparison = text[:-1] if text.endswith("\n") else text
        matched = _compare(
            text_for_comparison,
            condition.expected,
            condition.operator,
            condition.case_sensitive,
        )
        return EvaluationResult(
            matched=matched,
            anchor_found=None,
            evaluated_line=None,
            observed=_clip(text),
            truncated=truncated,
            line_count=len(lines),
        )

    if condition.location is ConditionLocation.LINE_NUMBER:
        number = condition.line_number
        if number is None:
            raise ConditionError("A line-number condition is missing its line number.")
        if number < 1:
            raise ConditionError("Line numbers start at 1.")
        if number > len(lines):
            # Explicitly out of range. This is a *no match*, not an error: the
            # step produced fewer lines than the rule expects, which is a normal
            # outcome the author must be able to branch on.
            return EvaluationResult(
                matched=False,
                anchor_found=None,
                evaluated_line=number,
                observed=None,
                truncated=truncated,
                line_count=len(lines),
            )
        line = lines[number - 1]
        return EvaluationResult(
            matched=_compare(
                line, condition.expected, condition.operator, condition.case_sensitive
            ),
            anchor_found=None,
            evaluated_line=number,
            observed=_clip(line),
            truncated=truncated,
            line_count=len(lines),
        )

    anchor = condition.anchor
    if not anchor:
        raise ConditionError("An anchor condition is missing its anchor text.")
    # Compare on the normalised form so case-insensitivity also applies to the
    # anchor; a case-sensitive rule matches the anchor exactly.
    needle = _normalise(anchor, condition.case_sensitive)
    for index, line in enumerate(lines, start=1):
        if needle in _normalise(line, condition.case_sensitive):
            return EvaluationResult(
                matched=_compare(
                    line, condition.expected, condition.operator, condition.case_sensitive
                ),
                anchor_found=True,
                evaluated_line=index,
                observed=_clip(line),
                truncated=truncated,
                line_count=len(lines),
            )
    return EvaluationResult(
        matched=False,
        anchor_found=False,
        evaluated_line=None,
        observed=None,
        truncated=truncated,
        line_count=len(lines),
    )


def _clip(text: str, limit: int = 400) -> str:
    """Keep a short, storable excerpt for execution history."""
    flat = text.replace("\n", "\\n").replace("\t", "\\t")
    if len(flat) <= limit:
        return flat
    return flat[:limit] + "..."


def action_requires_condition(action: StepAction) -> bool:
    return action is not StepAction.CONTINUE


@dataclass(frozen=True)
class StepRule:
    """A step's execution control: an action plus the condition it depends on."""

    action: StepAction = StepAction.CONTINUE
    condition: Condition | None = None

    def as_dict(self) -> dict[str, object] | None:
        """Canonical form for the version checksum, or None when unconditional.

        Returning ``None`` for ``CONTINUE`` is load-bearing: the checksum payload
        then stays byte-identical to the one computed before execution rules
        existed, so upgrading a database cannot make a no-op save look changed and
        bump a version. A conditional rule *is* included, because changing only a
        condition must append a version like any other content change.
        """
        if self.action is StepAction.CONTINUE or self.condition is None:
            return None
        condition = self.condition
        return {
            "action": self.action.value,
            "stream": condition.stream.value,
            "location": condition.location.value,
            "operator": condition.operator.value,
            "expected": condition.expected,
            "case_sensitive": condition.case_sensitive,
            "line_number": condition.line_number,
            "anchor": condition.anchor,
        }


def validate_rule(rule: StepRule) -> StepRule:
    """Reject incoherent rules. Called by the schema *and* the service.

    Written as one function so the API boundary and the storage layer cannot
    drift: the template editor is a second door into the same columns, and the
    execution path loads rules from a snapshot written earlier.
    """
    action = rule.action
    condition = rule.condition

    if action is StepAction.CONTINUE:
        if condition is not None:
            raise ConditionError(
                "'continue' runs the step unconditionally, so it takes no condition."
            )
        return rule

    if condition is None:
        raise ConditionError(f"The '{action.value}' action requires a condition.")

    if not condition.expected.strip():
        raise ConditionError("The expected value must not be blank.")
    if len(condition.expected) > MAX_CONDITION_VALUE_CHARS:
        raise ConditionError(
            f"The expected value must be at most {MAX_CONDITION_VALUE_CHARS} characters."
        )

    if condition.location is ConditionLocation.LINE_NUMBER:
        if condition.line_number is None:
            raise ConditionError("Choose a line number for a specific-line condition.")
        # Validated here rather than trusted from the client: a negative or zero
        # line would silently never match and look like a logic bug to the author.
        if not 1 <= condition.line_number <= 1_000_000:
            raise ConditionError("The line number must be between 1 and 1000000.")
        if condition.anchor:
            raise ConditionError("A specific-line condition cannot also set an anchor.")
    elif condition.location is ConditionLocation.ANCHOR:
        if condition.anchor is None or not condition.anchor.strip():
            raise ConditionError("Enter the text that identifies the line to check.")
        if len(condition.anchor) > MAX_ANCHOR_CHARS:
            raise ConditionError(
                f"The anchor must be at most {MAX_ANCHOR_CHARS} characters."
            )
        if condition.line_number is not None:
            raise ConditionError("An anchored condition cannot also set a line number.")
    else:
        if condition.line_number is not None or condition.anchor:
            raise ConditionError(
                "An 'anywhere' condition does not use a line number or an anchor."
            )
    return rule

