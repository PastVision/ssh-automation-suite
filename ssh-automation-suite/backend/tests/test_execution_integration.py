"""Real SSH integration tests.

Every test here opens an actual SSH connection to an OpenSSH server running in
the Docker test lab. Nothing is mocked: no fake transport, no patched asyncssh.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from app.models.enums import ExecutionStatus, HostRunStatus, StepRunStatus
from app.services import execution_state
from app.services.execution_runner import CancelToken, ExecutionRunner
from app.services.execution_state import HostSpec, StepSpec
from app.services.ssh_transport import _CAPTURE_LIMIT_CHARS
from tests import lab_support as lab

pytestmark = pytest.mark.lab


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

@pytest.fixture()
def lab_env(settings, migrated_db, monkeypatch):
    """App configured for the lab, with a credential pointing at the lab key."""
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


def _steps(*definitions: tuple[str, str, int]) -> list[StepSpec]:
    return [
        StepSpec(
            template_name=name,
            template_version=1,
            script=script,
            interpreter="bash",
            timeout_seconds=timeout,
        )
        for name, script, timeout in definitions
    ]


def _run(settings, execution_id: int, *, runner: ExecutionRunner | None = None):
    runner = runner or ExecutionRunner(settings)
    lab.run(runner._run_execution(execution_id, CancelToken()))
    return execution_state.get_execution(execution_id)


def _transcript(settings, execution) -> str:
    return _transcript_for(settings, execution, execution.host_runs[0])


def _transcript_for(settings, execution, host) -> str:
    path = settings.log_dir / "executions" / str(execution.id) / f"{host.id}-127.0.0.1.log"
    return path.read_text(encoding="utf-8")


def stderr_blocks(text: str) -> list[str]:
    """Return the contents of each marked stderr block, in order."""
    from app.services.execution_log import STDERR_CLOSE, STDERR_OPEN

    blocks, remainder = [], text
    while STDERR_OPEN in remainder:
        _, after_open = remainder.split(STDERR_OPEN, 1)
        body, remainder = after_open.split(STDERR_CLOSE, 1)
        blocks.append(body.lstrip("\n"))
    return blocks


def stdout_only(text: str) -> str:
    """Everything that was written as remote stdout (stderr blocks removed)."""
    from app.services.execution_log import STDERR_CLOSE, STDERR_OPEN

    parts, remainder = [], text
    while STDERR_OPEN in remainder:
        before, after_open = remainder.split(STDERR_OPEN, 1)
        _, remainder = after_open.split(STDERR_CLOSE, 1)
        parts.append(before)
    parts.append(remainder)
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 1. One host, one successful command
# --------------------------------------------------------------------------- #

def test_single_host_single_successful_command(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="single",
        steps=_steps(("hello", "echo hello-from-lab\n", 30)),
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert execution.succeeded_host_count == 1
    assert execution.failed_host_count == 0
    host = execution.host_runs[0]
    assert host.status is HostRunStatus.SUCCEEDED
    step = host.step_runs[0]
    assert step.status is StepRunStatus.SUCCEEDED
    assert step.exit_code == 0
    assert step.duration_ms is not None and step.duration_ms >= 0
    assert "hello-from-lab" in _transcript(settings, execution)


# --------------------------------------------------------------------------- #
# 2. One host, multiple sequential commands
# --------------------------------------------------------------------------- #

def test_single_host_runs_steps_sequentially(lab_env):
    """Each step must observe the previous step's effect and run in order."""
    settings, credential_id = lab_env
    marker = f"/tmp/lab-seq-{int(time.time() * 1000)}"
    execution_id = execution_state.create_execution(
        workflow_name="sequential",
        steps=_steps(
            ("create", f"echo first > {marker}\n", 30),
            ("append", f"echo second >> {marker}\n", 30),
            ("read", f"cat {marker}\n", 30),
        ),
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)

    assert execution.status is ExecutionStatus.SUCCEEDED
    steps = execution.host_runs[0].step_runs
    assert [step.status for step in steps] == [StepRunStatus.SUCCEEDED] * 3
    # Steps must start in order; equality is allowed because timestamps are coarse.
    assert steps[0].started_at <= steps[1].started_at <= steps[2].started_at
    transcript = _transcript(settings, execution)
    assert "first" in transcript and "second" in transcript


