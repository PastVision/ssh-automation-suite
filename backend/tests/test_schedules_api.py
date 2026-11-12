"""Schedule HTTP surface.

These drive the real routes against a real migrated database. The point is the
contract a client depends on: a schedule is created inert (nothing dispatches from
an HTTP call), its history is readable, and another user cannot see it.

The service-level behaviour of scheduling is covered in ``test_scheduling.py``;
this file is only about the translation layer.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.services import workflows
from tests import lab_support as lab
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


@pytest.fixture()
def workflow_id(migrated_db, users) -> int:
    """A workflow owned by alice, created through the real service."""
    from app.services import sessions, templates

    principal = _principal(users["alice"], "alice")
    credential_id = lab.create_lab_credential(migrated_db, owner_id=users["alice"])
    template = templates.create(
        templates.TemplateBody(
            name="probe",
            description=None,
            steps=(templates.StepData(name="probe", script="echo hi\n"),),
        ),
        principal=principal,
    )
    body = workflows.WorkflowBody(
        name="nightly",
        description=None,
        credential_id=credential_id,
        entries=(workflows.StepData(template_id=template.id),),
        variables=(),
        hosts=(workflows.HostData(hostname="web-1.example.com", port=None),),
    )
    del sessions  # imported for its side effect of ensuring the module is loaded
    return workflows.create(body, principal=principal).id


def _principal(user_id: int, username: str):
    from app.services import sessions as sessions_service
    from app.services.sessions import Principal

    issued = sessions_service.create_session(user_id, user_agent="test", remote_addr=None)
    return Principal(
        user_id=user_id,
        username=username,
        display_name=username,
        source="test",
        session_id=issued.session_id,
    )


def _payload(workflow_id: int, **overrides) -> dict:
    body = {
        "name": "hourly",
        "workflow_id": workflow_id,
        "schedule_kind": "cron",
        "timezone": "UTC",
        "cron_expression": "0 * * * *",
        "run_at": None,
        "misfire_policy": "skip",
        "misfire_grace_seconds": 300,
    }
    body.update(overrides)
    return body


def _create(client: TestClient, token: str, payload: dict) -> dict:
    response = client.post("/api/schedules", json=payload, headers=csrf(token))
    assert response.status_code == 201, response.text
    return response.json()


# --------------------------------------------------------------------------- #
# Create and read
# --------------------------------------------------------------------------- #


def test_a_created_schedule_is_inert_and_has_a_next_firing(
    migrated_db, alice, workflow_id
) -> None:
    """Creating a schedule stores it; it must not dispatch anything by itself."""
    from sqlalchemy import func, select

    from app.db.session import get_session_factory
    from app.models.execution import Execution

    client, token = alice
    created = _create(client, token, _payload(workflow_id))

    assert created["state"] == "active"
    assert created["enabled"] is True
    assert created["next_run_at"] is not None
    assert created["workflow_name"] == "nightly"
    # The cron is hourly on the hour, so the next firing is in the future.
    assert datetime.fromisoformat(created["next_run_at"]) > datetime.now(UTC) - timedelta(
        seconds=1
    )

    session = get_session_factory()()
    try:
        assert session.scalar(select(func.count()).select_from(Execution)) == 0
    finally:
        session.close()


def test_an_invalid_cron_is_rejected(migrated_db, alice, workflow_id) -> None:
    client, token = alice
    response = client.post(
        "/api/schedules",
        json=_payload(workflow_id, cron_expression="not a cron"),
        headers=csrf(token),
    )

    assert response.status_code == 422


def test_a_one_time_schedule_needs_a_run_time(migrated_db, alice, workflow_id) -> None:
    client, token = alice
    response = client.post(
        "/api/schedules",
        json=_payload(workflow_id, schedule_kind="once", cron_expression=None),
        headers=csrf(token),
    )

    assert response.status_code == 422


def test_a_one_time_schedule_is_accepted_with_a_run_time(
    migrated_db, alice, workflow_id
) -> None:
    client, token = alice
    run_at = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    created = _create(
        client,
        token,
        _payload(
            workflow_id, schedule_kind="once", cron_expression=None, run_at=run_at
        ),
    )

    assert created["state"] == "scheduled"
    assert created["run_at"] is not None
    assert created["next_run_at"] is not None


def test_a_duplicate_name_is_a_conflict(migrated_db, alice, workflow_id) -> None:
    client, token = alice
    _create(client, token, _payload(workflow_id))

    response = client.post("/api/schedules", json=_payload(workflow_id), headers=csrf(token))

    assert response.status_code == 409


def test_a_schedule_on_a_workflow_i_cannot_see_is_not_found(
    migrated_db, alice, bob, workflow_id
) -> None:
    client, token = bob
    response = client.post("/api/schedules", json=_payload(workflow_id), headers=csrf(token))

    assert response.status_code == 404
    assert response.json()["detail"] == "Workflow not found"


# --------------------------------------------------------------------------- #
# Isolation
# --------------------------------------------------------------------------- #


def test_another_user_cannot_see_or_read_a_schedule(
    migrated_db, alice, bob, workflow_id
) -> None:
    alice_client, alice_token = alice
    created = _create(alice_client, alice_token, _payload(workflow_id))

    bob_client, bob_token = bob
    listed = bob_client.get("/api/schedules", headers=csrf(bob_token))
    assert listed.status_code == 200
    assert listed.json()["items"] == []

    response = bob_client.get(f"/api/schedules/{created['id']}", headers=csrf(bob_token))
    assert response.status_code == 404


# --------------------------------------------------------------------------- #
# Update, pause, resume, delete
# --------------------------------------------------------------------------- #


def test_pausing_clears_the_cursor_and_resuming_recomputes_it(
    migrated_db, alice, workflow_id
) -> None:
    client, token = alice
    created = _create(client, token, _payload(workflow_id))

    paused = client.post(
        f"/api/schedules/{created['id']}/enabled",
        json={"enabled": False, "row_version": created["row_version"]},
        headers=csrf(token),
    )
    assert paused.status_code == 200, paused.text
    body = paused.json()
    assert body["state"] == "paused"
    assert body["next_run_at"] is None

    resumed = client.post(
        f"/api/schedules/{created['id']}/enabled",
        json={"enabled": True, "row_version": body["row_version"]},
        headers=csrf(token),
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["state"] == "active"
    assert resumed.json()["next_run_at"] is not None


def test_a_stale_row_version_is_refused(migrated_db, alice, workflow_id) -> None:
    """Two editors must not silently overwrite each other."""
    client, token = alice
    created = _create(client, token, _payload(workflow_id))

    stale = created["row_version"] - 1
    response = client.put(
        f"/api/schedules/{created['id']}",
        json={**_payload(workflow_id, name="renamed"), "row_version": stale},
        headers=csrf(token),
    )

    assert response.status_code == 409


def test_an_edit_recomputes_the_next_firing(migrated_db, alice, workflow_id) -> None:
    client, token = alice
    created = _create(client, token, _payload(workflow_id, cron_expression="0 * * * *"))

    response = client.put(
        f"/api/schedules/{created['id']}",
        json={
            **_payload(workflow_id, cron_expression="0 3 * * *"),
            "row_version": created["row_version"],
        },
        headers=csrf(token),
    )

    assert response.status_code == 200, response.text
    updated = response.json()
    assert datetime.fromisoformat(updated["next_run_at"]).hour == 3


def test_deleting_a_schedule_hides_it_but_keeps_the_row(
    migrated_db, alice, workflow_id
) -> None:
    client, token = alice
    created = _create(client, token, _payload(workflow_id))

    response = client.delete(f"/api/schedules/{created['id']}", headers=csrf(token))
    assert response.status_code == 204

    listed = client.get("/api/schedules", headers=csrf(token))
    assert listed.json()["items"] == []

    # Reads still succeed for the owner, so the firing history stays reachable.
    still_there = client.get(f"/api/schedules/{created['id']}", headers=csrf(token))
    assert still_there.status_code == 200
    assert still_there.json()["enabled"] is False


# --------------------------------------------------------------------------- #
# Preview and history
# --------------------------------------------------------------------------- #


def test_preview_shows_when_a_cron_will_actually_fire(migrated_db, alice) -> None:
    client, token = alice
    response = client.post(
        "/api/schedules/preview",
        json={
            "schedule_kind": "cron",
            "timezone": "America/New_York",
            "cron_expression": "0 9 * * 1",
            "count": 3,
        },
        headers=csrf(token),
    )

    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert len(items) == 3
    # Every firing is a Monday at 09:00 local time.
    for item in items:
        local = datetime.fromisoformat(item["local_time"])
        assert local.hour == 9
        assert local.weekday() == 0
    # And they are strictly increasing.
    instants = [datetime.fromisoformat(item["scheduled_for"]) for item in items]
    assert instants == sorted(instants)
    assert len(set(instants)) == 3


def test_preview_rejects_an_unusable_cron(migrated_db, alice) -> None:
    client, token = alice
    response = client.post(
        "/api/schedules/preview",
        json={"schedule_kind": "cron", "timezone": "UTC", "cron_expression": "99 99 * * *"},
        headers=csrf(token),
    )

    assert response.status_code == 422


def test_a_new_schedule_has_no_firing_history(migrated_db, alice, workflow_id) -> None:
    client, token = alice
    created = _create(client, token, _payload(workflow_id))

    response = client.get(
        f"/api/schedules/{created['id']}/runs", headers=csrf(token)
    )

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


def test_history_reports_a_skipped_firing_with_its_reason(
    migrated_db, alice, workflow_id
) -> None:
    """The ledger's whole purpose: a firing that did not run is still explained."""
    from datetime import timedelta

    from app.models.schedule import MisfirePolicy
    from app.services import scheduler
    from app.services.lease import SchedulerLease

    client, token = alice
    created = _create(client, token, _payload(workflow_id))
    schedule_id = created["id"]

    # Push the schedule into the past and run one cycle with a tiny grace window.
    _force_cursor(schedule_id, now=datetime.now(UTC) - timedelta(hours=3))

    lease = SchedulerLease()
    assert lease.acquire().held
    lab.run(scheduler.Scheduler(lease=lease).tick())

    response = client.get(f"/api/schedules/{schedule_id}/runs", headers=csrf(token))

    assert response.status_code == 200
    body = response.json()
    assert body["total"] >= 1
    outcomes = {item["outcome"] for item in body["items"]}
    assert "skipped_misfire" in outcomes
    skipped = next(item for item in body["items"] if item["outcome"] == "skipped_misfire")
    assert skipped["detail"]
    del MisfirePolicy  # imported only to document the policy under test


def _force_cursor(schedule_id: int, *, now: datetime) -> None:
    """Backdate a schedule so a recovered firing falls outside the grace window."""
    from app.db.session import get_session_factory
    from app.models.schedule import Schedule

    session = get_session_factory()()
    try:
        schedule = session.get(Schedule, schedule_id)
        schedule.next_run_at = now
        schedule.misfire_grace_seconds = 60
        session.commit()
    finally:
        session.close()
