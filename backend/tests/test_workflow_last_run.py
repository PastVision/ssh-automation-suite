"""Workflow list metadata: ``last_run_at``.

The field existed on the schema and was rendered by the frontend, but nothing ever
populated it, so every workflow claimed never to have run. The value is derived from
executions rather than denormalised onto the workflow, because a workflow cannot know
when a run started without asking its history -- and a stored copy would drift the
moment an execution was cancelled.

These tests pin the three properties that make the number trustworthy: it is the most
recent start (not the most recent row, and not the most recent success), it is absent
rather than fabricated when a workflow has never run, and it costs one query for the
whole list instead of one per row.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests import lab_support as lab
from tests.conftest import csrf, sign_in

ALICE = ("alice", "alice-password-1")


@pytest.fixture()
def alice(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *ALICE)


def _template(client: TestClient, token: str, name: str = "t") -> int:
    response = client.post(
        "/api/templates",
        json={
            "name": name,
            "description": None,
            "visibility": "private",
            "steps": [
                {
                    "name": "s1",
                    "script": "echo hi\n",
                    "interpreter": "bash",
                    "timeout_seconds": 60,
                }
            ],
        },
        headers=csrf(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _credential(user_id: int) -> int:
    from app.core.config import get_settings

    return lab.create_lab_credential(get_settings(), owner_id=user_id)


def _workflow(client: TestClient, token: str, *, credential_id: int, name: str) -> int:
    response = client.post(
        "/api/workflows",
        json={
            "name": name,
            "description": None,
            "credential_id": credential_id,
            "entries": [{"kind": "template", "template_id": _template(client, token, name)}],
            "hosts": [{"hostname": "web-1.example.com"}],
            "variables": [],
        },
        headers=csrf(token),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _add_execution(
    workflow_id: int,
    *,
    started_at: datetime | None,
    status: str = "succeeded",
) -> None:
    """Write one execution row directly.

    The runner is deliberately not involved: these assertions are about the *query*
    that derives the summary, and driving real SSH execution would make the
    timestamps depend on wall-clock timing instead of the values under test.
    """
    from app.db.session import get_session_factory
    from app.models.enums import ExecutionStatus, ExecutionTrigger
    from app.models.execution import Execution

    session = get_session_factory()()
    try:
        session.add(
            Execution(
                workflow_id=workflow_id,
                workflow_name="nightly",
                status=ExecutionStatus(status),
                trigger=ExecutionTrigger.MANUAL,
                max_concurrent_hosts=20,
                started_at=started_at,
            )
        )
        session.commit()
    finally:
        session.close()


def _listed(client: TestClient, token: str) -> dict[str, dict]:
    response = client.get("/api/workflows", headers=csrf(token))
    assert response.status_code == 200, response.text
    return {item["name"]: item for item in response.json()["items"]}


# --------------------------------------------------------------------------- #
# Absence
# --------------------------------------------------------------------------- #


def test_a_workflow_that_has_never_run_reports_no_time(app_client, alice, users):
    """Null, not "now" and not the creation time."""
    client, token = alice
    _workflow(client, token, credential_id=_credential(users["alice"]), name="never")

    assert _listed(client, token)["never"]["last_run_at"] is None


def test_a_pending_execution_does_not_count_as_a_run(app_client, alice, users):
    """A queued run has ``started_at`` null, so it has not started.

    Counting it would make an execution that has not touched a host yet appear in
    history as a run -- precisely the confusion the column exists to avoid.
    """
    client, token = alice
    workflow_id = _workflow(
        client, token, credential_id=_credential(users["alice"]), name="queued"
    )
    _add_execution(workflow_id, started_at=None, status="pending")

    assert _listed(client, token)["queued"]["last_run_at"] is None


# --------------------------------------------------------------------------- #
# Selection
# --------------------------------------------------------------------------- #


def test_the_most_recent_start_wins(app_client, alice, users):
    """Not the most recent row: an older execution could finish later."""
    client, token = alice
    workflow_id = _workflow(
        client, token, credential_id=_credential(users["alice"]), name="nightly"
    )
    now = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
    # Inserted out of order on purpose, so "last row" and "latest start" disagree.
    _add_execution(workflow_id, started_at=now)
    _add_execution(workflow_id, started_at=now - timedelta(days=1))
    _add_execution(workflow_id, started_at=now - timedelta(days=2))

    listed = _listed(client, token)["nightly"]

    assert listed["last_run_at"] == "2026-05-01T12:00:00Z"


def test_a_failed_run_still_counts_as_a_run(app_client, alice, users):
    """ "Last run" means last attempt.

    Reporting only successes would leave a workflow that fails nightly showing a
    stale timestamp from weeks ago, which reads as "it has not run" when in fact it
    runs and fails every night.
    """
    client, token = alice
    workflow_id = _workflow(
        client, token, credential_id=_credential(users["alice"]), name="flaky"
    )
    _add_execution(
        workflow_id, started_at=datetime(2026, 4, 1, tzinfo=UTC), status="succeeded"
    )
    _add_execution(
        workflow_id, started_at=datetime(2026, 4, 2, tzinfo=UTC), status="failed"
    )

    assert _listed(client, token)["flaky"]["last_run_at"] == "2026-04-02T00:00:00Z"


def test_each_workflow_reports_its_own_last_run(app_client, alice, users):
    """The grouping key must be the workflow, not a global maximum."""
    client, token = alice
    credential_id = _credential(users["alice"])
    busy = _workflow(client, token, credential_id=credential_id, name="busy")
    idle = _workflow(client, token, credential_id=credential_id, name="idle")

    _add_execution(busy, started_at=datetime(2026, 3, 1, tzinfo=UTC))
    _add_execution(idle, started_at=datetime(2026, 1, 1, tzinfo=UTC))

    listed = _listed(client, token)

    assert listed["busy"]["last_run_at"] == "2026-03-01T00:00:00Z"
    assert listed["idle"]["last_run_at"] == "2026-01-01T00:00:00Z"


def test_the_time_is_serialised_as_an_instant(app_client, alice, users):
    """SQLite hands back naive datetimes; the API must still say UTC.

    An offset-less timestamp is read as local time by a browser, which would shift
    every displayed run by the viewer's offset.
    """
    client, token = alice
    workflow_id = _workflow(
        client, token, credential_id=_credential(users["alice"]), name="utc"
    )
    _add_execution(workflow_id, started_at=datetime(2026, 2, 3, 4, 5, tzinfo=UTC))

    value = _listed(client, token)["utc"]["last_run_at"]

    assert value.endswith("Z")
    assert value.startswith("2026-02-03T04:05:00")


# --------------------------------------------------------------------------- #
# Cost
# --------------------------------------------------------------------------- #


def test_the_aggregate_is_one_query_regardless_of_workflow_count(migrated_db):
    """Pins the shape of the implementation, not just its output.

    A per-row lookup would be invisible in a functional test and become an N+1 on
    every list render, so the statement count is asserted directly.
    """
    from sqlalchemy import event

    from app.db.session import get_session_factory
    from app.services import execution_state

    engine = get_session_factory().kw["bind"]
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        execution_state.last_started_at_by_workflow()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    selects = [s for s in statements if s.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 1, selects
    # One grouped aggregate is the point; a correlated subquery would reintroduce
    # the per-row cost inside a single statement.
    assert "group by" in selects[0].lower()