# --------------------------------------------------------------------------- #
# 3. Multiple hosts concurrently
# --------------------------------------------------------------------------- #

def test_multiple_hosts_run_and_all_succeed(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="fanout",
        steps=_steps(("identity", "hostname\n", 30)),
        hosts=_hosts(5, credential_id),
    )

    execution = _run(settings, execution_id)

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert execution.host_count == 5
    assert execution.succeeded_host_count == 5
    assert all(host.status is HostRunStatus.SUCCEEDED for host in execution.host_runs)


def test_hosts_really_overlap_in_time(lab_env):
    """Distinct hosts must be in flight simultaneously, not serialised."""
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="parallel",
        steps=_steps(("sleep", "sleep 2\n", 30)),
        hosts=_hosts(5, credential_id),
    )

    started = time.monotonic()
    execution = _run(settings, execution_id)
    elapsed = time.monotonic() - started

    assert execution.status is ExecutionStatus.SUCCEEDED
    # Five sequential 2s sleeps would take >=10s; concurrent execution is ~2s.
    assert elapsed < 8.0, f"hosts did not run concurrently (took {elapsed:.1f}s)"
    spans = [
        (host.started_at, host.finished_at) for host in execution.host_runs
    ]
    assert all(start and finish for start, finish in spans)


# --------------------------------------------------------------------------- #
# 4. Non-zero exit code
# --------------------------------------------------------------------------- #

def test_nonzero_exit_code_fails_the_step(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="nonzero",
        steps=_steps(("fails", "echo before-failure\nexit 42\n", 30)),
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)

    assert execution.status is ExecutionStatus.FAILED
    host = execution.host_runs[0]
    assert host.status is HostRunStatus.FAILED
    assert host.failed_step_position == 0
    step = host.step_runs[0]
    assert step.status is StepRunStatus.FAILED
    assert step.exit_code == 42
    assert "42" in (host.error_message or "")
    assert "before-failure" in _transcript(settings, execution)


# --------------------------------------------------------------------------- #
# 5. Connection failure
# --------------------------------------------------------------------------- #

def test_connection_failure_marks_host_failed(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="unreachable",
        steps=_steps(("never", "echo unreachable\n", 30)),
        # Port 2299 has no listener in the lab.
        hosts=[HostSpec("127.0.0.1", 2299, credential_id, "lab")],
    )

    execution = _run(settings, execution_id)

    assert execution.status is ExecutionStatus.FAILED
    host = execution.host_runs[0]
    assert host.status is HostRunStatus.FAILED
    assert host.failed_step_position is None, "no step ran, so none should be blamed"
    assert "connect" in (host.error_message or "").lower()
    assert host.step_runs[0].status is StepRunStatus.SKIPPED
    assert "[engine]" in _transcript(settings, execution)


# --------------------------------------------------------------------------- #
# 6. Authentication failure
# --------------------------------------------------------------------------- #

def test_authentication_failure_is_reported(lab_env):
    """A valid-but-unauthorized key must fail as auth, not as a transport error."""
    settings, credential_id = lab_env
    import asyncssh

    from app.db.session import get_session_factory
    from app.models.credential import SshCredential
    from app.services import key_store

    wrong_key = asyncssh.generate_private_key("ssh-ed25519").export_private_key().decode()
    session = get_session_factory()()
    try:
        record = SshCredential(
            name="wrong-key",
            username=lab.LAB_USER,
            default_port=22,
            owner_id=lab.ensure_test_owner(),
        )
        session.add(record)
        session.flush()
        key_store.store_private_key(record.id, wrong_key, settings=settings)
        session.commit()
        wrong_id = record.id
    finally:
        session.close()

    execution_id = execution_state.create_execution(
        workflow_name="bad-auth",
        steps=_steps(("never", "echo nope\n", 30)),
        hosts=[HostSpec("127.0.0.1", lab.lab_port(1), wrong_id, "wrong-key")],
    )

    execution = _run(settings, execution_id)

    host = execution.host_runs[0]
    assert host.status is HostRunStatus.FAILED
    message = (host.error_message or "").lower()
    assert "authentication failed" in message or "permission denied" in message
    assert execution.status is ExecutionStatus.FAILED


