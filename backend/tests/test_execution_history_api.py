"""Execution history and host transcript endpoints.

These cover the two read surfaces the UI needs that the engine did not expose:
an owner-scoped list, and the filesystem transcript for one host run.

Executions are written directly through ``execution_state.create_execution``
rather than by running SSH work. That keeps these tests fast and deterministic
while still exercising the real persistence layer and the real HTTP routes.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.models.enums import ExecutionStatus
from app.services import execution_state
from app.services.execution_state import HostSpec, StepSpec
from tests.conftest import csrf, sign_in

ALICE = ("alice", "alice-password-1")
BOB = ("bob", "bob-password-1")


@pytest.fixture()
def alice(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *ALICE)


@pytest.fixture()
def bob(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *BOB)


def _host_run_ids(execution_id: int) -> list[tuple[int, str]]:
    """Host run (id, hostname) rows, read straight from the database.

    ``load_host_specs`` is not used here because it resolves the credential row,
    which these tests deliberately do not create: no SSH work ever happens.
    """
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
    name: str = "nightly",
    hostnames: tuple[str, ...] = ("web-1.example.com",),
    status: ExecutionStatus = ExecutionStatus.PENDING,
) -> int:
    execution_id = execution_state.create_execution(
        workflow_name=name,
        steps=[StepSpec("probe", 1, "echo hi\n", "bash", 30)],
        hosts=[
            HostSpec(hostname, 22, 1, "lab", username="tester")
            for hostname in hostnames
        ],
        created_by_id=owner_id,
    )
    if status is not ExecutionStatus.PENDING:
        execution_state.finish_execution(execution_id, status=status)
    return execution_id


def test_history_is_listed_newest_first_with_counts(
    alice, users
) -> None:
    client, token = alice
    first = _execution(users["alice"], name="first")
    second = _execution(
        users["alice"], name="second", hostnames=("a", "b"), status=ExecutionStatus.SUCCEEDED
    )

    response = client.get("/api/executions", headers=csrf(token))
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["total"] == 2
    assert [item["id"] for item in body["items"]] == [second, first]

    newest = body["items"][0]
    assert newest["workflow_name"] == "second"
    assert newest["status"] == "succeeded"
    assert newest["host_count"] == 2
    assert newest["trigger"] == "manual"
    # A summary row must not embed host detail or transcripts.
    assert "hosts" not in newest
    assert "content" not in newest


def test_history_is_scoped_to_the_caller(alice, bob, users) -> None:
    alice_client, alice_token = alice
    bob_client, bob_token = bob

    mine = _execution(users["alice"])
    theirs = _execution(users["bob"])

    alice_body = alice_client.get("/api/executions", headers=csrf(alice_token)).json()
    assert [item["id"] for item in alice_body["items"]] == [mine]

    bob_body = bob_client.get("/api/executions", headers=csrf(bob_token)).json()
    assert [item["id"] for item in bob_body["items"]] == [theirs]

    # Bob cannot open Alice's run, and gets 404 rather than 403.
    assert (
        bob_client.get(f"/api/executions/{mine}", headers=csrf(bob_token)).status_code
        == 404
    )


def test_history_supports_status_filter_and_paging(alice, users) -> None:
    client, token = alice
    for _ in range(3):
        _execution(users["alice"])
    done = _execution(users["alice"], status=ExecutionStatus.SUCCEEDED)

    only_done = client.get("/api/executions?status=succeeded", headers=csrf(token))
    assert only_done.status_code == 200, only_done.text
    assert only_done.json()["total"] == 1
    assert only_done.json()["items"][0]["id"] == done

    page = client.get("/api/executions?limit=2&offset=0", headers=csrf(token)).json()
    assert len(page["items"]) == 2
    assert page["total"] == 4

    second_page = client.get("/api/executions?limit=2&offset=2", headers=csrf(token)).json()
    assert len(second_page["items"]) == 2
    # Pages do not overlap.
    assert {item["id"] for item in page["items"]}.isdisjoint(
        {item["id"] for item in second_page["items"]}
    )


def test_history_rejects_an_unknown_status(alice) -> None:
    client, token = alice
    response = client.get("/api/executions?status=exploded", headers=csrf(token))
    assert response.status_code == 422


def test_history_requires_authentication(app_client) -> None:
    assert app_client.get("/api/executions").status_code == 401


def test_transcript_is_returned_for_an_owned_host_run(alice, users) -> None:
    client, token = alice
    execution_id = _execution(users["alice"], hostnames=("web-1.example.com",))

    host_run_id = _host_run_ids(execution_id)[0][0]

    from app.services.log_store import get_log_store

    store = get_log_store()
    store.prepare_execution_dir(execution_id)
    store.host_log_path(execution_id, host_run_id, "web-1.example.com").write_text(
        "[00:00:01] connecting\n[00:00:02] step 1 ok\n", encoding="utf-8"
    )

    response = client.get(
        f"/api/executions/{execution_id}/hosts/{host_run_id}/transcript",
        headers=csrf(token),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True
    assert body["truncated"] is False
    assert "step 1 ok" in body["content"]
    assert body["hostname"] == "web-1.example.com"


def test_transcript_reports_unavailable_before_a_host_starts(alice, users) -> None:
    """A queued host has no log yet; that is a state, not an error."""
    client, token = alice
    execution_id = _execution(users["alice"])
    host_run_id = _host_run_ids(execution_id)[0][0]

    response = client.get(
        f"/api/executions/{execution_id}/hosts/{host_run_id}/transcript",
        headers=csrf(token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["available"] is False
    assert response.json()["content"] == ""


def test_transcript_is_bounded(alice, users) -> None:
    """A runaway command must not return an unbounded payload."""
    client, token = alice
    execution_id = _execution(users["alice"])
    host_run_id = _host_run_ids(execution_id)[0][0]

    from app.services.log_store import get_log_store

    store = get_log_store()
    store.prepare_execution_dir(execution_id)
    store.host_log_path(execution_id, host_run_id, "web-1.example.com").write_text(
        "x" * 400_000 + "TAIL-MARKER", encoding="utf-8"
    )

    body = client.get(
        f"/api/executions/{execution_id}/hosts/{host_run_id}/transcript",
        headers=csrf(token),
    ).json()
    assert body["truncated"] is True
    assert len(body["content"]) < 400_000
    # The tail is kept, because that is what a failure needs.
    assert body["content"].endswith("TAIL-MARKER")


def test_transcript_hides_another_users_execution(alice, bob, users) -> None:
    alice_client, alice_token = alice
    bob_client, bob_token = bob

    execution_id = _execution(users["alice"])
    host_run_id = _host_run_ids(execution_id)[0][0]

    response = bob_client.get(
        f"/api/executions/{execution_id}/hosts/{host_run_id}/transcript",
        headers=csrf(bob_token),
    )
    assert response.status_code == 404


def test_transcript_rejects_a_host_run_from_another_execution(alice, users) -> None:
    """A host id must belong to the execution named in the path."""
    client, token = alice
    first = _execution(users["alice"])
    second = _execution(users["alice"])
    other_host = _host_run_ids(second)[0][0]

    response = client.get(
        f"/api/executions/{first}/hosts/{other_host}/transcript", headers=csrf(token)
    )
    assert response.status_code == 404


def test_transcript_rejects_a_missing_host_run(alice, users) -> None:
    client, token = alice
    execution_id = _execution(users["alice"])
    response = client.get(
        f"/api/executions/{execution_id}/hosts/999999/transcript", headers=csrf(token)
    )
    assert response.status_code == 404
