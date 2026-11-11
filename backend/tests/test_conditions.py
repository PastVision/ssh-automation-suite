"""Unit tests for the condition evaluator and the rule decision table.

These are pure: no database, no SSH, no event loop. They are the first line of
defence for the branching semantics, because a mistake here silently changes what
a workflow does on every host.
"""

from __future__ import annotations

import pytest

from app.models.enums import (
    ConditionLocation,
    MatchOperator,
    OutputStream,
    StepAction,
    StepDecision,
)
from app.services import conditions
from app.services.conditions import (
    Condition,
    ConditionError,
    StepRule,
    evaluate,
    split_lines,
    validate_rule,
)
from app.services.execution_runner import _decide


def cond(
    *,
    stream: OutputStream = OutputStream.STDOUT,
    location: ConditionLocation = ConditionLocation.ANYWHERE,
    operator: MatchOperator = MatchOperator.CONTAINS,
    expected: str = "ok",
    case_sensitive: bool = True,
    line_number: int | None = None,
    anchor: str | None = None,
) -> Condition:
    return Condition(
        stream=stream,
        location=location,
        operator=operator,
        expected=expected,
        case_sensitive=case_sensitive,
        line_number=line_number,
        anchor=anchor,
    )


def run(condition: Condition, stdout: str = "", stderr: str = "", truncated: bool = False):
    """Call ``evaluate`` with both streams defaulted, for terse assertions."""
    return evaluate(condition, stdout=stdout, stderr=stderr, truncated=truncated)


# --------------------------------------------------------------------------- #
# Line splitting
# --------------------------------------------------------------------------- #


def test_trailing_newline_does_not_create_a_phantom_line():
    assert split_lines("a\nb\n") == ["a", "b"]
    assert split_lines("a\nb") == ["a", "b"]


def test_crlf_is_normalised_so_lines_never_keep_a_carriage_return():
    assert split_lines("a\r\nb\r\n") == ["a", "b"]


def test_empty_text_has_no_lines():
    assert split_lines("") == []


def test_blank_interior_line_is_preserved():
    # A blank line is a real line and must keep the numbering of what follows.
    assert split_lines("a\n\nc\n") == ["a", "", "c"]


def test_vertical_tab_does_not_split_a_line():
    # str.splitlines() would split here and shift every later line number.
    assert split_lines("a\vb\nc") == ["a\vb", "c"]


# --------------------------------------------------------------------------- #
# Stream selection
# --------------------------------------------------------------------------- #


def test_stdout_and_stderr_are_separate_namespaces():
    stdout_only = run(
        cond(stream=OutputStream.STDOUT, expected="boom"),
        stdout="all good",
        stderr="boom",
    )
    assert stdout_only.matched is False

    stderr_only = run(
        cond(stream=OutputStream.STDERR, expected="boom"),
        stdout="all good",
        stderr="boom",
    )
    assert stderr_only.matched is True


def test_combined_streams_see_both():
    result = run(
        cond(stream=OutputStream.COMBINED, expected="boom"),
        stdout="all good",
        stderr="boom",
    )
    assert result.matched is True


def test_combined_does_not_glue_a_line_together():
    # Without the newline join, "abc" + "def" would fabricate the substring
    # "cd" that appears in neither stream.
    result = run(
        cond(stream=OutputStream.COMBINED, expected="cd"),
        stdout="abc",
        stderr="def",
    )
    assert result.matched is False


# --------------------------------------------------------------------------- #
# Operators and case sensitivity
# --------------------------------------------------------------------------- #


def test_contains_is_a_substring_test():
    assert run(cond(expected="ERROR"), stdout="x ERROR y").matched is True


def test_equals_requires_the_whole_selected_text():
    equals = MatchOperator.EQUALS
    assert run(cond(operator=equals, expected="ERROR"), stdout="an ERROR here").matched is False
    assert run(cond(operator=equals, expected="ERROR"), stdout="ERROR").matched is True