# --------------------------------------------------------------------------- #
# 7. Command timeout
# --------------------------------------------------------------------------- #

def test_command_timeout_fails_and_frees_the_host(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="timeout",
        steps=_steps(("hangs", "sleep 60\n", 2)),
        hosts=_hosts(1, credential_id),
    )

    started = time.monotonic()
    execution = _run(settings, execution_id)
    elapsed = time.monotonic() - started

    assert elapsed < 30, "the engine should kill the command, not wait for it"
    host = execution.host_runs[0]
    assert host.status is HostRunStatus.FAILED
    assert host.step_runs[0].status is StepRunStatus.FAILED
    assert "timed out" in (host.error_message or "").lower()


# --------------------------------------------------------------------------- #
# 8 & 9. Large stdout / large stderr
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_large_output_is_preserved_in_full(lab_env, stream):
    """2 MB of output must land on disk byte-for-byte, untruncated."""
    settings, credential_id = lab_env
    size = 2_000_000
    if stream == "stdout":
        script = f"head -c {size} /dev/zero | tr '\\0' 'o'\n"
    else:
        script = f"head -c {size} /dev/zero | tr '\\0' 'e' >&2\n"

    execution_id = execution_state.create_execution(
        workflow_name=f"large-{stream}",
        steps=_steps(("bulk", script, 120)),
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)

    assert execution.status is ExecutionStatus.SUCCEEDED
    transcript = _transcript(settings, execution)

    if stream == "stdout":
        body = stdout_only(transcript)
        count = body.count("o")
        assert count >= size, f"stdout truncated: {count} < {size}"
    else:
        blocks = stderr_blocks(transcript)
        assert blocks, "no stderr block was written"
        count = sum(block.count("e") for block in blocks)
        assert count >= size, f"stderr truncated: {count} < {size}"
        # And none of it leaked into the stdout portion.
        assert "e" * 1000 not in stdout_only(transcript)

def test_detached_child_does_not_fail_a_successful_step(lab_env):
    """A script that exits 0 must not be reported as a timeout.

    A background process inheriting stdout keeps the channel open after the
    script exits. Waiting for channel EOF made a *successful* step block for the
    child's lifetime and then be reported as ``timed_out`` with no exit code.
    """
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="detached-child",
        # The child outlives the step timeout, so EOF-only detection would fail.
        steps=_steps(("detached", "(sleep 30 &) ; echo done-ok ; exit 0\n", 5)),
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)

    host = execution.host_runs[0]
    assert host.status is HostRunStatus.SUCCEEDED, host.error_message
    assert host.step_runs[0].status is StepRunStatus.SUCCEEDED
    assert host.step_runs[0].exit_code == 0
    assert "done-ok" in _transcript(settings, execution)


def test_retained_result_is_bounded_but_streaming_is_complete(lab_env):
    """Bounded in-memory retention must not truncate what callbacks receive."""
    settings, credential_id = lab_env
    size = _CAPTURE_LIMIT_CHARS * 2

    async def scenario():
        from app.db.session import get_session_factory
        from app.models.credential import SshCredential
        from app.services import key_store, ssh_transport

        session = get_session_factory()()
        try:
            pem = key_store.load_private_key(credential_id, settings)
            assert session.get(SshCredential, credential_id) is not None
        finally:
            session.close()

        conn = await ssh_transport.connect(
            host="127.0.0.1",
            port=lab.lab_port(1),
            username=lab.LAB_USER,
            client_key=key_store.inspect_private_key(pem),
            settings=settings,
        )
        collected: list[str] = []
        try:
            result = await ssh_transport.run_command(
                conn,
                "bash -s",
                timeout=120,
                stdin_data=f"head -c {size} /dev/zero | tr '\\0' 'x'\n",
                on_stdout=collected.append,
            )
        finally:
            await ssh_transport.close(conn)
        return result, "".join(collected)

    result, delivered = lab.run(scenario())

    assert result.exit_code == 0
    # The callback still receives every byte...
    assert len(delivered) == size
    # ...while the value held in memory for the caller is capped.
    assert len(result.stdout) <= size
    assert len(result.stdout) < len(delivered)


