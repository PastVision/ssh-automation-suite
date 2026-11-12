"""Execution runner.

Runs one workflow (an ordered list of steps) against a list of hosts.

Model, in one paragraph: each host is an independent *host workflow* that opens
its own connection, runs every step sequentially, and stops at the first failure.
Host workflows run concurrently with each other, bounded by the single global
``HostWorkflowLimiter``. The limiter is acquired once per host workflow — not per
step and not per command — so a host never releases its slot mid-run and the
20-host ceiling means what it says.

Cancellation and shutdown are cooperative: a shutdown ``CancelToken`` is checked
before each step and raced against each running command, so a host stops promptly
but *cleanly*, with its partial transcript and a persisted final state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from app.core.config import Settings, get_settings
from app.db.types import utcnow
from app.models.enums import (
    ConditionLocation,
    ExecutionStatus,
    HostRunStatus,
    OutputStream,
    StepAction,
    StepDecision,
    StepRunStatus,
    VariableSource,
)
from app.models.workflow import RunAsMethod
from app.services import (
    conditions,
    execution_state,
    run_as,
    ssh_transport,
    sudo,
    variables,
)
from app.services.concurrency import HostWorkflowLimiter, get_limiter
from app.services.execution_log import HostTranscript
from app.services.execution_state import HostSpec, StepSpec, VariableSpec
from app.services.log_broadcast import get_broadcaster
from app.services.ssh_transport import _CAPTURE_LIMIT_CHARS

logger = logging.getLogger(__name__)


class ExecutionCancelled(RuntimeError):
    """Raised inside a host workflow when cancellation was requested."""


@dataclass
class _StepOutcome:
    """Result of running one step for one host.

    ``failure`` is set when the host has already been settled; ``result`` carries
    the captured output for rule evaluation when the command exited cleanly.
    """

    failure: str | None = None
    result: ssh_transport.CommandResult | None = None
    step_run_id: int | None = None

    @property
    def ok(self) -> bool:
        return self.failure is None


@dataclass
class _HostFlow:
    """What a rule told the step loop to do next."""

    # The host reached a terminal state inside rule handling (terminate or fail).
    host_settled: bool = False
    # Position of the step to skip next, or None.
    skip_target: int | None = None


@dataclass
class CancelToken:
    """Cooperative cancellation shared by every host workflow of one execution."""

    _event: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str | None = None

    def cancel(self, reason: str = "cancelled by request") -> None:
        self.reason = reason
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def event(self) -> asyncio.Event:
        return self._event


class ExecutionRunner:
    """Owns the asyncio task set for in-flight executions."""

    def __init__(
        self,
        settings: Settings | None = None,
        limiter: HostWorkflowLimiter | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.limiter = limiter or get_limiter()
        self._tasks: dict[int, asyncio.Task[None]] = {}
        self._tokens: dict[int, CancelToken] = {}
        self._closing = False
        # Per-execution, immutable-once-set inputs. Keyed by execution id so the
        # twenty concurrent host workflows of one execution share them, while two
        # concurrent executions never see each other's values. They are set before
        # the host tasks are created and removed when the execution settles, so no
        # credential outlives the run in memory.
        self._variable_specs: dict[int, list[VariableSpec]] = {}
        self._run_as_passwords: dict[int, dict[int, str]] = {}
        # Per-execution primary sudo passwords, keyed by credential id, plus the
        # set of credentials already looked up (so "not configured" is cached too).
        # Scoped to the execution like the Run-As passwords above so one run's
        # secret cannot outlive it, and so a second run sharing a credential
        # re-reads rather than inheriting.
        self._sudo_passwords: dict[int, dict[int, str]] = {}
        self._sudo_password_loaded: dict[int, set[int]] = {}
        # Whether `sudo -n true` succeeded, keyed by (execution id, host run id).
        # Per host, not per credential: two hosts can share a credential and still
        # have different sudoers. The execution id keeps the cache bounded to the
        # run that created it.
        self._sudo_nopasswd: dict[tuple[int, int], bool] = {}

    # -- lifecycle --------------------------------------------------------- #

    @property
    def closing(self) -> bool:
        return self._closing

    def active_execution_ids(self) -> list[int]:
        return sorted(self._tasks)

    def request_cancel(self, execution_id: int, reason: str) -> bool:
        token = self._tokens.get(execution_id)
        if token is None:
            return False
        token.cancel(reason)
        return True

    def is_active(self, execution_id: int) -> bool:
        """Whether this process is currently running the execution."""
        return execution_id in self._tasks

    def _broadcast_status(
        self, execution_id: int, status: str, *, reason: str | None = None
    ) -> None:
        """Push a state change to viewers. Best-effort: never breaks a run."""
        try:
            get_broadcaster().publish_status(
                execution_id, status, reason=reason
            )
        except Exception:  # pragma: no cover - publisher is in-process and benign
            logger.exception("Could not broadcast status for execution %s", execution_id)

    def start(self, execution_id: int) -> asyncio.Task[None]:
        """Schedule an execution. Raises if shutting down."""
        if self._closing:
            raise RuntimeError("The service is shutting down and cannot accept new executions")
        if execution_id in self._tasks:
            raise RuntimeError(f"Execution {execution_id} is already running")
        token = CancelToken()
        task = asyncio.create_task(
            self._run_execution(execution_id, token), name=f"execution-{execution_id}"
        )
        self._tasks[execution_id] = task
        self._tokens[execution_id] = token
        task.add_done_callback(lambda _: self._forget(execution_id))
        return task

    def _forget(self, execution_id: int) -> None:
        self._tasks.pop(execution_id, None)
        self._tokens.pop(execution_id, None)
        # Drop plaintext Run-As passwords as soon as the execution ends: keeping
        # them cached would leave a secret in memory for the life of the process.
        self._variable_specs.pop(execution_id, None)
        self._run_as_passwords.pop(execution_id, None)
        self._sudo_passwords.pop(execution_id, None)
        self._sudo_password_loaded.pop(execution_id, None)
        for key in [key for key in self._sudo_nopasswd if key[0] == execution_id]:
            self._sudo_nopasswd.pop(key, None)

    async def shutdown(self, grace_seconds: float | None = None) -> None:
        """Stop accepting work, let host workflows finish, then cancel.

        Order matters: new executions are refused first, then in-flight work gets
        a bounded chance to complete, and only what remains is cancelled. Final
        states are persisted by each host workflow and by the execution task, so
        nothing is left RUNNING.
        """
        self._closing = True
        timeout = self.settings.shutdown_grace_seconds if grace_seconds is None else grace_seconds
        tasks = list(self._tasks.values())
        if not tasks:
            return
        logger.info("Shutdown: waiting up to %.1fs for %d execution(s)", timeout, len(tasks))
        done, pending = await asyncio.wait(tasks, timeout=timeout)
        if pending:
            logger.warning("Shutdown: cancelling %d execution(s) after grace period", len(pending))
            for execution_id in list(self._tokens):
                self.request_cancel(execution_id, "service shutdown")
            # Give the cooperative path a moment to persist, then hard-cancel.
            still = await asyncio.wait(pending, timeout=5.0)
            for task in still[1]:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                logger.error("Execution task failed during shutdown", exc_info=task.exception())

    # -- orchestration ----------------------------------------------------- #

    async def _run_execution(self, execution_id: int, token: CancelToken) -> None:
        """Run every host workflow of one execution and settle its final state."""
        try:
            try:
                steps = execution_state.load_steps(execution_id)
                host_specs = execution_state.load_host_specs(execution_id)
                self._variable_specs[execution_id] = execution_state.load_variable_specs(
                    execution_id
                )
                # Decrypted once per execution and held only in memory. Reading it
                # here (rather than per step) keeps a password out of the step specs,
                # which are persisted.
                self._run_as_passwords[execution_id] = (
                    execution_state.load_run_as_passwords(execution_id)
                )
            except Exception as exc:
                logger.exception("Could not load execution %s", execution_id)
                execution_state.finish_execution(
                    execution_id,
                    status=ExecutionStatus.FAILED,
                    error_message=f"The execution could not be started: {exc}",
                )
                return

            execution_state.mark_execution_running(execution_id)

            if not steps or not host_specs:
                reason = "no steps" if not steps else "no hosts"
                execution_state.finish_execution(
                    execution_id,
                    status=ExecutionStatus.FAILED,
                    error_message=f"The workflow has {reason} to run.",
                )
                return

            await asyncio.gather(
                *(
                    self._run_host(execution_id, host_run_id, spec, steps, token)
                    for host_run_id, spec in host_specs
                ),
                return_exceptions=True,
            )
        except asyncio.CancelledError:
            # A hard task cancellation (shutdown) bypasses the cooperative checks.
            # Marking the token first means the final state is recorded as
            # cancelled rather than failed, and orphaned hosts are closed out.
            token.cancel("service shutdown")
            raise
        finally:
            # Runs on the cooperative path, on an unexpected error, and on a hard
            # task cancellation during shutdown. Without it a cancelled task would
            # leave the execution and its hosts RUNNING forever.
            self._settle(execution_id, token)

    def _settle(self, execution_id: int, token: CancelToken) -> None:
        try:
            status = self._final_status(execution_id, token)
            # A hard cancel unwinds host workflows without settling their rows;
            # close those out first so no host is left RUNNING or PENDING under a
            # finished execution.
            if status is ExecutionStatus.CANCELLED:
                self._settle_orphan_hosts(execution_id, token.reason or "cancelled")
            execution_state.finish_execution(
                execution_id,
                status=status,
                error_message=token.reason if status is ExecutionStatus.CANCELLED else None,
            )
            # Told last, after the final state is durable: a viewer that reloads on
            # this signal must not still read RUNNING.
            self._broadcast_status(
                execution_id,
                status.value,
                reason=token.reason if status is ExecutionStatus.CANCELLED else None,
            )
            logger.info("Execution %s finished: %s", execution_id, status.value)
        except Exception:
            logger.exception("Could not persist the final state of execution %s", execution_id)

    @staticmethod
    def _settle_orphan_hosts(execution_id: int, reason: str) -> None:
        for host_run_id in _host_run_ids(execution_id):
            if execution_state.host_status(host_run_id) in (
                HostRunStatus.PENDING,
                HostRunStatus.RUNNING,
            ):
                execution_state.finish_host(
                    host_run_id,
                    status=HostRunStatus.CANCELLED,
                    error_message=f"Execution cancelled ({reason}).",
                    cancelled_steps=True,
                )

    def _final_status(self, execution_id: int, token: CancelToken) -> ExecutionStatus:
        statuses = [
            execution_state.host_status(host_run_id)
            for host_run_id in _host_run_ids(execution_id)
        ]
        if token.cancelled:
            # A cancellation is reported as such even if every host happened to
            # finish first, because the operator asked for it explicitly.
            return ExecutionStatus.CANCELLED
        if not statuses:
            return ExecutionStatus.FAILED
        # A host that stopped early because a TERMINATE_IF rule matched did
        # exactly what it was told: that is a success, not a failure.
        if all(
            s in (HostRunStatus.SUCCEEDED, HostRunStatus.TERMINATED) for s in statuses
        ):
            return ExecutionStatus.SUCCEEDED
        return ExecutionStatus.FAILED

    async def _run_host(
        self,
        execution_id: int,
        host_run_id: int,
        spec: HostSpec,
        steps: list[StepSpec],
        token: CancelToken,
    ) -> None:
        """One host workflow: acquire a slot, connect, run steps, settle."""
        if token.cancelled:
            execution_state.finish_host(
                host_run_id,
                status=HostRunStatus.CANCELLED,
                error_message="Cancelled before this host started.",
                cancelled_steps=True,
            )
            return

        async with self.limiter.slot():
            # Re-checked after queueing: it may have been cancelled while waiting
            # for a slot, and connecting for a dead run would be wasteful.
            if token.cancelled:
                execution_state.finish_host(
                    host_run_id,
                    status=HostRunStatus.CANCELLED,
                    error_message="Cancelled while waiting for a concurrency slot.",
                    cancelled_steps=True,
                )
                return
            try:
                await self._host_workflow(execution_id, host_run_id, spec, steps, token)
            except Exception as exc:
                # Safety net: an unexpected failure (log directory unwritable, a
                # database error, a bug) must never leave a host row RUNNING, or
                # the UI would show a run in progress forever.
                logger.exception(
                    "Unhandled error in host workflow %s (%s)", host_run_id, spec.hostname
                )
                self._finalise_after_crash(host_run_id, spec, exc)

    async def _host_workflow(
        self,
        execution_id: int,
        host_run_id: int,
        spec: HostSpec,
        steps: list[StepSpec],
        token: CancelToken,
    ) -> None:
        execution_state.mark_host_running(host_run_id)
        broadcaster = get_broadcaster()

        def on_chunk(text: str, start: int, end: int) -> None:
            # Synchronous on purpose: this runs inline with SSH output, so it can
            # only hand the frame to bounded queues. Publishing is never awaited.
            broadcaster.publish(execution_id, host_run_id, text, start=start, end=end)

        transcript = HostTranscript(
            execution_id=execution_id,
            host_run_id=host_run_id,
            hostname=spec.hostname,
            settings=self.settings,
            on_chunk=on_chunk,
        )
        with transcript:
            execution_state.set_host_log_path(host_run_id, transcript.relative_path)
            await self._run_steps(
                execution_id, host_run_id, spec, steps, token, transcript
            )

    async def _run_steps(
        self,
        execution_id: int,
        host_run_id: int,
        spec: HostSpec,
        steps: list[StepSpec],
        token: CancelToken,
        transcript: HostTranscript,
    ) -> None:
        conn = None
        try:
            transcript.note(
                f"connecting to {spec.hostname}:{spec.port} as credential "
                f"{spec.credential_name!r}"
            )
            try:
                client_key = ssh_transport.load_client_key(
                    spec.credential_id, None, self.settings
                )
                conn = await ssh_transport.connect(
                    host=spec.hostname,
                    port=spec.port,
                    username=spec.username,
                    client_key=client_key,
                    settings=self.settings,
                )
            except ssh_transport.SshError as exc:
                self._fail_host(host_run_id, transcript, str(exc))
                return
            except Exception as exc:  # defensive: never leave the host RUNNING
                logger.exception("Unexpected error connecting to %s", spec.hostname)
                self._fail_host(host_run_id, transcript, f"Could not connect: {exc}")
                return

            position = 0
            total = len(steps)

            # Variable discovery happens once per host, before any template, so
            # every step on this host renders with one consistent set of values.
            # A discovery failure fails the host immediately: rendering with a
            # missing value would either be refused (leaving a confusing error on
            # the first template) or, worse, silently wrong.
            values = await self._discover(
                execution_id, host_run_id, conn, token, transcript
            )
            if values is None:
                return  # discovery failed or was cancelled; host already settled

            # ``skip_this`` is the only per-host state a rule creates: a preceding
            # SKIP_NEXT_IF set it, so this step is recorded SKIPPED and never
            # connected. It lives in this coroutine's frame and is created fresh
            # per host workflow, so nothing can leak between hosts.
            skip_this: int | None = None
            while position < total:
                if token.cancelled:
                    self._cancel_host(host_run_id, transcript, token, position)
                    return
                step = steps[position]
                if skip_this == position:
                    self._skip_step(host_run_id, transcript, step, position)
                    position += 1
                    continue
                outcome = await self._run_step(
                    execution_id, host_run_id, step, position, conn, token,
                    transcript, values, spec,
                )
                if not outcome.ok:
                    return  # step failed or was cancelled; host already settled
                flow = self._apply_rule(
                    host_run_id, step, position, outcome, transcript, total
                )
                if flow.host_settled:
                    return
                skip_this = flow.skip_target
                position += 1

            transcript.footer("succeeded")
            execution_state.finish_host(host_run_id, status=HostRunStatus.SUCCEEDED)
        finally:
            if conn is not None:
                await ssh_transport.close(conn)

    async def _discover(
        self,
        execution_id: int,
        host_run_id: int,
        conn,
        token: CancelToken,
        transcript: HostTranscript,
    ) -> dict[str, str] | None:
        """Run every frozen discovery command on this host.

        Returns the discovered values, or ``None`` if discovery failed or the run
        was cancelled -- in which case the host has already been settled and the
        caller must stop.

        Values live only in this per-host dict. They are not written to the
        database, and the transcript records only lengths and a short preview so
        an operator can tell discovery worked without leaking host data into the
        shared log.
        """
        specs = self._variable_specs.get(execution_id) or []
        if not specs:
            return {}

        values: dict[str, str] = {}
        for spec in specs:
            if token.cancelled:
                self._cancel_host(host_run_id, transcript, token, 0)
                return None

            # A user-defined variable is already resolved: its value was frozen into
            # the execution at launch. Branching on ``source`` (not on "the command
            # happens to be blank") means an empty static value is still a value, and
            # nothing here opens an SSH command for it.
            if spec.source is VariableSource.USER_DEFINED:
                value = spec.static_value or ""
                values[spec.name] = value
                execution_state.record_discovery(
                    host_run_id,
                    spec.name,
                    status=StepRunStatus.SUCCEEDED,
                    duration_ms=0,
                    value_length=len(value),
                    detail=(
                        f"user-defined value, {len(value)} characters"
                        if value
                        else "user-defined empty value"
                    ),
                )
                transcript.note(f"using user-defined value for {spec.name!r}")
                continue

            started_at = utcnow()
            start = time.monotonic()
            transcript.note(f"discovering variable {spec.name!r}")
            try:
                result = await ssh_transport.run_command(
                    conn,
                    "bash -s",
                    timeout=spec.timeout_seconds or 30,
                    stdin_data=spec.discovery_command,
                    cancel_event=token.event,
                )
            except Exception as exc:
                logger.exception(
                    "Variable discovery %r on host_run %s raised", spec.name, host_run_id
                )
                duration = int((time.monotonic() - start) * 1000)
                message = f"Variable discovery {spec.name!r} failed: {exc}"
                execution_state.record_discovery(
                    host_run_id,
                    spec.name,
                    status=StepRunStatus.FAILED,
                    duration_ms=duration,
                    detail=message,
                    started_at=started_at,
                )
                transcript.note(message)
                self._fail_host(host_run_id, transcript, message)
                return None

            duration = int((time.monotonic() - start) * 1000)
            if result.cancelled:
                execution_state.record_discovery(
                    host_run_id,
                    spec.name,
                    status=StepRunStatus.SKIPPED,
                    duration_ms=duration,
                    detail="cancelled",
                    started_at=started_at,
                )
                self._cancel_host(host_run_id, transcript, token, 0)
                return None

            if result.exit_code != 0:
                detail = (
                    f"discovery command exited {result.exit_code}: "
                    f"{(result.stderr or '').strip()[:400]}"
                )
                execution_state.record_discovery(
                    host_run_id,
                    spec.name,
                    status=StepRunStatus.FAILED,
                    exit_code=result.exit_code,
                    duration_ms=duration,
                    detail=detail,
                    started_at=started_at,
                )
                message = f"Variable discovery {spec.name!r} failed: {detail}"
                transcript.note(message)
                self._fail_host(host_run_id, transcript, message)
                return None

            # Trailing newlines are dropped: a command that prints one line almost
            # always ends with a newline, and keeping it would put a line break
            # inside a quoted value for no reason.
            value = result.stdout.rstrip("\r\n")
            values[spec.name] = value
            execution_state.record_discovery(
                host_run_id,
                spec.name,
                status=StepRunStatus.SUCCEEDED,
                exit_code=0,
                duration_ms=duration,
                value_length=len(value),
                detail=f"captured {len(value)} characters" if value else "empty value",
                started_at=started_at,
            )
            transcript.note(f"discovered {spec.name!r} ({len(value)} characters)")

        return values

    async def _step_transport(
        self,
        execution_id: int,
        host_run_id: int,
        step: StepSpec,
        position: int,
        script: str,
        transcript: HostTranscript,
        spec: HostSpec,
        conn,
        token: CancelToken,
    ) -> tuple[str, str | None] | None:
        """Return ``(command, stdin_data)`` for one step, or None on failure.

        Without Run-As the step keeps its original shape: the interpreter reads the
        program from stdin, so nothing has to be quoted into a command line.

        With Run-As the command runs under ``sudo``, and stdin carries the password
        instead of the script. That is why the two paths differ: only one of them
        may use stdin for the program text.

        A ``None`` return means the step cannot be transported and the host has
        already been settled; the caller must stop. The reason is on the
        transcript, which is where the operator will read it.
        """
        if not step.run_as_username:
            return await self._plain_transport(
                execution_id, host_run_id, step, position, script, transcript,
                spec, conn, token
            )

        method = step.run_as_method or RunAsMethod.NOPASSWD
        password = None
        if method is RunAsMethod.PASSWORD:
            # ``is not None``, not a truthiness fallback: position 0 is a real block,
            # and ``or -1`` would silently look up a key that never exists.
            password = (
                (self._run_as_passwords.get(execution_id) or {}).get(
                    step.run_as_position
                )
                if step.run_as_position is not None
                else None
            )
        context = run_as.RunAsContext(
            target_username=step.run_as_username, method=method, password=password
        )

        try:
            run_as.validate_target(context.target_username)
            run_as.validate_password(context.password, context.method)
            command = run_as.wrap_script(context, step.interpreter, script)
        except run_as.RunAsError as exc:
            return self._abort_transport(
                host_run_id, step, position, transcript,
                f"Run-As to {step.run_as_username!r} is not usable: {exc}",
            )

        if step.run_as_position is not None:
            execution_state.record_transition(
                host_run_id,
                step.run_as_position,
                status=StepRunStatus.RUNNING,
                finished=False,
            )
            transcript.note(run_as.describe(context))

        # The password trails a single newline: sudo reads one line from stdin and
        # every later read sees EOF.
        stdin = f"{password}\n" if password else None
        return command, stdin

    def _settle_transition(
        self,
        host_run_id: int,
        step: StepSpec,
        position: int,
        *,
        status: StepRunStatus,
        exit_code: int | None = None,
        duration_ms: int | None = None,
        detail: str | None = None,
    ) -> None:
        """Update the Run-As transition row for the block governing this step."""
        if step.run_as_position is None:
            return
        execution_state.record_transition(
            host_run_id,
            step.run_as_position,
            status=status,
            exit_code=exit_code,
            duration_ms=duration_ms,
            detail=detail,
        )

    async def _plain_transport(
        self,
        execution_id: int,
        host_run_id: int,
        step: StepSpec,
        position: int,
        script: str,
        transcript: HostTranscript,
        spec: HostSpec,
        conn,
        token: CancelToken,
    ) -> tuple[str, str | None] | None:
        """Transport for a step that runs as the primary SSH user.

        Three shapes, chosen by what this host and script actually need:

        * **No sudo that could prompt** -- unchanged behaviour. The interpreter
          reads the program from stdin (``bash -s``), exactly as before.
        * **Passwordless sudo** -- proven by probing ``sudo -n true`` once per
          host. Also unchanged, so ``NOPASSWD`` hosts keep their original stdin
          and need no password at all.
        * **Password-based sudo** -- stdin becomes sudo's password channel, so the
          program travels in the command line (``-c '...'``) and the sudo calls
          are rewritten to ``-S``. With no password configured the step fails with
          the specified warning instead of the terminal-required error.
        """
        analysis = sudo.analyze(script)
        if not analysis.requires_password:
            return _script_command(step), script

        program = (step.interpreter or "bash").strip() or "bash"

        # Ask sudo itself rather than guessing from the script text: NOPASSWD
        # rules, `sudo -n`, and a cached credential all make "requires a
        # password" untrue, and proceeding as if they did would break hosts that
        # work today.
        if await self._passwordless_sudo(
            execution_id, host_run_id, conn, spec, token, transcript
        ):
            return _script_command(step), script

        password = self._sudo_password(execution_id, spec.credential_id)
        if password is None:
            return self._abort_transport(
                host_run_id, step, position, transcript,
                sudo.missing_password_warning(spec.hostname),
            )

        conflict = sudo.stdin_consumer(script)
        if conflict:
            return self._abort_transport(
                host_run_id, step, position, transcript,
                f"{spec.hostname}: this template runs sudo with a password and "
                f"also reads standard input (`{conflict}`). Both need the same "
                "channel, so the password cannot be delivered. Configure "
                "passwordless sudo for this account, or remove the standard-input "
                "read.",
            )

        # ``-c`` frees stdin for the password. The rewritten script is quoted so
        # the remote shell sees it as one argument, preserving every quote,
        # newline, ``$`` and pipeline exactly as authored.
        #
        # One line per sudo invocation: sudo's credential cache usually means
        # only the first one reads it, but a host with timestamps disabled reads
        # every time. Supplying exactly as many lines as there are invocations
        # covers both, because an unread line is simply never consumed.
        lines = max(1, sudo.sudo_invocation_count(script))
        return (
            f"{program} -c {_shell_single_quote(sudo.rewrite(script))}",
            f"{password}\n" * lines,
        )

    def _abort_transport(
        self,
        host_run_id: int,
        step: StepSpec,
        position: int,
        transcript: HostTranscript,
        message: str,
    ) -> None:
        """Fail a step that cannot be transported, and settle its rows.

        Without this the host stays ``RUNNING`` forever: the transport decides
        before any command is sent, so nothing later in the step path settles it.
        """
        transcript.note(message)
        if step.run_as_position is not None:
            execution_state.record_transition(
                host_run_id,
                step.run_as_position,
                status=StepRunStatus.FAILED,
                detail=message,
            )
        self._fail_host(host_run_id, transcript, message, failed_step=position)

    async def _passwordless_sudo(
        self,
        execution_id: int,
        host_run_id: int,
        conn,
        spec: HostSpec,
        token: CancelToken,
        transcript: HostTranscript,
    ) -> bool:
        """Whether ``sudo`` works here without a password. Cached per host run.

        One probe per host rather than per step: the answer cannot change during
        a run, and a probe per step would add a command to every sudo template.
        ``-n`` is deliberate -- it makes sudo fail immediately instead of waiting
        for input that will never come.
        """
        key = (execution_id, host_run_id)
        if key in self._sudo_nopasswd:
            return self._sudo_nopasswd[key]
        try:
            result = await ssh_transport.run_command(
                conn,
                "sudo -n true",
                timeout=self.settings.ssh_command_timeout_seconds,
                cancel_event=token.event,
            )
            allowed = result.exit_code == 0
        except Exception:
            # A probe failure is not a verdict on sudo; fall through to the
            # configured-password path so a transient error cannot silently
            # skip authentication entirely.
            logger.exception("sudo -n probe failed for %s", spec.hostname)
            return False
        self._sudo_nopasswd[key] = allowed
        if allowed:
            transcript.note(
                f"{spec.hostname}: sudo is available without a password "
                "(NOPASSWD); leaving sudo commands unchanged."
            )
        return allowed

    def _sudo_password(self, execution_id: int, credential_id: int) -> str | None:
        """The primary sudo password for a credential, decrypted once per run.

        ``None`` means "not configured", which is a normal state for passwordless
        hosts. The loaded set distinguishes that from "not looked up yet", so an
        unset password is not re-queried on every step of the same run.
        """
        loaded = self._sudo_password_loaded.setdefault(execution_id, set())
        if credential_id in loaded:
            return self._sudo_passwords.get(execution_id, {}).get(credential_id)
        loaded.add(credential_id)
        try:
            password = execution_state.load_primary_sudo_password(credential_id)
        except Exception:
            logger.exception(
                "Could not load the sudo password for credential %s", credential_id
            )
            return None
        if password is not None:
            self._sudo_passwords.setdefault(execution_id, {})[credential_id] = password
        return password

    def _transition_detail(
        self, step: StepSpec, exit_code: int | None, result, failed: bool
    ) -> str:
        """Explain a Run-As failure without blaming the child script for it.

        In password mode a rejected password exits 1 exactly like a normal script
        failure, so the exit code alone cannot tell them apart. sudo's own stderr
        can, and this is the one place that distinction is actionable: an operator
        seeing "authentication failed" knows to fix the credential, whereas
        "exit code 1" sends them to debug a script that never ran.

        The stderr is matched, never echoed, so no credential material is copied
        into history.
        """
        if not failed or step.run_as_username is None:
            return f"exit code {exit_code}"
        method = step.run_as_method or RunAsMethod.NOPASSWD
        stderr = (getattr(result, "stderr", "") or "").lower()
        if method is RunAsMethod.PASSWORD and (
            "incorrect password" in stderr or "no password was provided" in stderr
        ):
            return "sudo rejected the password for the primary SSH user"
        if "a password is required" in stderr or "not in the sudoers" in stderr:
            return (
                f"sudoers does not permit this account to run as "
                f"{step.run_as_username!r}"
            )
        return f"exit code {exit_code}"

    async def _run_step(
        self,
        execution_id: int,
        host_run_id: int,
        step: StepSpec,
        position: int,
        conn,
        token: CancelToken,
        transcript: HostTranscript,
        values: dict[str, str] | None = None,
        spec: HostSpec | None = None,
    ) -> _StepOutcome:
        """Run one step and return its outcome to the caller.

        The ``CommandResult`` travels back in the return value, never on ``self``:
        twenty host workflows share one runner instance, so any attribute holding
        output or a rule decision would be read by the wrong host.
        """
        step_run_id = execution_state.step_run_id(host_run_id, position)
        timeout = step.timeout_seconds or self.settings.ssh_command_timeout_seconds
        transcript.step_start(position, step.template_name, timeout)

        if step_run_id is not None:
            execution_state.mark_step_running(step_run_id)

        # Render first: a template referencing an undiscovered name must fail here,
        # before anything runs, rather than run half-substituted.
        try:
            rendered = variables.render(step.script, values or {})
        except variables.RenderError as exc:
            message = f"Step {position} ('{step.template_name}') could not be rendered: {exc}"
            transcript.note(message)
            if step_run_id is not None:
                execution_state.finish_step(
                    step_run_id,
                    status=StepRunStatus.FAILED,
                    exit_code=None,
                    duration_ms=None,
                )
            self._fail_host(host_run_id, transcript, message, failed_step=position)
            return _StepOutcome(failure=message)

        transport = await self._step_transport(
            execution_id, host_run_id, step, position, rendered.text, transcript,
            spec or HostSpec(
                hostname="this host", port=22, credential_id=-1, credential_name=""
            ),
            conn,
            token,
        )
        if transport is None:
            # The transport already failed and settled this host; a second
            # failure here would overwrite its own message.
            return _StepOutcome(failure="the step could not be transported")
        command, stdin_data = transport

        try:
            result = await ssh_transport.run_command(
                conn,
                command,
                timeout=timeout,
                stdin_data=stdin_data,
                on_stdout=transcript.write_stdout,
                on_stderr=transcript.write_stderr,
                cancel_event=token.event,
            )
        except Exception as exc:
            logger.exception("Step %s on host_run %s raised", position, host_run_id)
            message = f"Step {position} ('{step.template_name}') failed: {exc}"
            transcript.note(message)
            if step_run_id is not None:
                execution_state.finish_step(
                    step_run_id,
                    status=StepRunStatus.FAILED,
                    exit_code=None,
                    duration_ms=None,
                )
            self._settle_transition(
                host_run_id, step, position, status=StepRunStatus.FAILED, detail=message
            )
            self._fail_host(host_run_id, transcript, message, failed_step=position)
            return _StepOutcome(failure=message)

        if result.cancelled:
            transcript.note(f"step {position} cancelled")
            if step_run_id is not None:
                execution_state.finish_step(
                    step_run_id,
                    status=StepRunStatus.CANCELLED,
                    exit_code=result.exit_code,
                    duration_ms=result.duration_ms,
                )
            self._settle_transition(
                host_run_id,
                step,
                position,
                status=StepRunStatus.CANCELLED,
                exit_code=result.exit_code,
                duration_ms=result.duration_ms,
                detail="cancelled",
            )
            self._cancel_host(host_run_id, transcript, token, position)
            return _StepOutcome(failure="cancelled")

        if result.timed_out:
            message = (
                f"Step {position} ('{step.template_name}') timed out after {timeout}s."
            )
            transcript.note(message)
            if step_run_id is not None:
                execution_state.finish_step(
                    step_run_id,
                    status=StepRunStatus.FAILED,
                    exit_code=None,
                    duration_ms=result.duration_ms,
                )
            self._fail_host(host_run_id, transcript, message, failed_step=position)
            return _StepOutcome(failure=message)

        if result.exit_unavailable:
            message = f"Step {position} ('{step.template_name}') exited without a status."
            transcript.note(message)
            if step_run_id is not None:
                execution_state.finish_step(
                    step_run_id,
                    status=StepRunStatus.FAILED,
                    exit_code=None,
                    duration_ms=result.duration_ms,
                )
            self._fail_host(host_run_id, transcript, message, failed_step=position)
            return _StepOutcome(failure=message)

        exit_code = result.exit_code
        failed = exit_code != 0
        if step_run_id is not None:
            execution_state.finish_step(
                step_run_id,
                status=StepRunStatus.FAILED if failed else StepRunStatus.SUCCEEDED,
                exit_code=exit_code,
                duration_ms=result.duration_ms,
            )
        self._settle_transition(
            host_run_id,
            step,
            position,
            status=StepRunStatus.FAILED if failed else StepRunStatus.SUCCEEDED,
            exit_code=exit_code,
            duration_ms=result.duration_ms,
            detail=self._transition_detail(step, exit_code, result, failed),
        )
        transcript.step_end(
            position,
            status="failed" if failed else "succeeded",
            exit_code=exit_code,
            duration_ms=result.duration_ms,
        )

        if failed:
            # Nonzero exit is final. A conditional rule is never consulted, so a
            # matching condition can never turn a failed command into a success.
            message = (
                f"Step {position} ('{step.template_name}') exited with code {exit_code}; "
                "remaining steps were skipped."
            )
            transcript.note(message)
            self._fail_host(host_run_id, transcript, message, failed_step=position)
            return _StepOutcome(failure=message)

        return _StepOutcome(result=result, step_run_id=step_run_id)

    # -- execution rules --------------------------------------------------- #

    def _apply_rule(
        self,
        host_run_id: int,
        step: StepSpec,
        position: int,
        outcome: _StepOutcome,
        transcript: HostTranscript,
        total_steps: int,
    ) -> _HostFlow:
        """Evaluate this step's rule and act on it.

        Called only after a clean exit, so nothing here can override a nonzero
        exit, a timeout, a transport failure or a cancellation -- those paths
        returned from ``_run_step`` before this is reached.

        Evaluation is pure and synchronous: it reads the bounded capture the
        command already produced and returns a value. It never issues another SSH
        command and never awaits, so it cannot be interleaved with another host's
        work.
        """
        rule = step.rule
        if rule.action is StepAction.CONTINUE or rule.condition is None:
            return _HostFlow()

        result = outcome.result
        stream_truncated = False
        if result is not None:
            if rule.condition.stream is OutputStream.STDOUT:
                stream_truncated = result.stdout_truncated
            elif rule.condition.stream is OutputStream.STDERR:
                stream_truncated = result.stderr_truncated
            else:
                stream_truncated = result.stdout_truncated or result.stderr_truncated

        try:
            evaluation = conditions.evaluate(
                rule.condition,
                stdout=result.stdout if result else "",
                stderr=result.stderr if result else "",
                truncated=stream_truncated,
            )
        except conditions.ConditionError as exc:
            message = (
                f"Step {position} ('{step.template_name}') could not evaluate its "
                f"condition: {exc}"
            )
            self._record_outcome(
                outcome.step_run_id, rule, None, StepDecision.FAIL, str(exc)
            )
            transcript.note(message)
            self._fail_host(host_run_id, transcript, message, failed_step=position)
            return _HostFlow(host_settled=True)

        if stream_truncated and not evaluation.matched:
            # A bounded prefix cannot prove absence. Reporting "no match" here
            # would silently branch on incomplete data, so this is an explicit
            # evaluation failure instead -- the log keeps the full transcript.
            reason = (
                f"the {rule.condition.stream.value} output exceeded the "
                f"{_CAPTURE_LIMIT_CHARS}-character retention limit, so the condition "
                "could not be evaluated against the complete output"
            )
            message = f"Step {position} ('{step.template_name}') {reason}."
            self._record_outcome(
                outcome.step_run_id, rule, evaluation, StepDecision.FAIL, reason
            )
            transcript.note(message)
            self._fail_host(host_run_id, transcript, message, failed_step=position)
            return _HostFlow(host_settled=True)

        decision = _decide(rule.action, evaluation.matched)
        self._record_outcome(outcome.step_run_id, rule, evaluation, decision, None)
        self._note_decision(transcript, position, step, rule, evaluation, decision)

        if decision is StepDecision.TERMINATE:
            transcript.footer(
                f"terminated - step {position} ('{step.template_name}') met its "
                "terminate condition"
            )
            execution_state.finish_host(
                host_run_id,
                status=HostRunStatus.TERMINATED,
                failed_step_position=position,
                error_message=(
                    f"Stopped after step {position} ('{step.template_name}'): its "
                    "terminate condition matched."
                ),
            )
            return _HostFlow(host_settled=True)

        if decision is StepDecision.FAIL:
            message = (
                f"Step {position} ('{step.template_name}') failed its "
                f"{rule.action.value} condition; remaining steps were skipped."
            )
            self._fail_host(host_run_id, transcript, message, failed_step=position)
            return _HostFlow(host_settled=True)

        if decision is StepDecision.SKIP_NEXT:
            target = position + 1
            if target >= total_steps:
                # A rule that names a step which does not exist is a no-op, not a
                # failure: the author may have removed the trailing step.
                transcript.note(
                    f"step {position} matched its skip-next condition, but there is "
                    "no following step to skip"
                )
                return _HostFlow()
            return _HostFlow(skip_target=target)

        return _HostFlow()

    def _record_outcome(
        self,
        step_run_id: int | None,
        rule,
        evaluation,
        decision: StepDecision,
        evaluation_error: str | None,
    ) -> None:
        if step_run_id is None:
            return
        execution_state.record_rule_outcome(
            step_run_id,
            rule=rule,
            result=evaluation,
            decision=decision,
            evaluation_error=evaluation_error,
        )

    @staticmethod
    def _note_decision(
        transcript: HostTranscript,
        position: int,
        step: StepSpec,
        rule,
        evaluation,
        decision: StepDecision,
    ) -> None:
        """Write the reasoning into the host log, next to the output it used."""
        condition = rule.condition
        assert condition is not None  # narrowed by _apply_rule
        where = {
            ConditionLocation.ANYWHERE: "anywhere in",
            ConditionLocation.LINE_NUMBER: f"line {condition.line_number} of",
            ConditionLocation.ANCHOR: f"the first line containing {condition.anchor!r} in",
        }[condition.location]
        verb = "matched" if evaluation.matched else "did not match"
        detail = (
            "anchor not found"
            if evaluation.anchor_found is False
            else f"observed {evaluation.observed!r}"
            if evaluation.observed is not None
            else "no line selected"
        )
        transcript.note(
            f"step {position} rule {rule.action.value}: expected {verb} "
            f"{condition.operator.value} {condition.expected!r} {where} "
            f"{condition.stream.value} ({detail}) -> {decision.value}"
        )

    def _skip_step(
        self,
        host_run_id: int,
        transcript: HostTranscript,
        step: StepSpec,
        position: int,
    ) -> None:
        """Record a step that a preceding SKIP_NEXT_IF prevented from running."""
        step_run_id = execution_state.step_run_id(host_run_id, position)
        reason = (
            "Skipped: the previous step's skip-next condition matched, so this step "
            "was not run."
        )
        if step_run_id is not None:
            execution_state.skip_step(step_run_id, reason=reason)
        transcript.note(f"step {position} '{step.template_name}' skipped by rule")

    # -- settling ---------------------------------------------------------- #

    def _finalise_after_crash(
        self, host_run_id: int, spec: HostSpec, exc: Exception
    ) -> None:
        """Record an unexpected host failure without masking the original error."""
        try:
            execution_state.finish_host(
                host_run_id,
                status=HostRunStatus.FAILED,
                error_message=f"Internal error on {spec.hostname}: {exc}",
            )
        except Exception:  # pragma: no cover - the database itself is unavailable
            logger.exception("Could not record the failure of host_run %s", host_run_id)

    def _fail_host(
        self,
        host_run_id: int,
        transcript: HostTranscript,
        message: str,
        *,
        failed_step: int | None = None,
    ) -> None:
        transcript.footer(f"failed — {message}")
        execution_state.finish_host(
            host_run_id,
            status=HostRunStatus.FAILED,
            error_message=message,
            failed_step_position=failed_step,
        )

    def _cancel_host(
        self,
        host_run_id: int,
        transcript: HostTranscript,
        token: CancelToken,
        position: int,
    ) -> None:
        message = token.reason or "cancelled by request"
        transcript.footer(f"cancelled — {message}")
        # A host interrupted mid-flight is FAILED, not CANCELLED: it did not skip
        # cleanly, and its transcript is incomplete.
        execution_state.finish_host(
            host_run_id,
            status=HostRunStatus.FAILED,
            error_message=f"Execution cancelled ({message}) during step {position}.",
            failed_step_position=position,
            cancelled_steps=True,
        )

def _decide(action: StepAction, matched: bool) -> StepDecision:
    """Map (configured action, condition result) to what the engine does next.

    Kept as a pure function so every combination is testable in isolation, without
    a connection, a transcript or a database.
    """
    if action is StepAction.CONTINUE_IF:
        return StepDecision.CONTINUE if matched else StepDecision.FAIL
    if action is StepAction.SKIP_NEXT_IF:
        return StepDecision.SKIP_NEXT if matched else StepDecision.CONTINUE
    if action is StepAction.TERMINATE_IF:
        return StepDecision.TERMINATE if matched else StepDecision.CONTINUE
    if action is StepAction.FAIL_IF:
        return StepDecision.FAIL if matched else StepDecision.CONTINUE
    return StepDecision.CONTINUE


def _host_run_ids(execution_id: int) -> list[int]:
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.execution import ExecutionHostRun

    session = get_session_factory()()
    try:
        return list(
            session.scalars(
                select(ExecutionHostRun.id).where(ExecutionHostRun.execution_id == execution_id)
            ).all()
        )
    finally:
        session.close()


def _script_command(step: StepSpec) -> str:
    """Return the remote command that will receive the script on stdin.

    Feeding the body on stdin avoids shell-quoting a multi-line script into a
    command line, so a script containing quotes, ``$`` or newlines runs exactly as
    stored. ``bash -s`` reads the program from standard input.
    """
    interpreter = (step.interpreter or "bash").strip() or "bash"
    return f"{interpreter} -s"


def _shell_single_quote(value: str) -> str:
    """Quote ``value`` as one POSIX shell word.

    Single quotes suppress every expansion, so the script reaches the remote
    interpreter byte-for-byte. An embedded single quote is closed, escaped and
    reopened, which is the only sequence a single-quoted string cannot contain.
    """
    return "'" + value.replace("'", "'\\''") + "'"


RUNNER_SINGLETON: ExecutionRunner | None = None


def get_runner() -> ExecutionRunner:
    global RUNNER_SINGLETON
    if RUNNER_SINGLETON is None:
        RUNNER_SINGLETON = ExecutionRunner()
    return RUNNER_SINGLETON


def reset_runner() -> None:
    global RUNNER_SINGLETON
    RUNNER_SINGLETON = None
