"""SSH transport.

Owns every interaction with asyncssh so the executor above it never touches the
library, and so the security policy for host keys and private keys lives in one
place.

Three properties are load-bearing:

1. **Private keys never leave this module in plaintext.** ``load_private_key``
   returns an ``asyncssh.SSHKey`` that is only ever handed to ``asyncssh.connect``.
   No caller receives the PEM text, and no exception raised here embeds it.
2. **Host keys are verified.** Verification uses ``known_hosts``; the only way to
   skip it is an explicit opt-in setting that is refused in production (see
   ``Settings._guard_host_key_policy``).
3. **Streams stay separate.** Remote stdout and stderr are drained concurrently
   into distinct callbacks. Draining them sequentially would deadlock as soon as
   both exceed the channel window, and merging them would destroy the distinction
   the execution history depends on.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import asyncssh

from app.core.config import Settings, get_settings
from app.services import key_store

logger = logging.getLogger(__name__)

# 64 KiB matches asyncssh's internal window; probing showed read(65536) yields
# chunks up to exactly that size instead of coalescing the whole stream.
_READ_CHUNK = 65536
# How long a killed command gets to report its status before we give up on it.
_KILL_GRACE_SECONDS = 5.0
# How long to keep draining output after the remote process has exited. A
# detached grandchild that inherits stdout keeps the channel open indefinitely,
# so waiting for EOF would report a *successful* script as a timeout.
_EXIT_DRAIN_GRACE_SECONDS = 2.0
# Upper bound on the text retained in ``CommandResult``. Callers stream the full
# output through the callbacks, so this only bounds what is held for convenience;
# without it a 1 GB print would be pinned in memory for the whole run.
_CAPTURE_LIMIT_CHARS = 256 * 1024
# Exit status is polled rather than awaited; 10 ms is far below any user-visible
# latency while costing nothing measurable per command.
_EXIT_POLL_SECONDS = 0.01


class _Capture:
    """Bounded text collected from one stream, for ``CommandResult``."""

    __slots__ = ("chunks", "size", "truncated")

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.size = 0
        # True once text was dropped. A condition evaluator must not conclude
        # "no match" from a prefix, so this is surfaced on ``CommandResult``
        # rather than being silently ignored.
        self.truncated = False

    def text(self) -> str:
        return "".join(self.chunks)


class SshError(RuntimeError):
    """Base class for transport failures. Messages never contain key material."""


class SshConnectError(SshError):
    """Could not open the connection (DNS, refused, handshake, host key, auth)."""


class SshHostKeyError(SshConnectError):
    """The server presented a host key that does not match known_hosts."""


class SshCommandTimeout(SshError):
    """The remote command exceeded its timeout and was killed."""


class SshCancelled(SshError):
    """The command was cancelled before it completed."""


@dataclass
class CommandResult:
    exit_code: int | None
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False
    # Set when the command ran but the exit status could not be determined.
    exit_unavailable: bool = False
    # True when the retained text is a prefix and later output was dropped. A
    # condition must not be evaluated against a prefix without saying so.
    stdout_truncated: bool = False
    stderr_truncated: bool = False

    @property
    def truncated(self) -> bool:
        return self.stdout_truncated or self.stderr_truncated


def _describe(exc: BaseException) -> str:
    """Render an exception without leaking anything key-related.

    asyncssh errors are generally safe, but a bad key import can echo fragments of
    the key text, so the message is filtered for PEM markers before it is stored
    or logged.
    """
    text = str(exc) or exc.__class__.__name__
    if "PRIVATE KEY" in text:
        return exc.__class__.__name__
    return text


def load_client_key(credential_id: int, passphrase: str | None,
                    settings: Settings | None = None) -> asyncssh.SSHKey:
    """Decrypt and parse a stored key, returning an object asyncssh can use.

    A ``client_keys`` argument must be a path or an ``SSHKey``; passing PEM text
    makes asyncssh treat the text as a *filename* and fail with a confusing
    ``OSError``. Parsing here also means the plaintext never travels further.
    """
    settings = settings or get_settings()
    try:
        pem = key_store.load_private_key(credential_id, settings)
    except FileNotFoundError as exc:
        raise SshConnectError(
            f"No private key is stored for credential {credential_id}."
        ) from exc
    try:
        return key_store.inspect_private_key(pem, passphrase)
    except key_store.InvalidPrivateKeyError as exc:
        raise SshConnectError(
            f"The stored private key for credential {credential_id} could not be parsed."
        ) from exc


# Sentinel meaning "do not pass known_hosts at all", so asyncssh uses its own
# default (the user's ~/.ssh/known_hosts). Verified behaviour: omitting the
# argument rejects unknown host keys, while known_hosts=None accepts anything.
_USE_DEFAULT_KNOWN_HOSTS = object()
# Sentinel meaning verification is deliberately disabled. Lab/test use only.
_ACCEPT_ANY_HOST_KEY = object()


def build_known_hosts(settings: Settings | None = None):
    """Resolve the host-key verification policy for ``asyncssh.connect``.

    Returns one of:
      * ``_USE_DEFAULT_KNOWN_HOSTS`` — verify against ``~/.ssh/known_hosts``
        (the default, so verification is never accidentally off).
      * an explicit path string — verify against that file.
      * ``_ACCEPT_ANY_HOST_KEY`` — verification deliberately disabled.

    ``asyncssh`` treats ``known_hosts=None`` as "accept any host key" and a bare
    string as a *filename* (never as entry text), so the disabled case must be
    represented by a distinct sentinel rather than ``None``.
    """
    settings = settings or get_settings()
    if settings.ssh_allow_unknown_host_keys:
        return _ACCEPT_ANY_HOST_KEY
    if settings.ssh_known_hosts_file is not None:
        return str(settings.ssh_known_hosts_file)
    return _USE_DEFAULT_KNOWN_HOSTS


async def connect(
    *,
    host: str,
    port: int,
    username: str,
    client_key: asyncssh.SSHKey,
    settings: Settings | None = None,
) -> asyncssh.SSHClientConnection:
    """Open an authenticated connection, or raise SshConnectError."""
    settings = settings or get_settings()
    known_hosts = build_known_hosts(settings)
    kwargs: dict[str, object] = {
        "username": username,
        "client_keys": [client_key],
        "connect_timeout": settings.ssh_connect_timeout_seconds,
        # Only key auth is supported by this product; do not silently fall back.
        "preferred_auth": "publickey",
        "agent_path": None,
    }
    if known_hosts is _ACCEPT_ANY_HOST_KEY:
        kwargs["known_hosts"] = None
        logger.warning(
            "Host key verification is DISABLED for %s:%s (ssh_allow_unknown_host_keys). "
            "This is only appropriate for the disposable test lab.",
            host, port,
        )
    elif known_hosts is not _USE_DEFAULT_KNOWN_HOSTS:
        kwargs["known_hosts"] = known_hosts
    try:
        return await asyncssh.connect(host, port=port, **kwargs)
    except asyncssh.HostKeyNotVerifiable as exc:
        raise SshHostKeyError(
            f"Host key verification failed for {host}:{port}: {_describe(exc)}"
        ) from exc
    except asyncssh.PermissionDenied as exc:
        raise SshConnectError(
            f"Authentication failed for {username}@{host}:{port}."
        ) from exc
    except (TimeoutError, asyncssh.Error, OSError) as exc:
        raise SshConnectError(f"Could not connect to {host}:{port}: {_describe(exc)}") from exc


async def run_command(
    conn: asyncssh.SSHClientConnection,
    command: str,
    *,
    timeout: float,
    stdin_data: str | None = None,
    on_stdout: Callable[[str], Awaitable[None]] | None = None,
    on_stderr: Callable[[str], Awaitable[None]] | None = None,
    cancel_event: asyncio.Event | None = None,
) -> CommandResult:
    """Run one shell command, streaming both streams into their callbacks.

    ``stdin_data`` is written to the remote process and then closed. That is how a
    multi-line script reaches ``bash -s`` without being shell-quoted into the
    command line.

    Output is delivered incrementally through the callbacks, which decide what to
    persist; only a bounded prefix (``_CAPTURE_LIMIT_CHARS``) is also kept in the
    returned ``CommandResult``, so unbounded output does not pin memory.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    stdout_capture = _Capture()
    stderr_capture = _Capture()

    async def sink(capture: _Capture, callback):
        async def _write(chunk: str) -> None:
            # Bound retention: keep a prefix for convenience, never the whole run.
            # The kept text is clipped so ``size`` never exceeds the limit, which
            # matters beyond tidiness: the condition evaluator refuses a stream
            # longer than its scan cap, so an overshoot here would turn a clean
            # "truncated, cannot answer" into an unrelated evaluation error.
            # ``truncated`` is set only when part of a chunk is actually dropped,
            # so a stream that merely ends at the cap is still complete and can be
            # evaluated with confidence.
            if capture.size >= _CAPTURE_LIMIT_CHARS:
                capture.truncated = True
            else:
                remaining = _CAPTURE_LIMIT_CHARS - capture.size
                kept = chunk if len(chunk) <= remaining else chunk[:remaining]
                capture.chunks.append(kept)
                capture.size += len(kept)
                if len(kept) < len(chunk):
                    capture.truncated = True
            if callback is not None:
                # Callbacks may be plain functions (the transcript writer) or
                # coroutines; both must work without the caller worrying about it.
                outcome = callback(chunk)
                if inspect.isawaitable(outcome):
                    await outcome
        return _write

    async def drain(stream, callback, capture: _Capture) -> None:
        write = await sink(capture, callback)
        try:
            while True:
                chunk = await stream.read(_READ_CHUNK)
                if not chunk:
                    return
                await write(chunk)
        except (asyncssh.Error, OSError):
            # Stream torn down (connection lost, channel aborted). Whatever
            # arrived before that point is kept; cancellation is *not* caught
            # here so a shutdown can still interrupt this coroutine.
            return

    async def wait_for_exit() -> None:
        """Resolve when the *process* exits, not when its stdout closes.

        ``process.wait()`` also waits for channel EOF, which a detached child
        inheriting stdout can delay for as long as it lives. Polling the exit
        status lets a finished script be reported promptly and correctly.
        """
        while process.exit_status is None and process.exit_signal is None:
            await asyncio.sleep(_EXIT_POLL_SECONDS)

    process = await conn.create_process(command, encoding="utf-8", errors="replace")
    if stdin_data is not None:
        process.stdin.write(stdin_data)
    # Always close stdin: `bash -s` blocks forever waiting for EOF otherwise.
    process.stdin.write_eof()

    wait_task = asyncio.ensure_future(wait_for_exit())
    drain_task = asyncio.ensure_future(
        asyncio.gather(
            drain(process.stdout, on_stdout, stdout_capture),
            drain(process.stderr, on_stderr, stderr_capture),
        )
    )
    cancel_task = asyncio.ensure_future(cancel_event.wait()) if cancel_event else None

    timed_out = False
    cancelled = False
    exit_code: int | None = None
    exit_unavailable = False

    try:
        watched = {wait_task}
        if cancel_task is not None:
            watched.add(cancel_task)
        done, _ = await asyncio.wait(
            watched, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
        )
        if not done:
            timed_out = True
        elif cancel_task is not None and cancel_task in done and not wait_task.done():
            cancelled = True
        if timed_out or cancelled:
            process.kill()
    finally:
        try:
            # Always reap the channel so the connection stays usable for later
            # steps. A cancellation here propagates (it must not be swallowed, or
            # shutdown would block on a task that kept running).
            try:
                await asyncio.wait_for(wait_task, _KILL_GRACE_SECONDS)
            except TimeoutError:
                exit_unavailable = True

            # The process has exited (or was killed). Give the streams a brief
            # chance to flush, then stop: a lingering grandchild holding the pipe
            # open must not be mistaken for the command still running. ``drain``
            # keeps whatever arrived, and the exit status is already known, so a
            # timeout here is not an error.
            try:
                await asyncio.wait_for(drain_task, _EXIT_DRAIN_GRACE_SECONDS)
            except TimeoutError:
                pass
        finally:
            # Never leave a helper task pending, on any exit path.
            for task in (wait_task, drain_task, cancel_task):
                if task is not None and not task.done():
                    task.cancel()

        if not exit_unavailable:
            try:
                exit_code = process.exit_status
            except (asyncssh.Error, AttributeError):
                exit_unavailable = True

    duration_ms = int((loop.time() - started) * 1000)
    return CommandResult(
        exit_code=exit_code,
        stdout=stdout_capture.text(),
        stderr=stderr_capture.text(),
        duration_ms=duration_ms,
        timed_out=timed_out,
        cancelled=cancelled,
        exit_unavailable=exit_unavailable,
        stdout_truncated=stdout_capture.truncated,
        stderr_truncated=stderr_capture.truncated,
    )


async def close(conn: asyncssh.SSHClientConnection) -> None:
    """Close a connection, never raising."""
    try:
        conn.close()
        await conn.wait_closed()
    except (asyncssh.Error, OSError, RuntimeError):  # pragma: no cover - best effort
        logger.debug("Ignoring error while closing SSH connection", exc_info=True)
