"""In-process fan-out of live transcript bytes to WebSocket subscribers.

The runner appends a host's transcript to disk; this module lets a connected
client follow those appends as they happen. It is deliberately small because the
deployment is a single process: the concurrency limiter and the runner are both
process-wide singletons, so there is no second process whose output would need
relaying. No broker, no database, no cross-process claim.

Two properties matter more than elegance:

**Publishing must never block the SSH work.** ``publish`` is a synchronous
``put_nowait`` into bounded queues. A subscriber that stops reading therefore
cannot stall a host workflow -- waiting on a slow browser would hold a
concurrency slot hostage. When a queue is full the *oldest* frame is dropped,
because a log viewer cares about the newest output, and the drop is counted so
the consumer is told the stream is incomplete rather than being shown a gap as
if it were continuous.

**Offsets are absolute file offsets.** Every frame carries ``start``/``end``
byte offsets relative to the transcript file. That is what makes replay
race-free: a subscriber that connects mid-run reads the file up to a captured
boundary and then discards queued frames that end at or before it, so a chunk
written during the replay is delivered exactly once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Frames retained per subscriber. Output can arrive far faster than a browser
# renders it, so this bounds memory per connection instead of growing without
# limit. 2048 chunks is generous for rendering while keeping a stalled
# subscriber small; the buffer drains as the client reads.
DEFAULT_QUEUE_SIZE = 2048

# A single client-side warning above this many subscribers suggests a leak
# (a consumer that never unsubscribed) rather than legitimate use.
_SUBSCRIBER_WARN_THRESHOLD = 256


@dataclass(frozen=True, slots=True)
class LogFrame:
    """One chunk of transcript bytes, addressed by absolute file offset."""

    start: int
    end: int
    text: str


@dataclass(frozen=True, slots=True)
class StatusEvent:
    """Execution-level state change, fanned out on the same subscriptions.

    Carried on the transcript channel because that is the only live channel an
    open execution view already holds. It deliberately has no byte offsets: it is
    a state notification, not transcript content, and must never be treated as
    bytes to concatenate -- otherwise a status line would corrupt the log the
    client is reassembling.
    """

    execution_id: int
    status: str
    reason: str | None = None
    cancel_requested_at: str | None = None


@dataclass
class _Subscription:
    queue: asyncio.Queue[LogFrame | None]
    dropped_chunks: int = 0
    dropped_bytes: int = 0
    _closed: bool = field(default=False, repr=False)


class LogBroadcaster:
    """Fan-out of transcript frames, keyed by ``(execution_id, host_run_id)``."""

    def __init__(self, *, queue_size: int = DEFAULT_QUEUE_SIZE) -> None:
        self._queue_size = queue_size
        self._subscribers: dict[tuple[int, int], list[_Subscription]] = {}
        # Absolute offset of the next byte expected per host. The transcript
        # writer reports lengths; the broadcaster is the single place that turns
        # them into file positions, so a re-created writer cannot restart at 0.
        self._offsets: dict[tuple[int, int], int] = {}

    # -- publishing -------------------------------------------------------- #

    def publish(
        self, execution_id: int, host_run_id: int, text: str, *, start: int, end: int
    ) -> LogFrame | None:
        """Fan ``text`` out to subscribers as the frame ``[start, end)``.

        The writer supplies the offsets because it owns the file position: it
        knows whether it appended to an existing transcript (a restart, or a
        rerun of the same host row) and therefore whether the first chunk
        belongs at 0. This module only makes sure the numbering the client sees
        stays monotonic.

        Returns the frame, or ``None`` when there was nothing to publish. Never
        raises for a subscriber's benefit: a broken queue must not break the run.
        """
        if end <= start:
            return None

        key = (execution_id, host_run_id)
        guessed = self._offsets.get(key, 0)
        if start < guessed:  # pragma: no branch - only on a misbehaving writer
            logger.warning(
                "Transcript offset went backwards for execution %s host %s (%s < %s)",
                execution_id,
                host_run_id,
                start,
                guessed,
            )
        frame = LogFrame(start=start, end=end, text=text)
        self._offsets[key] = max(guessed, end)

        subscribers = self._subscribers.get(key)
        if not subscribers:
            return frame

        # Iterate a copy: an unsubscribe can run while this loop holds the list.
        for subscription in list(subscribers):
            if subscription._closed:
                continue
            try:
                subscription.queue.put_nowait(frame)
            except asyncio.QueueFull:
                # Drop the oldest frame to make room for the newest. Losing the
                # head of a log is better than stalling the run or losing the
                # tail, which is what an operator is watching for.
                try:
                    dropped = subscription.queue.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover - full implies not empty
                    dropped = None
                if dropped is not None:
                    subscription.dropped_chunks += 1
                    subscription.dropped_bytes += dropped.end - dropped.start
                try:
                    subscription.queue.put_nowait(frame)
                except asyncio.QueueFull:  # pragma: no cover - defensive
                    subscription.dropped_chunks += 1
                    subscription.dropped_bytes += frame.end - frame.start
        return frame

    def current_offset(self, execution_id: int, host_run_id: int) -> int:
        """Bytes published so far for this host, as the writer sees it."""
        return self._offsets.get((execution_id, host_run_id), 0)

    # -- subscription ------------------------------------------------------ #

    def subscribe(self, execution_id: int, host_run_id: int) -> _Subscription:
        key = (execution_id, host_run_id)
        subscription = _Subscription(queue=asyncio.Queue(maxsize=self._queue_size))
        subscribers = self._subscribers.setdefault(key, [])
        subscribers.append(subscription)
        if len(subscribers) > _SUBSCRIBER_WARN_THRESHOLD:
            logger.warning(
                "Unusually many transcript subscribers for execution %s host %s: %s",
                execution_id,
                host_run_id,
                len(subscribers),
            )
        return subscription

    def publish_status(
        self, execution_id: int, status: str, *, reason: str | None = None,
        cancel_requested_at: str | None = None,
    ) -> None:
        """Tell every subscriber of this execution that its state changed.

        Delivered to all hosts' subscriptions, because a client watching host 3
        still needs to learn that the whole execution was cancelled. A status
        event is never dropped for queue pressure -- it is small and it is the
        last message a run produces, so losing it would leave a viewer waiting on
        a run that is already over.
        """
        event = StatusEvent(
            execution_id=execution_id,
            status=status,
            reason=reason,
            cancel_requested_at=cancel_requested_at,
        )
        for (run_execution_id, _host_run_id), subscribers in list(self._subscribers.items()):
            if run_execution_id != execution_id:
                continue
            for subscription in list(subscribers):
                if subscription._closed:
                    continue
                try:
                    subscription.queue.put_nowait(event)
                except asyncio.QueueFull:
                    # Make room by discarding one stale chunk, which the client can
                    # still read from the transcript file.
                    with contextlib.suppress(asyncio.QueueEmpty):
                        subscription.queue.get_nowait()
                    with contextlib.suppress(asyncio.QueueFull):
                        subscription.queue.put_nowait(event)

    def unsubscribe(
        self, execution_id: int, host_run_id: int, subscription: _Subscription
    ) -> None:
        """Remove a subscriber. Idempotent, so ``finally`` can always call it."""
        key = (execution_id, host_run_id)
        subscription._closed = True
        subscribers = self._subscribers.get(key)
        if subscribers is None:
            return
        try:
            subscribers.remove(subscription)
        except ValueError:
            return
        if not subscribers:
            self._subscribers.pop(key, None)

    def subscriber_count(self, execution_id: int, host_run_id: int) -> int:
        return len(self._subscribers.get((execution_id, host_run_id), ()))

    def reset(self) -> None:
        """Drop all state. For tests and for an application restart in-process."""
        self._subscribers.clear()
        self._offsets.clear()


BROADCASTER = LogBroadcaster()


def get_broadcaster() -> LogBroadcaster:
    return BROADCASTER


def drain_dropped(subscription: _Subscription) -> tuple[int, int]:
    """Read and clear the drop counters, so a warning is sent once."""
    chunks, byte_count = subscription.dropped_chunks, subscription.dropped_bytes
    subscription.dropped_chunks = 0
    subscription.dropped_bytes = 0
    return chunks, byte_count
