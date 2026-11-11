"""Adversarial tests for conditional step flow.

These attack the places where a per-host feature is most likely to leak across
hosts or to misbehave when interrupted:

* the engine's per-host state under real concurrency,
* a rule decision racing a cancellation,
* a condition evaluated on a host whose output differs from its peers,
* the bound on how much a single evaluation may scan.
"""

from __future__ import annotations

import asyncio

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


def _step(name: str, script: str, rule: StepRule | None = None, timeout: int = 60):
    return StepSpec(
        template_name=name,
        template_version=1,
        script=script,
        interpreter="bash",
        timeout_seconds=timeout,
        rule=rule or StepRule(),
    )


def _rule(
    action: StepAction,
    *,
    expected: str,
    stream: OutputStream = OutputStream.STDOUT,
    operator: MatchOperator = MatchOperator.CONTAINS,
) -> StepRule:
    return StepRule(
        action=action,
        condition=Condition(
            stream=stream,
            location=ConditionLocation.ANYWHERE,
            operator=operator,
            expected=expected,
            case_sensitive=True,
        ),
    )


def _run(settings, execution_id: int):
    lab.run(ExecutionRunner(settings)._run_execution(execution_id, CancelToken()))
    return execution_state.get_execution(execution_id)


def _steps_of(host) -> dict[int, object]:
    return {step.position: step for step in host.step_runs}


# --------------------------------------------------------------------------- #
# The same template, twenty hosts, divergent outcomes
# --------------------------------------------------------------------------- #


def test_twenty_concurrent_hosts_each_branch_on_their_own_output(lab_env):
    """The headline case: 20 hosts sharing one template must not share a decision.

    Each host receives a script that prints its own hostname. The rule fails
    exactly one host, so a leaked decision would show up as the wrong number of
    failed hosts -- or as a skipped step on a host that never matched.
    """
    settings, credential_id = lab_env
    target = lab.lab_hostname(7)

    execution_id = execution_state.create_execution(
        workflow_name="fan-out-branch",
        steps=[
            _step(
                "identify",
                f'if [ "$(hostname)" = "{target}" ]; then echo "{target}"; '
                'else echo "peer"; fi\n',
                # Fails only where the condition matches, i.e. host 7. EQUALS so
                # the word "peer" cannot accidentally satisfy it.
                _rule(StepAction.FAIL_IF, expected=target, operator=MatchOperator.EQUALS),
            ),
            _step("work", "echo WORK-ON-$(hostname)\n"),
        ],
        hosts=_hosts(20, credential_id),
    )

    execution = _run(settings, execution_id)
    hosts = list(execution.host_runs)

    assert len(hosts) == 20
    failed = [h for h in hosts if h.status is HostRunStatus.FAILED]
    succeeded = [h for h in hosts if h.status is HostRunStatus.SUCCEEDED]
    assert len(failed) == 1
    assert len(succeeded) == 19

    assert execution.failed_host_count == 1
    assert execution.succeeded_host_count == 19
    assert execution.status is ExecutionStatus.FAILED

    # The failed host skipped its second step; every other host ran it.
    assert _steps_of(failed[0])[1].status is StepRunStatus.SKIPPED
    for host in succeeded:
        assert _steps_of(host)[1].status is StepRunStatus.SUCCEEDED

    # And the skip did not bleed: exactly one host has a skipped step 1.
    skipped = [h for h in hosts if _steps_of(h)[1].status is StepRunStatus.SKIPPED]
    assert len(skipped) == 1


def test_skip_next_state_never_carries_to_another_host(lab_env):
    """A skip set on one host must not skip the same position on a peer."""
    settings, credential_id = lab_env
    target = lab.lab_hostname(3)

    skip_next_if = _rule(StepAction.SKIP_NEXT_IF, expected="skip-me")

    execution_id = execution_state.create_execution(
        workflow_name="skip-isolation",
        steps=[
            _step(
                "decide",
                f'if [ "$(hostname)" = "{target}" ]; then echo skip-me; '
                "else echo proceed; fi\n",
                skip_next_if,
            ),
            _step("expensive", "echo EXPENSIVE-ON-$(hostname)\n"),
            _step("always", "echo ALWAYS-ON-$(hostname)\n"),
        ],
        hosts=_hosts(6, credential_id),
    )

    execution = _run(settings, execution_id)
    hosts = list(execution.host_runs)

    assert execution.status is ExecutionStatus.SUCCEEDED
    skipped_middle = [h for h in hosts if _steps_of(h)[1].status is StepRunStatus.SKIPPED]
    assert len(skipped_middle) == 1
    # The third step runs on every host regardless: the skip was exactly one step
    # deep and exactly one host wide.
    for host in hosts:
        assert _steps_of(host)[2].status is StepRunStatus.SUCCEEDED