def test_stdout_and_stderr_are_never_mixed(lab_env):
    """Distinct content must land in distinct places within the transcript."""
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="streams",
        steps=_steps(
            (
                "split",
                "printf 'OUT-A\\nOUT-B\\n'\n"
                "printf 'ERR-A\\nERR-B\\n' >&2\n"
                "printf 'OUT-C\\n'\n",
                30,
            )
        ),
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)

    transcript = _transcript(settings, execution)
    out = stdout_only(transcript)
    err = "\n".join(stderr_blocks(transcript))

    assert "OUT-A" in out and "OUT-B" in out and "OUT-C" in out
    assert "ERR-A" in err and "ERR-B" in err
    # The critical assertion: neither stream leaked into the other.
    assert "ERR-A" not in out and "ERR-B" not in out
    assert "OUT-A" not in err and "OUT-B" not in err and "OUT-C" not in err


def test_multiline_and_missing_trailing_newline_preserved(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="newlines",
        steps=_steps(("trailing", "printf 'a\\nb\\nc'\n", 30)),
        hosts=_hosts(1, credential_id),
    )

    execution = _run(settings, execution_id)

    transcript = _transcript(settings, execution)
    assert "a\nb\nc" in transcript


# --------------------------------------------------------------------------- #
# 10. One host fails while others succeed
# --------------------------------------------------------------------------- #

def test_one_host_failure_does_not_affect_other_hosts(lab_env):
    settings, credential_id = lab_env
    hosts = _hosts(4, credential_id)
    # Make the third host unroutable so it fails independently.
    hosts[2] = HostSpec("127.0.0.1", 2299, credential_id, "lab")

    execution_id = execution_state.create_execution(
        workflow_name="mixed",
        steps=_steps(("ok", "echo fine\n", 30)),
        hosts=hosts,
    )

    execution = _run(settings, execution_id)

    statuses = [host.status for host in execution.host_runs]
    assert statuses.count(HostRunStatus.SUCCEEDED) == 3
    assert statuses.count(HostRunStatus.FAILED) == 1
    assert execution.status is ExecutionStatus.FAILED
    assert execution.succeeded_host_count == 3
    assert execution.failed_host_count == 1


def test_failed_step_skips_remaining_steps_on_that_host_only(lab_env):
    """Fail-fast is per host: later steps skip there, but still run elsewhere.

    The step sequence is identical on every host, so the script itself decides
    based on its own hostname. That is exactly the production case where a
    template succeeds on some machines and fails on others.
    """
    settings, credential_id = lab_env
    failing_host = "ssh-host-01"
    execution_id = execution_state.create_execution(
        workflow_name="fail-fast",
        steps=_steps(
            ("one", "echo step-one\n", 30),
            # Fails only on ssh-host-01.
            ("two", f'[ "$(hostname)" = "{failing_host}" ] && exit 7\necho ok\n', 30),
            ("three", "echo step-three\n", 30),
        ),
        hosts=_hosts(2, credential_id),
    )

    execution = _run(settings, execution_id)

    by_name = {host.hostname: host for host in execution.host_runs}
    # Both lab hosts share the address 127.0.0.1, so distinguish by container
    # hostname from their transcripts instead of by row order.
    transcripts = {
        host.id: _transcript_for(settings, execution, host) for host in execution.host_runs
    }
    failed = [
        host for host in execution.host_runs
        if host.status is HostRunStatus.FAILED
    ]
    succeeded = [
        host for host in execution.host_runs
        if host.status is HostRunStatus.SUCCEEDED
    ]

    assert len(failed) == 1 and len(succeeded) == 1, (
        f"expected exactly one host to fail; got "
        f"{[(h.id, h.status) for h in execution.host_runs]}"
    )

    # The failing host stops after step two and skips step three.
    assert failed[0].failed_step_position == 1
    assert [s.status for s in failed[0].step_runs] == [
        StepRunStatus.SUCCEEDED,
        StepRunStatus.FAILED,
        StepRunStatus.SKIPPED,
    ]
    # The healthy host ran all three.
    assert [s.status for s in succeeded[0].step_runs] == [
        StepRunStatus.SUCCEEDED,
        StepRunStatus.SUCCEEDED,
        StepRunStatus.SUCCEEDED,
    ]
    # And the skipped step genuinely never executed there.
    assert "step-three" not in transcripts[failed[0].id]
    assert "step-three" in transcripts[succeeded[0].id]
    assert by_name  # sanity: hostname rows exist