def test_equals_ignores_a_trailing_newline():
    """`echo OK` must equal "OK".

    The newline is how a program ends a line, not something the author can type
    into the expected value. Comparing it verbatim would make the most natural
    rule in the product silently never match.
    """
    equals = MatchOperator.EQUALS
    assert run(cond(operator=equals, expected="OK"), stdout="OK\n").matched is True
    # Only the terminator is dropped. A second newline is a real empty line, so
    # the output is no longer the one-line text "OK".
    assert run(cond(operator=equals, expected="OK"), stdout="OK\n\n").matched is False
    # Real whitespace before the terminator still counts.
    assert run(cond(operator=equals, expected="OK"), stdout="OK \n").matched is False
    assert run(cond(operator=equals, expected="OK"), stdout="\nOK").matched is False
    # And multi-line output still cannot equal a single value.
    assert run(cond(operator=equals, expected="OK"), stdout="OK\nOK\n").matched is False


def test_equals_on_a_single_line_compares_the_line_not_the_stream():
    # The point of a line/anchor location is that EQUALS becomes usable: the
    # comparison target is one line, not the whole output.
    result = run(
        cond(
            location=ConditionLocation.LINE_NUMBER,
            line_number=1,
            operator=MatchOperator.EQUALS,
            expected="ERROR",
        ),
        stdout="ERROR\nmore output\n",
    )
    assert result.matched is True


def test_case_insensitive_by_default_on_the_normalised_path():
    # case_sensitive=False is the default of the dataclass, not of this helper,
    # so ask for it explicitly to keep the intent obvious.
    assert (
        run(cond(expected="error", case_sensitive=False), stdout="ERROR").matched
        is True
    )


def test_case_sensitive_comparison_is_exact():
    assert (
        run(cond(expected="error", case_sensitive=True), stdout="ERROR").matched
        is False
    )


# --------------------------------------------------------------------------- #
# Line-number location
# --------------------------------------------------------------------------- #


def test_line_number_selects_exactly_that_line():
    text = "alpha\nbeta\ngamma\n"
    assert (
        run(
            cond(location=ConditionLocation.LINE_NUMBER, line_number=2, expected="bet"),
            stdout=text,
        ).matched
        is True
    )
    assert (
        run(
            cond(location=ConditionLocation.LINE_NUMBER, line_number=2, expected="gam"),
            stdout=text,
        ).matched
        is False
    )


def test_line_number_out_of_range_is_a_no_match_not_an_error():
    result = run(
        cond(location=ConditionLocation.LINE_NUMBER, line_number=9, expected="x"),
        stdout="only\n",
    )
    assert result.matched is False
    assert result.evaluated_line == 9


def test_line_number_condition_without_a_number_is_an_error():
    with pytest.raises(ConditionError):
        run(
            cond(location=ConditionLocation.LINE_NUMBER, line_number=None),
            stdout="x",
        )


# --------------------------------------------------------------------------- #
# Anchor location
# --------------------------------------------------------------------------- #


def test_anchor_uses_the_first_matching_line():
    text = "header\ntotal: 10\nfooter\ntotal: 99\n"
    result = run(
        cond(location=ConditionLocation.ANCHOR, anchor="total", expected="10"),
        stdout=text,
    )
    assert result.matched is True
    assert result.anchor_found is True
    assert result.evaluated_line == 2


def test_missing_anchor_is_reported_distinctly_from_a_failed_comparison():
    result = run(
        cond(location=ConditionLocation.ANCHOR, anchor="absent", expected="x"),
        stdout="header\nfooter\n",
    )
    assert result.matched is False
    assert result.anchor_found is False


def test_anchor_matching_follows_case_sensitivity():
    text = "TOTAL: 10\n"
    insensitive = run(
        cond(
            location=ConditionLocation.ANCHOR,
            anchor="total",
            expected="10",
            case_sensitive=False,
        ),
        stdout=text,
    )
    assert insensitive.anchor_found is True

    sensitive = run(
        cond(
            location=ConditionLocation.ANCHOR,
            anchor="total",
            expected="10",
            case_sensitive=True,
        ),
        stdout=text,
    )
    assert sensitive.anchor_found is False


def test_anchor_condition_without_anchor_is_an_error():
    with pytest.raises(ConditionError):
        run(
            cond(location=ConditionLocation.ANCHOR, anchor=None), stdout="x"
        )