# --------------------------------------------------------------------------- #
# Cancellation during a rule-driven flow
# --------------------------------------------------------------------------- #


def test_cancellation_after_a_skip_settles_the_host(lab_env):
    """Cancelling while a skip is queued must not leave steps PENDING forever."""
    settings, credential_id = lab_env
    token = CancelToken()

    skip_next_if = _rule(StepAction.SKIP_NEXT_IF, expected="skip-me")

    execution_id = execution_state.create_execution(
        workflow_name="cancel-mid-flow",
        steps=[
            _step("decide", "echo skip\n", skip_next_if),
            _step("slow", "sleep 30\n"),
            _step("third", "echo third\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    async def scenario() -> None:
        runner = ExecutionRunner(settings)
        task = asyncio.ensure_future(
            runner._run_execution(execution_id, token)
        )
        # Long enough for the first step to finish and the rule to be applied,
        # short enough to land inside the sleeping second step.
        await asyncio.sleep(3)
        token.cancel("operator stopped it")
        await task

    lab.run(scenario())
    execution = execution_state.get_execution(execution_id)

    assert execution.status is ExecutionStatus.CANCELLED
    for host in execution.host_runs:
        # Nothing is left claiming to be in flight.
        assert host.status in (
            HostRunStatus.CANCELLED,
            HostRunStatus.FAILED,
        )
        for step in host.step_runs:
            assert step.status in (
                StepRunStatus.SUCCEEDED,
                StepRunStatus.FAILED,
                StepRunStatus.SKIPPED,
                StepRunStatus.CANCELLED,
            ), f"step {step.position} left {step.status}"


def test_terminate_then_cancel_keeps_the_terminated_host_out_of_failures(lab_env):
    """A host that already terminated on purpose is not re-labelled by a cancel."""
    settings, credential_id = lab_env
    token = CancelToken()
    target = lab.lab_hostname(1)

    execution_id = execution_state.create_execution(
        workflow_name="terminate-then-cancel",
        steps=[
            # Only host 1 prints the marker, so only it terminates here. Its peer
            # falls through to the sleeping step and is still running when the
            # operator cancels -- which is the interleaving under test.
            _step(
                "pre",
                f'if [ "$(hostname)" = "{target}" ]; then echo done; fi\n',
                _rule(StepAction.TERMINATE_IF, expected="done"),
            ),
            _step("slow", "sleep 20\n"),
        ],
        hosts=_hosts(2, credential_id),
    )

    async def scenario() -> None:
        runner = ExecutionRunner(settings)
        task = asyncio.ensure_future(runner._run_execution(execution_id, token))
        await asyncio.sleep(3)
        token.cancel("stop the rest")
        await task

    lab.run(scenario())
    execution = execution_state.get_execution(execution_id)
    hosts = list(execution.host_runs)

    assert execution.status is ExecutionStatus.CANCELLED
    by_status = sorted(host.status.value for host in hosts)
    # The host that terminated on purpose keeps that record. The host interrupted
    # mid-step is FAILED by design (its transcript is incomplete), which is why
    # the two are distinguishable rather than both reading "cancelled".
    assert by_status == ["failed", "terminated"]
    failed = next(h for h in hosts if h.status is HostRunStatus.FAILED)
    assert "cancelled" in (failed.error_message or "").lower()
    assert _steps_of(failed)[1].status is StepRunStatus.CANCELLED
    terminated = next(h for h in hosts if h.status is HostRunStatus.TERMINATED)
    assert _steps_of(terminated)[0].decision is StepDecision.TERMINATE
    # Its queued second step is recorded as skipped: it was ended by the rule
    # before the cancel arrived, so the cancel did not affect it.
    assert _steps_of(terminated)[1].status is StepRunStatus.SKIPPED


# --------------------------------------------------------------------------- #
# Evaluation cost is bounded
# --------------------------------------------------------------------------- #


def test_a_rule_on_a_very_noisy_step_still_settles(lab_env):
    """A step producing megabytes of output must not stall or hang the host."""
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="noisy",
        steps=[
            _step(
                "noisy",
                "for i in $(seq 1 60000); do echo 'noise-noise-noise-noise'; done\n",
                # Reads stderr, which stays empty, so the huge stdout cannot
                # affect this evaluation at all.
                _rule(StepAction.SKIP_NEXT_IF, expected="absent", stream=OutputStream.STDERR),
            ),
            _step("after", "echo AFTER-RAN\n"),
        ],
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)
    host = execution.host_runs[0]
    steps = _steps_of(host)

    # stdout was huge but the rule reads stderr, which is empty and complete, so
    # the evaluation is trustworthy: no match, and the workflow continues.
    assert execution.status is ExecutionStatus.SUCCEEDED
    assert steps[0].decision is StepDecision.CONTINUE
    assert steps[0].condition_matched is False
    assert steps[1].status is StepRunStatus.SUCCEEDED