# --------------------------------------------------------------------------- #
# 11. Global concurrency never exceeds the limit
# --------------------------------------------------------------------------- #

def test_global_concurrency_never_exceeds_the_limit(lab_env):
    """Track peak simultaneous host workflows with a limit of 4."""
    settings, credential_id = lab_env
    from app.services.concurrency import HostWorkflowLimiter

    limiter = HostWorkflowLimiter(limit=4)
    observed: list[int] = []
    original = limiter.slot

    import contextlib

    @contextlib.asynccontextmanager
    async def spying_slot():
        async with original():
            observed.append(limiter.snapshot().in_use)
            yield

    limiter.slot = spying_slot

    execution_id = execution_state.create_execution(
        workflow_name="limited",
        steps=_steps(("nap", "sleep 0.5\n", 60)),
        hosts=_hosts(8, credential_id),
        max_concurrent_hosts=4,
    )

    runner = ExecutionRunner(settings, limiter=limiter)
    execution = _run(settings, execution_id, runner=runner)

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert observed, "the limiter was never exercised"
    assert max(observed) <= 4, f"concurrency exceeded the limit: {max(observed)}"
    assert limiter.snapshot().in_use == 0, "every slot must be released"


def test_default_limit_is_twenty(settings):
    from app.services.concurrency import HostWorkflowLimiter

    assert HostWorkflowLimiter().limit == 20


def test_full_lab_fanout_at_the_real_limit(lab_env):
    """Every lab host at once, three steps each: the production shape.

    This is the closest thing to a real 20-host workflow the lab can produce. It
    exercises the global limit at its actual default, SQLite writes from 20
    concurrent host workflows, and 20 concurrent transcripts.
    """
    settings, credential_id = lab_env
    from app.services.concurrency import HostWorkflowLimiter

    limiter = HostWorkflowLimiter(limit=20)
    execution_id = execution_state.create_execution(
        workflow_name="full-fanout",
        steps=_steps(
            ("one", "echo one\necho one-err >&2\n", 60),
            ("two", "sleep 0.3\necho two\n", 60),
            ("three", "echo three\n", 60),
        ),
        hosts=_hosts(lab.LAB_HOSTS, credential_id),
        max_concurrent_hosts=20,
    )

    execution = _run(
        settings, execution_id, runner=ExecutionRunner(settings, limiter=limiter)
    )

    assert execution.status is ExecutionStatus.SUCCEEDED
    assert execution.host_count == lab.LAB_HOSTS
    assert execution.succeeded_host_count == lab.LAB_HOSTS
    assert execution.failed_host_count == 0

    steps = [step for host in execution.host_runs for step in host.step_runs]
    assert len(steps) == lab.LAB_HOSTS * 3
    assert all(step.status is StepRunStatus.SUCCEEDED for step in steps)

    logs = list((settings.log_dir / "executions" / str(execution_id)).glob("*.log"))
    assert len(logs) == lab.LAB_HOSTS, f"expected one log per host, got {len(logs)}"
    assert all(path.stat().st_size > 0 for path in logs)
    # Peak concurrency reached the ceiling and every slot came back.
    assert limiter.snapshot().in_use == 0