# --------------------------------------------------------------------------- #
# Truncation
# --------------------------------------------------------------------------- #


def test_truncated_is_reported_so_a_negative_result_is_not_trusted():
    result = run(cond(expected="nope"), stdout="abc", truncated=True)
    assert result.matched is False
    assert result.truncated is True


def test_oversized_stream_is_refused_instead_of_silently_scanning_a_prefix():
    huge = "x" * (conditions.MAX_SCAN_CHARS + 1)
    with pytest.raises(ConditionError):
        run(cond(), stdout=huge)


def test_observed_excerpt_is_bounded_and_single_line():
    result = run(cond(expected="x"), stdout=("a" * 5000) + "\nx")
    assert result.observed is not None
    assert len(result.observed) <= 403
    assert "\n" not in result.observed


# --------------------------------------------------------------------------- #
# Rule validation
# --------------------------------------------------------------------------- #


def test_continue_must_not_carry_a_condition():
    with pytest.raises(ConditionError):
        validate_rule(StepRule(action=StepAction.CONTINUE, condition=cond()))


def test_conditional_action_requires_a_condition():
    with pytest.raises(ConditionError):
        validate_rule(StepRule(action=StepAction.FAIL_IF, condition=None))


def test_anywhere_rule_rejects_location_specific_fields():
    with pytest.raises(ConditionError):
        validate_rule(
            StepRule(
                action=StepAction.FAIL_IF,
                condition=cond(line_number=3),
            )
        )


def test_anchor_rule_requires_anchor_text():
    rule = StepRule(
        action=StepAction.TERMINATE_IF,
        condition=cond(location=ConditionLocation.ANCHOR, anchor=None),
    )
    with pytest.raises(ConditionError):
        validate_rule(rule)


def test_blank_expected_value_is_rejected():
    with pytest.raises(ConditionError):
        validate_rule(StepRule(action=StepAction.FAIL_IF, condition=cond(expected="   ")))


def test_valid_rule_passes_through_unchanged():
    rule = StepRule(
        action=StepAction.SKIP_NEXT_IF,
        condition=cond(location=ConditionLocation.ANCHOR, anchor="total"),
    )
    assert validate_rule(rule) is rule


# --------------------------------------------------------------------------- #
# Rule -> checksum payload
# --------------------------------------------------------------------------- #


def test_unconditional_rule_has_no_checksum_payload():
    # This is what keeps a pre-rules template's content checksum stable.
    assert StepRule().as_dict() is None


def test_conditional_rule_payload_is_complete():
    rule = StepRule(
        action=StepAction.FAIL_IF,
        condition=cond(
            location=ConditionLocation.ANCHOR, anchor="total", expected="10"
        ),
    )
    payload = rule.as_dict()
    assert payload is not None
    assert payload["action"] == "fail_if"
    assert payload["anchor"] == "total"
    assert payload["expected"] == "10"


def test_rule_payload_changes_when_only_the_condition_changes():
    first = StepRule(action=StepAction.FAIL_IF, condition=cond(expected="1"))
    second = StepRule(action=StepAction.FAIL_IF, condition=cond(expected="2"))
    assert first.as_dict() != second.as_dict()


# --------------------------------------------------------------------------- #
# Decision table
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("action", "matched", "expected"),
    [
        (StepAction.CONTINUE, False, StepDecision.CONTINUE),
        (StepAction.CONTINUE_IF, True, StepDecision.CONTINUE),
        (StepAction.CONTINUE_IF, False, StepDecision.FAIL),
        (StepAction.SKIP_NEXT_IF, True, StepDecision.SKIP_NEXT),
        (StepAction.SKIP_NEXT_IF, False, StepDecision.CONTINUE),
        (StepAction.TERMINATE_IF, True, StepDecision.TERMINATE),
        (StepAction.TERMINATE_IF, False, StepDecision.CONTINUE),
        (StepAction.FAIL_IF, True, StepDecision.FAIL),
        (StepAction.FAIL_IF, False, StepDecision.CONTINUE),
    ],
)
def test_decision_table(action, matched, expected):
    assert _decide(action, matched) is expected
