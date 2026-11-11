"""Real SSH integration tests for conditional step flow.

Every test here opens actual SSH connections to the Docker test lab and asserts
what the engine did: which steps ran, which were skipped, and why. The condition
under test always reads output the remote shell genuinely produced.
"""

from __future__ import annotations

import pytest

from app.models.enums import (
    ConditionLocation,
    ExecutionStatus,
    HostRunStatus,
    MatchOperator,
    OutputStream,
    StepAction,
    StepDecision,
    StepRunStatus,
)
from app.services import execution_state
from app.services.conditions import Condition, StepRule
from app.services.execution_runner import CancelToken, ExecutionRunner
from app.services.execution_state import HostSpec, StepSpec
from tests import lab_support as lab

pytestmark = pytest.mark.lab


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


@pytest.fixture()
def lab_env(settings, migrated_db, monkeypatch):
    resolved = lab.configure_lab_settings(monkeypatch)
    credential_id = lab.create_lab_credential(resolved)
    return resolved, credential_id


def _hosts(count: int, credential_id: int) -> list[HostSpec]:
    return [
        HostSpec(
            hostname="127.0.0.1",
            port=lab.lab_port(index),
            credential_id=credential_id,
            credential_name="lab",
        )
        for index in range(1, count + 1)
    ]


def _rule(
    action: StepAction,
    *,
    expected: str,
    stream: OutputStream = OutputStream.STDOUT,
    operator: MatchOperator = MatchOperator.CONTAINS,
    location: ConditionLocation = ConditionLocation.ANYWHERE,
    line_number: int | None = None,
    anchor: str | None = None,
    case_sensitive: bool = True,
) -> StepRule:
    return StepRule(
        action=action,
        condition=Condition(
            stream=stream,
            location=location,
            operator=operator,
            expected=expected,
            case_sensitive=case_sensitive,
            line_number=line_number,
            anchor=anchor,
        ),
    )


def _step(name: str, script: str, rule: StepRule | None = None, timeout: int = 30):
    return StepSpec(
        template_name=name,
        template_version=1,
        script=script,
        interpreter="bash",
        timeout_seconds=timeout,
        rule=rule or StepRule(),
    )


def _run(settings, execution_id: int, *, runner: ExecutionRunner | None = None):
    runner = runner or ExecutionRunner(settings)
    lab.run(runner._run_execution(execution_id, CancelToken()))
    return execution_state.get_execution(execution_id)


def _host(execution, index: int = 0):
    return sorted(execution.host_runs, key=lambda h: h.id)[index]


def _step_map(host) -> dict[int, object]:
    return {step.position: step for step in host.step_runs}


def _transcript(settings, execution) -> str:
    host = _host(execution)
    path = (
        settings.log_dir / "executions" / str(execution.id) / f"{host.id}-127.0.0.1.log"
    )
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Baseline: no rule behaves exactly as before
# --------------------------------------------------------------------------- #