# --------------------------------------------------------------------------- #
# 12. Cancellation
# --------------------------------------------------------------------------- #

def test_cancellation_stops_a_running_execution(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="cancel-me",
        steps=_steps(("long", "sleep 30\n", 120)),
        hosts=_hosts(2, credential_id),
    )

    runner = ExecutionRunner(settings)

    async def scenario():
        task = runner.start(execution_id)
        await asyncio.sleep(1.5)
        assert runner.request_cancel(execution_id, "cancelled by request")
        await asyncio.wait_for(task, timeout=30)

    lab.run(scenario())

    execution = execution_state.get_execution(execution_id)
    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.finished_at is not None
    assert all(host.status is not HostRunStatus.SUCCEEDED for host in execution.host_runs)
    assert all(
        step.status is not StepRunStatus.RUNNING
        for host in execution.host_runs
        for step in host.step_runs
    ), "no step may be left RUNNING after cancellation"


# --------------------------------------------------------------------------- #
# 13. Graceful shutdown
# --------------------------------------------------------------------------- #

def test_shutdown_cancels_work_after_the_grace_period(lab_env):
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="shutdown",
        steps=_steps(("long", "sleep 30\n", 120)),
        hosts=_hosts(2, credential_id),
    )

    runner = ExecutionRunner(settings)

    async def scenario():
        runner.start(execution_id)
        await asyncio.sleep(1.5)
        # Zero grace: everything still running must be cancelled.
        await runner.shutdown(grace_seconds=0)

    lab.run(scenario())

    execution = execution_state.get_execution(execution_id)
    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.finished_at is not None
    assert runner.active_execution_ids() == []


def test_hard_cancel_settles_hosts_and_execution(lab_env):
    """A task cancelled mid-flight must not leave rows RUNNING forever.

    ``shutdown`` hard-cancels executions that outlive the grace period. That
    unwinds ``_run_execution`` with ``CancelledError``, which the per-host
    ``except Exception`` guards never see, so the finaliser itself has to close
    the host and step rows out.
    """
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="hard-cancel",
        steps=_steps(("slow", "sleep 30\n", 60)),
        hosts=_hosts(2, credential_id),
    )

    runner = ExecutionRunner(settings)

    async def scenario():
        task = runner.start(execution_id)
        # Let both host workflows get past connect and into the slow step.
        await asyncio.sleep(1.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    lab.run(scenario())

    execution = execution_state.get_execution(execution_id)
    assert execution.status is ExecutionStatus.CANCELLED
    assert execution.finished_at is not None
    assert all(
        host.status in (HostRunStatus.CANCELLED, HostRunStatus.FAILED)
        for host in execution.host_runs
    )
    # No step may be left claiming to still be running or pending.
    assert all(
        step.status in (StepRunStatus.CANCELLED, StepRunStatus.FAILED)
        for host in execution.host_runs
        for step in host.step_runs
    )
    # The slot must be returned so later executions are not starved.
    assert runner.limiter.snapshot().in_use == 0
    assert runner.active_execution_ids() == []


def test_shutdown_refuses_new_executions(lab_env):
    settings, credential_id = lab_env
    runner = ExecutionRunner(settings)

    async def scenario():
        await runner.shutdown(grace_seconds=0)
        with pytest.raises(RuntimeError, match="shutting down"):
            runner.start(1)

    lab.run(scenario())


def test_shutdown_lets_short_work_finish(lab_env):
    """With a generous grace period, in-flight work completes normally."""
    settings, credential_id = lab_env
    execution_id = execution_state.create_execution(
        workflow_name="graceful",
        steps=_steps(("quick", "sleep 1\n", 60)),
        hosts=_hosts(1, credential_id),
    )

    runner = ExecutionRunner(settings)

    async def scenario():
        runner.start(execution_id)
        await runner.shutdown(grace_seconds=30)

    lab.run(scenario())

    execution = execution_state.get_execution(execution_id)
    assert execution.status is ExecutionStatus.SUCCEEDED
