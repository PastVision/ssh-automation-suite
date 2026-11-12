"""Live transcript streaming over WebSocket.

Two things are being protected here, and they are the reason this file is long.

**Authorization.** The HTTP CSRF middleware only wraps ``http`` requests, so a
WebSocket upgrade bypasses it entirely. If the route did not check Origin and
re-derive ownership itself, any page on the internet could open a socket with a
victim's cookie and read their execution output. Every rejection therefore has a
test, and non-owners must see the same 404 an unknown id produces.

**Exactly-once delivery.** The endpoint subscribes before it reads the transcript
so that output written during the replay is not lost, which means it must also
deduplicate. A stream that loses a line, or repeats one, is a wrong log -- and a
log is the only evidence an operator has for what ran on a host.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDenialResponse

from app.models.enums import ExecutionStatus, HostRunStatus
from app.services import execution_state
from app.services.execution_log import HostTranscript
from app.services.execution_state import HostSpec, StepSpec
from app.services.log_broadcast import LogBroadcaster, get_broadcaster
from tests.conftest import sign_in

ALICE = ("alice", "alice-password-1")
BOB = ("bob", "bob-password-1")

SAME_ORIGIN = {"origin": "http://localhost:5180"}


@pytest.fixture()
def alice(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *ALICE)


@pytest.fixture()
def bob(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *BOB)


@pytest.fixture(autouse=True)
def clean_broadcaster():
    """No subscriber or offset may leak between tests."""
    broadcaster = get_broadcaster()
    broadcaster.reset()
    yield
    broadcaster.reset()


def _host_runs(execution_id: int) -> list[tuple[int, str]]:
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.execution import ExecutionHostRun

    session = get_session_factory()()
    try:
        rows = session.scalars(
            select(ExecutionHostRun)
            .where(ExecutionHostRun.execution_id == execution_id)
            .order_by(ExecutionHostRun.id)
        ).all()
        return [(row.id, row.hostname) for row in rows]
    finally:
        session.close()


def _execution(
    owner_id: int,
    *,
    hostnames: tuple[str, ...] = ("web-1.example.com",),
    status: ExecutionStatus = ExecutionStatus.PENDING,
) -> int:
    execution_id = execution_state.create_execution(
        workflow_name="stream",
        steps=[StepSpec("probe", 1, "echo hi\n", "bash", 30)],
        hosts=[
            HostSpec(hostname, 22, 1, "lab", username="tester") for hostname in hostnames
        ],
        created_by_id=owner_id,
    )
    if status is not ExecutionStatus.PENDING:
        # ``finish_execution`` finalises the execution row and its counters but
        # leaves host rows PENDING, and the stream decides whether to close from
        # the *host* status. Finalise each host too, the way the runner does, or
        # a "finished" execution would still hand out an endless stream.
        for host_run_id, _ in _host_runs(execution_id):
            execution_state.finish_host(
                host_run_id, status=HostRunStatus.SUCCEEDED
            )
        execution_state.finish_execution(execution_id, status=status)
    return execution_id


def _transcript(execution_id: int, host_run_id: int, hostname: str) -> HostTranscript:
    """A writer wired to the real broadcaster, exactly as the runner builds one."""
    broadcaster = get_broadcaster()
    return HostTranscript(
        execution_id=execution_id,
        host_run_id=host_run_id,
        hostname=hostname,
        on_chunk=lambda text, start, end: broadcaster.publish(
            execution_id, host_run_id, text, start=start, end=end
        ),
    )


def _collect(ws, *, until: str = "end", limit: int = 50) -> list[dict]:
    """Read frames until ``until`` arrives. Fails rather than hanging forever."""
    frames: list[dict] = []
    for _ in range(limit):
        message = ws.receive_json()
        frames.append(message)
        if message["type"] == until:
            return frames
    raise AssertionError(f"did not see a {until!r} frame in {limit} frames: {frames}")


def _run(coro):
    """Run a coroutine from a sync test, on the portal TestClient uses.

    Live delivery has to be driven from the same event loop as the app, so the
    test writes a chunk to the transcript while the endpoint is suspended
    waiting on its queue.
    """
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
# Authorization
# --------------------------------------------------------------------------- #


def test_unauthenticated_connection_is_rejected(app_client: TestClient, users) -> None:
    execution_id = _execution(users["alice"])
    host_run_id, _ = _host_runs(execution_id)[0]

    anonymous = TestClient(app_client.app)
    with pytest.raises(WebSocketDenialResponse) as denied:
        with anonymous.websocket_connect(
            f"/api/executions/{execution_id}/hosts/{host_run_id}/ws", headers=SAME_ORIGIN
        ):
            pass
    assert denied.value.status_code == 401


def test_non_owner_is_denied_with_404_not_403(app_client: TestClient, users, bob) -> None:
    """A stranger must not learn that the execution exists.

    404 rather than 403: distinguishing "forbidden" from "missing" tells an
    attacker which ids are real.
    """
    execution_id = _execution(users["alice"])
    host_run_id, _ = _host_runs(execution_id)[0]
    bob_client, _ = bob

    with pytest.raises(WebSocketDenialResponse) as denied:
        with bob_client.websocket_connect(
            f"/api/executions/{execution_id}/hosts/{host_run_id}/ws", headers=SAME_ORIGIN
        ):
            pass
    assert denied.value.status_code == 404


def test_unknown_execution_is_404(alice) -> None:
    alice_client, _ = alice
    with pytest.raises(WebSocketDenialResponse) as denied:
        with alice_client.websocket_connect(
            "/api/executions/999999/hosts/1/ws", headers=SAME_ORIGIN
        ):
            pass
    assert denied.value.status_code == 404


def test_host_from_another_execution_is_rejected(app_client: TestClient, users, alice) -> None:
    """Owning the execution is not enough: the host row must belong to it."""
    alice_client, _ = alice
    mine = _execution(users["alice"])
    other = _execution(users["alice"], hostnames=("other.example.com",))
    foreign_host_id, _ = _host_runs(other)[0]

    with pytest.raises(WebSocketDenialResponse) as denied:
        with alice_client.websocket_connect(
            f"/api/executions/{mine}/hosts/{foreign_host_id}/ws", headers=SAME_ORIGIN
        ):
            pass
    assert denied.value.status_code == 404


def test_denial_is_audited(app_client: TestClient, users, bob) -> None:
    from sqlalchemy import select

    from app.db.session import get_session_factory
    from app.models.audit import AuditEvent

    execution_id = _execution(users["alice"])
    host_run_id, _ = _host_runs(execution_id)[0]
    bob_client, _ = bob

    with pytest.raises(WebSocketDenialResponse):
        with bob_client.websocket_connect(
            f"/api/executions/{execution_id}/hosts/{host_run_id}/ws", headers=SAME_ORIGIN
        ):
            pass

    session = get_session_factory()()
    try:
        rows = session.scalars(
            select(AuditEvent).where(AuditEvent.action == "authz.access_denied")
        ).all()
        assert any(
            row.resource_type == "execution" and row.resource_id == execution_id
            for row in rows
        ), [row.action for row in rows]
    finally:
        session.close()


def test_hostile_origin_is_rejected_but_same_origin_accepted(
    app_client: TestClient, users, alice
) -> None:
    """The CSRF middleware does not see WebSocket upgrades; this check does."""
    alice_client, _ = alice
    execution_id = _execution(users["alice"])
    host_run_id, _ = _host_runs(execution_id)[0]
    path = f"/api/executions/{execution_id}/hosts/{host_run_id}/ws"

    with pytest.raises(WebSocketDenialResponse) as denied:
        with alice_client.websocket_connect(
            path, headers={"origin": "http://evil.example"}
        ):
            pass
    assert denied.value.status_code == 403

    # The same client, same cookie, same-origin: accepted.
    with alice_client.websocket_connect(path, headers=SAME_ORIGIN) as ws:
        assert ws.receive_json()["type"] == "replay"


# --------------------------------------------------------------------------- #
# Replay and live delivery
# --------------------------------------------------------------------------- #


def test_existing_transcript_is_replayed(app_client: TestClient, users, alice) -> None:
    alice_client, _ = alice
    execution_id = _execution(
        users["alice"], status=ExecutionStatus.SUCCEEDED
    )
    host_run_id, hostname = _host_runs(execution_id)[0]

    with _transcript(execution_id, host_run_id, hostname) as transcript:
        transcript.write_stdout("first line\n")
        transcript.write_stderr("a warning\n")

    with alice_client.websocket_connect(
        f"/api/executions/{execution_id}/hosts/{host_run_id}/ws", headers=SAME_ORIGIN
    ) as ws:
        replay = ws.receive_json()

    assert replay["type"] == "replay"
    assert "first line" in replay["content"]
    assert "a warning" in replay["content"]
    assert replay["truncated"] is False
    # The replay covers the whole file, so its byte range starts at 0.
    assert replay["start"] == 0
    assert replay["end"] == len(replay["content"].encode("utf-8"))


def test_live_chunk_is_delivered(app_client: TestClient, users, alice) -> None:
    """A chunk written after connecting arrives without a refresh."""
    alice_client, _ = alice
    execution_id = _execution(users["alice"])
    host_run_id, hostname = _host_runs(execution_id)[0]

    with _transcript(execution_id, host_run_id, hostname) as transcript:
        with alice_client.websocket_connect(
            f"/api/executions/{execution_id}/hosts/{host_run_id}/ws", headers=SAME_ORIGIN
        ) as ws:
            assert ws.receive_json()["type"] == "replay"
            transcript.write_stdout("live output\n")
            chunk = ws.receive_json()

    assert chunk["type"] == "chunk"
    assert "live output" in chunk["content"]
    assert chunk["end"] > chunk["start"]


def test_output_written_during_replay_is_neither_lost_nor_duplicated(
    app_client: TestClient, users, alice
) -> None:
    """A chunk straddling the replay boundary must be delivered exactly once.

    The endpoint subscribes before it reads the file, so a chunk published while
    the replay is in flight is present both in the file and in the queue. This
    reproduces that deterministically by publishing a frame whose byte range lies
    entirely inside the replay, which is exactly the duplicate the offsets exist
    to remove.

    The host is terminal so the server closes the stream with an ``end`` frame;
    the driver reads until it arrives instead of guessing a frame count.
    """
    alice_client, _ = alice
    execution_id = _execution(users["alice"], status=ExecutionStatus.SUCCEEDED)
    host_run_id, hostname = _host_runs(execution_id)[0]
    broadcaster = get_broadcaster()

    with _transcript(execution_id, host_run_id, hostname) as transcript:
        transcript.write_stdout("before connect\n")
        with alice_client.websocket_connect(
            f"/api/executions/{execution_id}/hosts/{host_run_id}/ws", headers=SAME_ORIGIN
        ) as ws:
            replay = ws.receive_json()
            boundary = replay["end"]

            # Re-publish bytes the replay already sent, simulating output that
            # raced the replay read. The client must not see them a second time.
            broadcaster.publish(
                execution_id,
                host_run_id,
                "before connect\n",
                start=boundary - len(b"before connect\n"),
                end=boundary,
            )
            transcript.write_stdout("after replay\n")

            frames = [replay, *_collect(ws, until="end")]

    seen = "".join(f["content"] for f in frames if "content" in f)
    assert seen.count("before connect") == 1
    assert seen.count("after replay") == 1
    # Nothing from the replay is echoed back as a live chunk.
    assert [f["type"] for f in frames].count("replay") == 1


def test_offsets_are_monotonic_and_contiguous(
    app_client: TestClient, users, alice
) -> None:
    """Frames must tile the file with no gap and no overlap.

    The host is terminal so the stream ends on its own; the driver reads to that
    ``end`` frame rather than assuming how many chunks to expect.
    """
    alice_client, _ = alice
    execution_id = _execution(users["alice"], status=ExecutionStatus.SUCCEEDED)
    host_run_id, hostname = _host_runs(execution_id)[0]

    with _transcript(execution_id, host_run_id, hostname) as transcript:
        transcript.write_stdout("one\n")
        with alice_client.websocket_connect(
            f"/api/executions/{execution_id}/hosts/{host_run_id}/ws", headers=SAME_ORIGIN
        ) as ws:
            replay = ws.receive_json()
            transcript.write_stdout("two\n")
            transcript.write_stdout("three\n")

            frames = [replay, *_collect(ws, until="end")]

    chunks = [f for f in frames if f["type"] in {"replay", "chunk"}]
    # Each frame starts exactly where the previous one ended: contiguous, because
    # a gap would drop bytes and an overlap would repeat them.
    for previous, current in zip(chunks, chunks[1:], strict=False):
        assert current["start"] == previous["end"], (previous, current)

    assert "".join(f["content"] for f in chunks).count("two") == 1


def test_multi_byte_characters_survive_replay(
    app_client: TestClient, users, alice
) -> None:
    """A byte window can start mid-character; the split must not corrupt text."""
    alice_client, _ = alice
    execution_id = _execution(users["alice"], status=ExecutionStatus.SUCCEEDED)
    host_run_id, hostname = _host_runs(execution_id)[0]

    with _transcript(execution_id, host_run_id, hostname) as transcript:
        transcript.write_stdout("héllo 日本語 😀\n")

    with alice_client.websocket_connect(
        f"/api/executions/{execution_id}/hosts/{host_run_id}/ws", headers=SAME_ORIGIN
    ) as ws:
        replay = ws.receive_json()

    assert "héllo 日本語 😀" in replay["content"]
    assert "\ufffd" not in replay["content"]


def test_terminal_host_ends_the_stream(app_client: TestClient, users, alice) -> None:
    alice_client, _ = alice
    execution_id = _execution(users["alice"], status=ExecutionStatus.SUCCEEDED)
    host_run_id, hostname = _host_runs(execution_id)[0]

    with _transcript(execution_id, host_run_id, hostname) as transcript:
        transcript.write_stdout("done\n")

    with alice_client.websocket_connect(
        f"/api/executions/{execution_id}/hosts/{host_run_id}/ws", headers=SAME_ORIGIN
    ) as ws:
        frames = _collect(ws, until="end")

    assert frames[-1]["type"] == "end"


def test_multiple_subscribers_receive_independent_streams(
    app_client: TestClient, users, alice
) -> None:
    alice_client, _ = alice
    execution_id = _execution(users["alice"])
    host_run_id, hostname = _host_runs(execution_id)[0]
    path = f"/api/executions/{execution_id}/hosts/{host_run_id}/ws"

    with _transcript(execution_id, host_run_id, hostname) as transcript:
        with alice_client.websocket_connect(path, headers=SAME_ORIGIN) as first:
            assert first.receive_json()["type"] == "replay"
            with alice_client.websocket_connect(path, headers=SAME_ORIGIN) as second:
                assert second.receive_json()["type"] == "replay"
                assert get_broadcaster().subscriber_count(execution_id, host_run_id) == 2

                transcript.write_stdout("shared\n")
                assert "shared" in first.receive_json()["content"]
                assert "shared" in second.receive_json()["content"]

            # Closing one connection must not disturb the other.
            assert get_broadcaster().subscriber_count(execution_id, host_run_id) == 1
            transcript.write_stdout("still here\n")
            assert "still here" in first.receive_json()["content"]


# --------------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------------- #


def test_disconnect_unsubscribes(app_client: TestClient, users, alice) -> None:
    alice_client, _ = alice
    execution_id = _execution(users["alice"])
    host_run_id, hostname = _host_runs(execution_id)[0]

    # The writer is only needed to create the transcript file; no output is
    # written, so only the lifecycle of the connection is under test.
    with _transcript(execution_id, host_run_id, hostname):
        with alice_client.websocket_connect(
            f"/api/executions/{execution_id}/hosts/{host_run_id}/ws", headers=SAME_ORIGIN
        ) as ws:
            assert ws.receive_json()["type"] == "replay"
            assert get_broadcaster().subscriber_count(execution_id, host_run_id) == 1

    assert get_broadcaster().subscriber_count(execution_id, host_run_id) == 0


# --------------------------------------------------------------------------- #
# Broadcaster unit behaviour
# --------------------------------------------------------------------------- #


def test_slow_consumer_overflow_never_blocks_publish() -> None:
    """A client that stops reading must not stall SSH work.

    Publishing is synchronous, so a full queue has to drop rather than wait. The
    drop is counted so the consumer can be told its stream is incomplete.
    """
    broadcaster = LogBroadcaster(queue_size=4)
    subscription = broadcaster.subscribe(1, 1)

    for index in range(20):
        broadcaster.publish(1, 1, f"line {index}\n", start=index, end=index + 1)

    assert subscription.dropped_chunks > 0
    assert subscription.queue.qsize() == 4
    # The newest frames are kept: an operator watches the tail, not the head.
    kept = [subscription.queue.get_nowait() for _ in range(4)]
    assert kept[-1].start == 19


def test_publish_without_subscribers_is_a_no_op() -> None:
    broadcaster = LogBroadcaster()
    assert broadcaster.publish(1, 1, "x", start=0, end=1) is not None
    assert broadcaster.subscriber_count(1, 1) == 0


def test_unsubscribe_is_idempotent() -> None:
    broadcaster = LogBroadcaster()
    subscription = broadcaster.subscribe(1, 1)
    broadcaster.unsubscribe(1, 1, subscription)
    broadcaster.unsubscribe(1, 1, subscription)
    assert broadcaster.subscriber_count(1, 1) == 0


def test_drain_dropped_resets_counters() -> None:
    from app.services.log_broadcast import drain_dropped

    broadcaster = LogBroadcaster(queue_size=1)
    subscription = broadcaster.subscribe(1, 1)
    for index in range(5):
        broadcaster.publish(1, 1, "x", start=index, end=index + 1)

    first = drain_dropped(subscription)
    assert first[0] > 0
    assert drain_dropped(subscription) == (0, 0)