def test_step_without_a_rule_runs_unconditionally(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="plain",
        steps=[
            _step("one", "echo one\n"),
            _step("two", "echo two\n"),
            _step("three", "echo three\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert host.status is HostRunStatus.SUCCEEDED
    assert [steps[p].status for p in (0, 1, 2)] == [
        StepRunStatus.SUCCEEDED
    ] * 3
    # No rule means no decision recorded, so history does not invent one.
    assert all(steps[p].decision is None for p in (0, 1, 2))


# --------------------------------------------------------------------------- #
# FAIL_IF
# --------------------------------------------------------------------------- #


def test_fail_if_matching_skips_remaining_steps_and_fails_the_host(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="fail-if",
        steps=[
            _step(
                "guard",
                "echo 'disk healthy'\n",
                _rule(StepAction.FAIL_IF, expected="nope-not-present"),
            ),
            _step(
                "guard2",
                "echo 'CRITICAL failure'\n",
                _rule(StepAction.FAIL_IF, expected="CRITICAL"),
            ),
            _step("after", "echo SHOULD-NOT-RUN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)
    transcript = _transcript(settings, execution)

    # Step 1's condition did not match, so the workflow continued to step 2.
    assert steps[0].decision is StepDecision.CONTINUE
    assert steps[0].condition_matched is False

    # Step 2 matched, so it failed the host and step 3 never ran.
    assert steps[1].decision is StepDecision.FAIL
    assert steps[1].condition_matched is True
    assert steps[2].status is StepRunStatus.SKIPPED

    assert host.status is HostRunStatus.FAILED
    assert host.failed_step_position == 1
    assert execution.status is ExecutionStatus.FAILED
    # The reason names the step that failed and the action it failed on.
    message = host.error_message or ""
    assert "'guard2'" in message
    assert "fail_if" in message
    # The rule's reasoning is written next to the output it used.
    assert "rule fail_if" in transcript
    assert "SHOULD-NOT-RUN" not in transcript


def test_fail_if_not_matching_continues(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="fail-if-nomatch",
        steps=[
            _step(
                "check",
                "echo fine\n",
                _rule(StepAction.FAIL_IF, expected="ERROR"),
            ),
            _step("next", "echo next-ran\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert steps[0].decision is StepDecision.CONTINUE
    assert steps[1].status is StepRunStatus.SUCCEEDED
    assert "next-ran" in _transcript(settings, execution)


# --------------------------------------------------------------------------- #
# TERMINATE_IF
# --------------------------------------------------------------------------- #


def test_terminate_if_stops_the_host_but_succeeds(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="terminate-if",
        steps=[
            _step(
                "already-applied",
                "echo 'state: applied'\n",
                _rule(StepAction.TERMINATE_IF, expected="applied"),
            ),
            _step("apply", "echo APPLY-SHOULD-NOT-RUN\n"),
            _step("verify", "echo VERIFY-SHOULD-NOT-RUN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)
    transcript = _transcript(settings, execution)

    # Terminating early on a matched precondition is success, not failure.
    assert host.status is HostRunStatus.TERMINATED
    assert execution.status is ExecutionStatus.SUCCEEDED
    assert execution.succeeded_host_count == 1
    assert execution.failed_host_count == 0

    assert steps[0].decision is StepDecision.TERMINATE
    assert steps[1].status is StepRunStatus.SKIPPED
    assert steps[2].status is StepRunStatus.SKIPPED
    # A skipped step has no exit code: it never connected.
    assert steps[1].exit_code is None

    assert "APPLY-SHOULD-NOT-RUN" not in transcript
    assert "terminated" in transcript
    assert host.error_message is not None and "terminate condition" in host.error_message


def test_terminated_and_succeeded_hosts_together_still_succeed(lab_env):
    """One host terminates early, another runs every step: the run succeeds."""
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="mixed-term",
        steps=[
            _step(
                "precondition",
                # The lab hosts all answer the same way, so make the condition
                # depend on the host itself to get a genuine split.
                'if [ "$(hostname)" = "' + lab.lab_hostname(1) + '" ]; then '
                "echo SKIP-ME; else echo proceed; fi\n",
                _rule(StepAction.TERMINATE_IF, expected="SKIP-ME"),
            ),
            _step("work", "echo WORK-RAN\n"),
        ],
        hosts=_hosts(2, credential_id),
    )

    execution = _run(settings, execution_id)
    hosts = sorted(execution.host_runs, key=lambda h: h.id)

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert execution.succeeded_host_count == 2
    assert execution.failed_host_count == 0

    terminated = [h for h in hosts if h.status is HostRunStatus.TERMINATED]
    succeeded = [h for h in hosts if h.status is HostRunStatus.SUCCEEDED]
    assert len(terminated) == 1
    assert len(succeeded) == 1
    assert _step_map(succeeded[0])[1].status is StepRunStatus.SUCCEEDED


# --------------------------------------------------------------------------- #
# SKIP_NEXT_IF
# --------------------------------------------------------------------------- #


def test_skip_next_if_skips_only_the_following_step(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="skip-next",
        steps=[
            _step(
                "decide",
                "echo 'already done'\n",
                _rule(StepAction.SKIP_NEXT_IF, expected="already done"),
            ),
            _step("expensive", "echo EXPENSIVE-SHOULD-NOT-RUN\n"),
            _step("always", "echo ALWAYS-RAN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)
    transcript = _transcript(settings, execution)

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert host.status is HostRunStatus.SUCCEEDED

    assert steps[0].status is StepRunStatus.SUCCEEDED
    assert steps[0].decision is StepDecision.SKIP_NEXT
    assert steps[1].status is StepRunStatus.SKIPPED
    assert steps[1].decision is StepDecision.SKIP_NEXT
    assert "previous step's skip-next condition" in (steps[1].evaluation_error or "")
    # Only the next step is skipped: execution resumes afterwards.
    assert steps[2].status is StepRunStatus.SUCCEEDED

    assert "EXPENSIVE-SHOULD-NOT-RUN" not in transcript
    assert "ALWAYS-RAN" in transcript


def test_skip_next_if_not_matching_runs_the_next_step(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="skip-next-nomatch",
        steps=[
            _step(
                "decide",
                "echo fresh\n",
                _rule(StepAction.SKIP_NEXT_IF, expected="already done"),
            ),
            _step("needed", "echo NEEDED-RAN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    steps = _step_map(_host(execution))

    assert steps[0].decision is StepDecision.CONTINUE
    assert steps[1].status is StepRunStatus.SUCCEEDED
    assert "NEEDED-RAN" in _transcript(settings, execution)


def test_skip_next_if_on_the_last_step_is_a_no_op(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="skip-next-last",
        steps=[
            _step("only", "echo done\n", _rule(StepAction.SKIP_NEXT_IF, expected="done")),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)

    # There is nothing to skip, and that is not an error.
    assert execution.status is ExecutionStatus.SUCCEEDED
    assert host.status is HostRunStatus.SUCCEEDED
    assert steps[0].status is StepRunStatus.SUCCEEDED
    assert steps[0].decision is StepDecision.SKIP_NEXT


# --------------------------------------------------------------------------- #
# CONTINUE_IF
# --------------------------------------------------------------------------- #


def test_continue_if_matching_continues(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="continue-if-ok",
        steps=[
            _step(
                "gate",
                "echo ready\n",
                _rule(StepAction.CONTINUE_IF, expected="ready"),
            ),
            _step("work", "echo WORK-RAN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    steps = _step_map(_host(execution))

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert steps[0].decision is StepDecision.CONTINUE
    assert steps[1].status is StepRunStatus.SUCCEEDED


def test_continue_if_not_matching_fails_the_host(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="continue-if-blocked",
        steps=[
            _step(
                "gate",
                "echo blocked\n",
                _rule(StepAction.CONTINUE_IF, expected="ready"),
            ),
            _step("work", "echo WORK-SHOULD-NOT-RUN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)

    assert execution.status is ExecutionStatus.FAILED
    assert host.status is HostRunStatus.FAILED
    assert steps[0].decision is StepDecision.FAIL
    assert steps[0].condition_matched is False
    assert steps[1].status is StepRunStatus.SKIPPED
    assert "WORK-SHOULD-NOT-RUN" not in _transcript(settings, execution)


# --------------------------------------------------------------------------- #
# A rule never rescues a failing command
# --------------------------------------------------------------------------- #


def test_nonzero_exit_is_final_even_when_a_condition_would_match(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="nonzero-final",
        steps=[
            _step(
                "boom",
                "echo 'this line would match'\nexit 7\n",
                # The condition would have matched, but the command failed first.
                _rule(StepAction.TERMINATE_IF, expected="this line would match"),
            ),
            _step("after", "echo AFTER-SHOULD-NOT-RUN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)

    assert execution.status is ExecutionStatus.FAILED
    assert host.status is HostRunStatus.FAILED
    assert steps[0].status is StepRunStatus.FAILED
    assert steps[0].exit_code == 7
    # The rule was never consulted, so no decision was invented for it.
    assert steps[0].decision is None
    assert steps[1].status is StepRunStatus.SKIPPED


# --------------------------------------------------------------------------- #
# Streams, line numbers and anchors over a real shell
# --------------------------------------------------------------------------- #


def test_condition_reads_stderr_when_told_to(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="stderr-branch",
        steps=[
            _step(
                "warn",
                "echo 'to stdout: fine'\necho 'to stderr: WARNING' >&2\n",
                _rule(
                    StepAction.FAIL_IF,
                    expected="WARNING",
                    stream=OutputStream.STDERR,
                ),
            ),
            _step("after", "echo AFTER-SHOULD-NOT-RUN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)

    assert host.status is HostRunStatus.FAILED
    assert steps[0].decision is StepDecision.FAIL
    assert steps[0].rule_stream is OutputStream.STDERR
    assert steps[1].status is StepRunStatus.SKIPPED


def test_stdout_condition_ignores_matching_stderr(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="stdout-only",
        steps=[
            _step(
                "warn",
                "echo 'clean'\necho 'WARNING' >&2\n",
                _rule(
                    StepAction.FAIL_IF,
                    expected="WARNING",
                    stream=OutputStream.STDOUT,
                ),
            ),
            _step("after", "echo AFTER-RAN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    steps = _step_map(_host(execution))

    # stderr is a separate namespace, so a stdout condition must not see it.
    assert execution.status is ExecutionStatus.SUCCEEDED
    assert steps[0].decision is StepDecision.CONTINUE
    assert steps[1].status is StepRunStatus.SUCCEEDED


def test_line_number_condition_selects_the_remote_line(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="line-number",
        steps=[
            _step(
                "three-lines",
                "echo alpha\necho beta\necho gamma\n",
                _rule(
                    StepAction.FAIL_IF,
                    expected="beta",
                    operator=MatchOperator.EQUALS,
                    location=ConditionLocation.LINE_NUMBER,
                    line_number=2,
                ),
            ),
            _step("after", "echo AFTER-SHOULD-NOT-RUN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)

    assert host.status is HostRunStatus.FAILED
    assert steps[0].decision is StepDecision.FAIL
    assert steps[0].evaluated_line == 2
    assert steps[0].rule_line_number == 2
    assert steps[0].observed == "beta"


def test_anchor_condition_compares_the_labelled_line(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="anchor",
        steps=[
            _step(
                "report",
                "echo 'header'\necho 'disk: 91%'\necho 'disk: 12%'\n",
                _rule(
                    StepAction.FAIL_IF,
                    expected="91%",
                    location=ConditionLocation.ANCHOR,
                    anchor="disk:",
                ),
            ),
            _step("after", "echo AFTER-SHOULD-NOT-RUN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)

    assert host.status is HostRunStatus.FAILED
    # The *first* line carrying the anchor is the one compared.
    assert steps[0].evaluated_line == 2
    assert steps[0].anchor_found is True
    assert steps[0].rule_anchor == "disk:"
    assert steps[0].observed == "disk: 91%"


def test_missing_anchor_is_recorded_as_not_found(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="anchor-missing",
        steps=[
            _step(
                "report",
                "echo 'nothing relevant'\n",
                _rule(
                    StepAction.FAIL_IF,
                    expected="10",
                    location=ConditionLocation.ANCHOR,
                    anchor="total:",
                ),
            ),
            _step("after", "echo AFTER-RAN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    steps = _step_map(_host(execution))

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert steps[0].decision is StepDecision.CONTINUE
    assert steps[0].anchor_found is False
    assert steps[1].status is StepRunStatus.SUCCEEDED


# --------------------------------------------------------------------------- #
# Per-host independence
# --------------------------------------------------------------------------- #


def test_a_rule_on_one_host_does_not_affect_another(lab_env):
    """Each host gets its own skip state; one host's branch is not global."""
    settings, credential_id = lab_env
    first = lab.lab_hostname(1)
    execution_id = execution_state.create_execution(
        workflow_name="per-host",
        steps=[
            _step(
                "decide",
                'if [ "$(hostname)" = "' + first + '" ]; then echo skip; '
                "else echo run; fi\n",
                _rule(StepAction.SKIP_NEXT_IF, expected="skip"),
            ),
            _step("maybe", "echo MAYBE-RAN-ON-$(hostname)\n"),
            _step("always", "echo ALWAYS-RAN\n"),
        ],
        hosts=_hosts(2, credential_id),
    )

    execution = _run(settings, execution_id)
    hosts = sorted(execution.host_runs, key=lambda h: h.id)

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert all(h.status is HostRunStatus.SUCCEEDED for h in hosts)

    statuses = {h.hostname + ":" + str(h.id): _step_map(h)[1].status for h in hosts}
    # Exactly one host skipped its middle step and one actually ran it.
    values = list(statuses.values())
    assert values.count(StepRunStatus.SKIPPED) == 1
    assert values.count(StepRunStatus.SUCCEEDED) == 1
    # The final step always runs on both hosts.
    assert all(_step_map(h)[2].status is StepRunStatus.SUCCEEDED for h in hosts)


def test_failed_host_does_not_stop_other_hosts(lab_env):
    """Fail-fast is per host: a rule failing one host leaves other hosts running."""
    settings, credential_id = lab_env
    first = lab.lab_hostname(1)
    execution_id = execution_state.create_execution(
        workflow_name="per-host-fail",
        steps=[
            _step(
                "guard",
                'if [ "$(hostname)" = "' + first + '" ]; then echo BLOCK; '
                "else echo ok; fi\n",
                _rule(StepAction.FAIL_IF, expected="BLOCK"),
            ),
            _step("work", "echo WORK-ON-$(hostname)\n"),
        ],
        hosts=_hosts(2, credential_id),
    )

    execution = _run(settings, execution_id)
    hosts = sorted(execution.host_runs, key=lambda h: h.id)

    failed = [h for h in hosts if h.status is HostRunStatus.FAILED]
    succeeded = [h for h in hosts if h.status is HostRunStatus.SUCCEEDED]
    assert len(failed) == 1
    assert len(succeeded) == 1

    # The blocked host's second step never ran; the other host's did.
    assert _step_map(failed[0])[1].status is StepRunStatus.SKIPPED
    assert _step_map(succeeded[0])[1].status is StepRunStatus.SUCCEEDED
    assert execution.failed_host_count == 1
    assert execution.succeeded_host_count == 1


# --------------------------------------------------------------------------- #
# Rule snapshot fidelity
# --------------------------------------------------------------------------- #


def test_rule_is_frozen_into_the_execution_snapshot(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="snapshot-rule",
        steps=[
            _step(
                "guarded",
                "echo hello\n",
                _rule(
                    StepAction.FAIL_IF,
                    expected="hello",
                    location=ConditionLocation.ANCHOR,
                    anchor="hello",
                ),
            )
        ],
        hosts=_hosts(1, credential_id),
    )

    loaded = execution_state.load_steps(execution_id)
    assert len(loaded) == 1
    rule = loaded[0].rule
    assert rule.action is StepAction.FAIL_IF
    assert rule.condition is not None
    assert rule.condition.expected == "hello"
    assert rule.condition.anchor == "hello"
    assert rule.condition.location is ConditionLocation.ANCHOR


def test_snapshot_rule_survives_the_template_being_gone(lab_env):
    """A run's rule comes from its own snapshot, not a live template row."""
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="orphan-rule",
        steps=[
            _step(
                "orphan",
                "echo done\n",
                _rule(StepAction.SKIP_NEXT_IF, expected="done"),
            ),
            _step("next", "echo NEXT-SKIPPED\n"),
        ],
        hosts=_hosts(1, credential_id),
    )
    # No template/version exists for these steps at all: they are standalone
    # StepSpecs. The engine must still honour the rule.
    execution = _run(settings, execution_id)
    steps = _step_map(_host(execution))

    assert steps[0].decision is StepDecision.SKIP_NEXT
    assert steps[1].status is StepRunStatus.SKIPPED


# --------------------------------------------------------------------------- #
# Truncation must never be read as "no match"
# --------------------------------------------------------------------------- #


def test_condition_over_an_output_larger_than_the_capture_limit_fails_the_host(
    lab_env,
):
    """A bounded prefix cannot prove absence, so a negative result is refused.

    The condition is deliberately looking for something that is *not* in the
    retained prefix. Silently answering "no match" would branch on incomplete
    data, so the engine must fail the host and say why.
    """
    settings, credential_id = lab_env
    # Comfortably past the 256 KiB retention limit.
    script = "for i in $(seq 1 20000); do echo 'padding-padding-padding-padding'; done\n"
    execution_id = execution_state.create_execution(
        workflow_name="truncated-condition",
        steps=[
            _step(
                "flood",
                script,
                _rule(StepAction.FAIL_IF, expected="NEVER-PRESENT"),
            ),
            _step("after", "echo AFTER-SHOULD-NOT-RUN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)

    assert host.status is HostRunStatus.FAILED
    assert execution.status is ExecutionStatus.FAILED
    # The command itself succeeded; it is the *evaluation* that is refused.
    assert steps[0].status is StepRunStatus.SUCCEEDED
    assert steps[0].exit_code == 0
    assert steps[0].decision is StepDecision.FAIL
    reason = steps[0].evaluation_error or ""
    assert "retention limit" in reason
    assert steps[1].status is StepRunStatus.SKIPPED
    assert "AFTER-SHOULD-NOT-RUN" not in _transcript(settings, execution)


def test_condition_finding_a_match_in_a_truncated_prefix_still_matches(lab_env):
    """Truncation only blocks a *negative* answer; a positive one is still valid."""
    settings, credential_id = lab_env
    script = "echo 'MATCH-AT-THE-START'\n"
    script += "for i in $(seq 1 20000); do echo 'padding-padding-padding-padding'; done\n"
    execution_id = execution_state.create_execution(
        workflow_name="truncated-positive",
        steps=[
            _step(
                "flood",
                script,
                _rule(StepAction.FAIL_IF, expected="MATCH-AT-THE-START"),
            ),
            _step("after", "echo AFTER-SHOULD-NOT-RUN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = _host(execution)
    steps = _step_map(host)

    assert host.status is HostRunStatus.FAILED
    assert steps[0].decision is StepDecision.FAIL
    assert steps[0].condition_matched is True
    assert steps[0].evaluation_error is None
    assert steps[1].status is StepRunStatus.SKIPPED
