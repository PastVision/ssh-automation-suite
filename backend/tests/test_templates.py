"""Template manager: CRUD, versioning, sharing, locks and deletion.

Integration tests over a real migrated SQLite database through the HTTP API. The
interesting properties are cross-request -- an immutable version, an editor
rejected by a stale token, a share that stops working once the template goes
private -- so the tests drive the API rather than asserting on service returns.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.db.types import utcnow
from app.models.audit_actions import AuditAction
from app.models.permission import ResourceType
from app.models.template import TemplateVersion
from app.services import locks
from tests.conftest import csrf, sign_in

ALICE = ("alice", "alice-password-1")
BOB = ("bob", "bob-password-1")
CAROL = ("carol", "carol-password-1")


@pytest.fixture()
def carol(users) -> int:
    from app.services import sessions

    return sessions.create_user(*CAROL, display_name="Carol").id


@pytest.fixture()
def alice(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *ALICE)


@pytest.fixture()
def bob(app_client: TestClient, users) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *BOB)


def _steps(*specs: tuple[str, str]) -> list[dict[str, object]]:
    return [
        {"name": name, "script": script, "interpreter": "bash", "timeout_seconds": 60}
        for name, script in specs
    ]


def _create(
    client: TestClient,
    token: str,
    *,
    name: str = "Disk check",
    steps: list[dict[str, object]] | None = None,
    description: str | None = "check disk usage",
    visibility: str = "private",
):
    return client.post(
        "/api/templates",
        json={
            "name": name,
            "description": description,
            "visibility": visibility,
            "steps": steps if steps is not None else _steps(("usage", "df -h")),
        },
        headers=csrf(token),
    )


def _edit_with_lock(
    client: TestClient, token: str, template_id: int, payload: dict[str, object]
):
    """Acquire the lock, then PUT, mirroring what the editor does."""
    assert client.post(
        f"/api/locks/template/{template_id}", headers=csrf(token)
    ).status_code == 201
    return client.put(
        f"/api/templates/{template_id}", json=payload, headers=csrf(token)
    )


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #

def test_create_read_update_delete(app_client, alice) -> None:
    client, token = alice
    created = _create(client, token)
    assert created.status_code == 201, created.text
    body = created.json()
    template_id = body["id"]

    assert body["name"] == "Disk check"
    assert body["is_owner"] is True
    assert body["can_edit"] is True
    assert body["visibility"] == "private"
    assert body["current_version"] == 1
    assert body["step_count"] == 1
    assert body["row_version"] == 1
    assert [s["position"] for s in body["steps"]] == [0]
    assert body["steps"][0]["script"] == "df -h"
    assert [v["version"] for v in body["versions"]] == [1]

    read = client.get(f"/api/templates/{template_id}")
    assert read.status_code == 200
    assert read.json()["name"] == "Disk check"

    listed = client.get("/api/templates")
    assert listed.status_code == 200
    assert [t["id"] for t in listed.json()["templates"]] == [template_id]

    updated = _edit_with_lock(
        client,
        token,
        template_id,
        {
            "name": "Disk check v2",
            "description": "updated",
            "steps": _steps(("usage", "df -h"), ("inodes", "df -i")),
            "row_version": 1,
        },
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["name"] == "Disk check v2"
    assert updated.json()["current_version"] == 2
    assert updated.json()["step_count"] == 2
    assert updated.json()["row_version"] == 2

    deleted = client.delete(f"/api/templates/{template_id}", headers=csrf(token))
    assert deleted.status_code == 204
    assert client.get(f"/api/templates/{template_id}").status_code == 404


def test_validation_rejects_bad_input(app_client, alice) -> None:
    client, token = alice

    assert _create(client, token, name="   ").status_code == 422
    assert _create(client, token, steps=[{"name": "x", "script": ""}]).status_code == 422
    assert _create(client, token, steps=[{"name": "", "script": "true"}]).status_code == 422
    assert (
        _create(
            client, token, steps=[{"name": "x", "script": "true", "timeout_seconds": 0}]
        ).status_code
        == 422
    )
    # The interpreter ends up in a remote command line, so no shell metacharacters.
    assert (
        _create(
            client,
            token,
            steps=[{"name": "x", "script": "true", "interpreter": "bash; rm -rf /"}],
        ).status_code
        == 422
    )
    # Too many steps.
    assert (
        _create(
            client,
            token,
            steps=[{"name": f"s{i}", "script": "true"} for i in range(101)],
        ).status_code
        == 422
    )


def test_duplicate_name_is_rejected_per_owner(app_client, alice, bob, users) -> None:
    client, token = alice
    assert _create(client, token, name="Same").status_code == 201
    assert _create(client, token, name="Same").status_code == 409
    # Another owner may reuse the name.
    assert _create(bob[0], bob[1], name="Same").status_code == 201


def test_rename_clash_is_rejected(app_client, alice) -> None:
    client, token = alice
    first = _create(client, token, name="One").json()["id"]
    _create(client, token, name="Two")
    response = _edit_with_lock(
        client, token, first, {"name": "Two", "steps": [], "row_version": 1}
    )
    assert response.status_code == 409


# --------------------------------------------------------------------------- #
# Steps and ordering
# --------------------------------------------------------------------------- #

def test_step_order_round_trips_and_reorders(app_client, alice) -> None:
    client, token = alice
    created = _create(
        client,
        token,
        steps=_steps(("first", "echo 1"), ("second", "echo 2"), ("third", "echo 3")),
    ).json()
    assert [s["name"] for s in created["steps"]] == ["first", "second", "third"]
    assert [s["position"] for s in created["steps"]] == [0, 1, 2]

    # Reorder by sending the same steps in a different order.
    reordered = _edit_with_lock(
        client,
        token,
        created["id"],
        {
            "name": created["name"],
            "description": created["description"],
            "steps": _steps(("third", "echo 3"), ("first", "echo 1"), ("second", "echo 2")),
            "row_version": created["row_version"],
        },
    )
    assert reordered.status_code == 200, reordered.text
    assert [s["name"] for s in reordered.json()["steps"]] == ["third", "first", "second"]
    assert [s["position"] for s in reordered.json()["steps"]] == [0, 1, 2]
    assert reordered.json()["current_version"] == 2


def test_steps_can_be_added_and_removed(app_client, alice) -> None:
    client, token = alice
    created = _create(client, token, steps=_steps(("a", "echo a"))).json()

    added = _edit_with_lock(
        client,
        token,
        created["id"],
        {
            "name": created["name"],
            "steps": _steps(("a", "echo a"), ("b", "echo b")),
            "row_version": created["row_version"],
        },
    )
    assert added.json()["step_count"] == 2

    removed = _edit_with_lock(
        client,
        token,
        created["id"],
        {
            "name": created["name"],
            "steps": _steps(("b", "echo b")),
            "row_version": added.json()["row_version"],
        },
    )
    assert removed.json()["step_count"] == 1
    assert removed.json()["steps"][0]["name"] == "b"


# --------------------------------------------------------------------------- #
# Versioning
# --------------------------------------------------------------------------- #

def test_noop_save_creates_no_new_version(app_client, alice) -> None:
    client, token = alice
    created = _create(client, token).json()
    same = _edit_with_lock(
        client,
        token,
        created["id"],
        {
            "name": created["name"],
            "description": created["description"],
            "steps": created["steps"],
            "row_version": created["row_version"],
        },
    )
    assert same.status_code == 200, same.text
    assert same.json()["current_version"] == 1, "an unchanged save must not bump the version"
    assert len(same.json()["versions"]) == 1
    assert same.json()["row_version"] == 2, "row_version still advances for concurrency"


def test_versions_are_immutable_after_later_edits(app_client, alice) -> None:
    client, token = alice
    created = _create(client, token, steps=_steps(("one", "echo v1"))).json()
    template_id = created["id"]

    _edit_with_lock(
        client,
        token,
        template_id,
        {
            "name": created["name"],
            "steps": _steps(("one", "echo v2"), ("two", "echo extra")),
            "row_version": created["row_version"],
        },
    )

    v1 = client.get(f"/api/templates/{template_id}/versions/1")
    assert v1.status_code == 200
    assert v1.json()["step_count"] == 1
    assert v1.json()["steps"][0]["script"] == "echo v1"
    assert len(v1.json()["steps"]) == 1, "a later edit must not touch v1"

    v2 = client.get(f"/api/templates/{template_id}/versions/2")
    assert v2.json()["steps"][0]["script"] == "echo v2"
    assert v2.json()["step_count"] == 2
    assert v1.json()["content_checksum"] != v2.json()["content_checksum"]

    history = client.get(f"/api/templates/{template_id}/versions").json()
    assert [v["version"] for v in history] == [2, 1], "history is newest first"


def test_version_keeps_the_metadata_in_force_at_the_time(app_client, alice) -> None:
    client, token = alice
    created = _create(client, token, name="Original", description="first desc").json()
    _edit_with_lock(
        client,
        token,
        created["id"],
        {
            "name": "Renamed",
            "description": "second desc",
            "steps": created["steps"],
            "row_version": created["row_version"],
        },
    )
    assert client.get(f"/api/templates/{created['id']}/versions/1").json()["name"] == "Original"
    assert client.get(f"/api/templates/{created['id']}/versions/2").json()["name"] == "Renamed"


def test_execution_snapshot_survives_template_edit(app_client, alice, users) -> None:
    """A frozen execution snapshot keeps its script after the template changes.

    This is the property the whole versioning model exists for: editing must not
    retroactively change what an in-flight or finished run executed.
    """
    from app.models.enums import ExecutionStatus
    from app.models.execution import Execution, ExecutionTemplateSnapshot

    client, token = alice
    created = _create(client, token, steps=_steps(("one", "echo original"))).json()
    version = client.get(f"/api/templates/{created['id']}/versions/1").json()

    session = get_session_factory()()
    try:
        execution = Execution(workflow_name="run", status=ExecutionStatus.SUCCEEDED, host_count=1)
        session.add(execution)
        session.flush()
        session.add(
            ExecutionTemplateSnapshot(
                execution_id=execution.id,
                template_id=created["id"],
                template_version_id=version["id"],
                template_name=created["name"],
                template_version=1,
                script="echo original",
                script_checksum=TemplateVersion.compute_checksum("echo original"),
            )
        )
        session.commit()
        execution_id = execution.id
    finally:
        session.close()

    _edit_with_lock(
        client,
        token,
        created["id"],
        {
            "name": created["name"],
            "steps": _steps(("one", "echo REPLACED")),
            "row_version": created["row_version"],
        },
    )

    session = get_session_factory()()
    try:
        snapshot = session.query(ExecutionTemplateSnapshot).filter_by(
            execution_id=execution_id
        ).one()
        assert snapshot.script == "echo original"
        assert snapshot.template_version == 1
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Ownership and sharing
# --------------------------------------------------------------------------- #

def test_bob_cannot_read_or_edit_alices_private_template(app_client, alice, bob) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]

    assert bob[0].get(f"/api/templates/{template_id}").status_code == 404
    assert bob[0].get("/api/templates").json()["templates"] == []
    assert (
        bob[0].put(
            f"/api/templates/{template_id}",
            json={"name": "hijack", "steps": []},
            headers=csrf(bob[1]),
        ).status_code
        == 404
    )
    assert (
        bob[0].delete(f"/api/templates/{template_id}", headers=csrf(bob[1])).status_code
        == 404
    )
    # The owner's template is untouched.
    assert client.get(f"/api/templates/{template_id}").json()["name"] == "Disk check"


def test_shared_view_and_edit_access(app_client, alice, bob, carol, users) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]

    # No grant yet: still invisible.
    assert bob[0].get(f"/api/templates/{template_id}").status_code == 404

    granted = client.post(
        f"/api/templates/{template_id}/shares",
        json={"user_id": users["bob"], "level": "view"},
        headers=csrf(token),
    )
    assert granted.status_code == 201, granted.text
    assert granted.json()[0]["level"] == "view"

    view = bob[0].get(f"/api/templates/{template_id}")
    assert view.status_code == 200
    assert view.json()["is_owner"] is False
    assert view.json()["can_edit"] is False
    assert view.json()["shares"] == [], "a non-owner must not see the share list"

    # A viewer cannot take the lock.
    assert (
        bob[0].post(f"/api/locks/template/{template_id}", headers=csrf(bob[1])).status_code
        == 403
    )

    # Upgrade to edit.
    assert client.post(
        f"/api/templates/{template_id}/shares",
        json={"user_id": users["bob"], "level": "edit"},
        headers=csrf(token),
    ).status_code == 201

    edited = _edit_with_lock(
        bob[0],
        bob[1],
        template_id,
        {"name": "Bob edit", "steps": _steps(("b", "echo bob")), "row_version": 1},
    )
    assert edited.status_code == 200, edited.text
    assert edited.json()["current_version"] == 2

    # Carol has no grant at all.
    assert carol not in (users["alice"], users["bob"])
    from tests.conftest import sign_in as _sign_in

    carol_client = TestClient(app_client.app)
    assert _sign_in(carol_client, *CAROL)
    assert carol_client.get(f"/api/templates/{template_id}").status_code == 404


def test_editor_cannot_share_or_delete(app_client, alice, bob, users) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    client.post(
        f"/api/templates/{template_id}/shares",
        json={"user_id": users["bob"], "level": "edit"},
        headers=csrf(token),
    )

    assert bob[0].post(
        f"/api/templates/{template_id}/shares",
        json={"user_id": users["bob"], "level": "edit"},
        headers=csrf(bob[1]),
    ).status_code == 403
    assert bob[0].delete(
        f"/api/templates/{template_id}/shares/{users['alice']}", headers=csrf(bob[1])
    ).status_code == 403
    assert bob[0].patch(
        f"/api/templates/{template_id}/visibility",
        json={"visibility": "private"},
        headers=csrf(bob[1]),
    ).status_code == 403
    assert bob[0].delete(
        f"/api/templates/{template_id}", headers=csrf(bob[1])
    ).status_code == 404
    assert client.get(f"/api/templates/{template_id}").status_code == 200


def test_switching_back_to_private_revokes_access_immediately(
    app_client, alice, bob, users
) -> None:
    client, token = alice
    created = _create(client, token, visibility="shared").json()
    template_id = created["id"]
    client.post(
        f"/api/templates/{template_id}/shares",
        json={"user_id": users["bob"], "level": "edit"},
        headers=csrf(token),
    )
    assert bob[0].get(f"/api/templates/{template_id}").status_code == 200

    assert client.patch(
        f"/api/templates/{template_id}/visibility",
        json={"visibility": "private"},
        headers=csrf(token),
    ).status_code == 200

    # Going private deletes the grants rather than leaving them dormant, so a
    # later flip back to shared cannot silently restore access.
    assert bob[0].get(f"/api/templates/{template_id}").status_code == 404
    assert bob[0].get("/api/templates").json()["templates"] == []

    shares = client.get(f"/api/templates/{template_id}/shares").json()
    assert shares == [], "grants must be deleted, not merely ignored"

    # Re-sharing starts from an empty list, and Bob's old lock is gone too.
    assert client.patch(
        f"/api/templates/{template_id}/visibility",
        json={"visibility": "shared"},
        headers=csrf(token),
    ).status_code == 200
    assert bob[0].get(f"/api/templates/{template_id}").status_code == 404
    assert client.post(
        f"/api/locks/template/{template_id}", headers=csrf(token)
    ).status_code == 201


def test_revoking_a_share_releases_the_editors_lock(app_client, alice, bob, users) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    client.post(
        f"/api/templates/{template_id}/shares",
        json={"user_id": users["bob"], "level": "edit"},
        headers=csrf(token),
    )
    assert bob[0].post(
        f"/api/locks/template/{template_id}", headers=csrf(bob[1])
    ).status_code == 201
    assert locks.current_holder(ResourceType.TEMPLATE, template_id, user_id=users["bob"])

    assert client.delete(
        f"/api/templates/{template_id}/shares/{users['bob']}", headers=csrf(token)
    ).status_code == 204
    assert locks.current_holder(ResourceType.TEMPLATE, template_id, user_id=users["bob"]) is None
    # The owner can take the lock right away instead of waiting for the lease.
    assert client.post(
        f"/api/locks/template/{template_id}", headers=csrf(token)
    ).status_code == 201


def test_search_and_owned_filter(app_client, alice, bob, users) -> None:
    client, token = alice
    _create(client, token, name="Disk usage", description="df")
    _create(client, token, name="Service restart", description="systemctl")
    shared = _create(
        client, token, name="Shared thing", description="unrelated", visibility="shared"
    ).json()
    client.post(
        f"/api/templates/{shared['id']}/shares",
        json={"user_id": users["bob"], "level": "view"},
        headers=csrf(token),
    )

    assert [t["name"] for t in client.get("/api/templates?search=disk").json()["templates"]] == [
        "Disk usage"
    ]
    assert [
        t["name"] for t in client.get("/api/templates?search=systemctl").json()["templates"]
    ] == ["Service restart"]

    bob_visible = bob[0].get("/api/templates").json()["templates"]
    assert [t["name"] for t in bob_visible] == ["Shared thing"]
    assert bob[0].get("/api/templates?owned=true").json()["templates"] == []


# --------------------------------------------------------------------------- #
# Duplication
# --------------------------------------------------------------------------- #

def test_duplicate_copies_content_privately(app_client, alice, bob, users) -> None:
    client, token = alice
    source = _create(
        client,
        token,
        name="Source",
        description="original",
        steps=_steps(("a", "echo a"), ("b", "echo b")),
        visibility="shared",
    ).json()
    client.post(
        f"/api/templates/{source['id']}/shares",
        json={"user_id": users["bob"], "level": "view"},
        headers=csrf(token),
    )

    copy = bob[0].post(f"/api/templates/{source['id']}/duplicate", headers=csrf(bob[1]))
    assert copy.status_code == 201, copy.text
    body = copy.json()
    assert body["name"] == "Source (copy)"
    assert body["is_owner"] is True
    assert body["visibility"] == "private", "a copy must not inherit sharing"
    assert body["owner_id"] == users["bob"]
    assert [s["script"] for s in body["steps"]] == ["echo a", "echo b"]
    assert body["current_version"] == 1
    assert body["shares"] == []

    # A second duplicate of the same source must not collide with the first.
    again = bob[0].post(f"/api/templates/{source['id']}/duplicate", headers=csrf(bob[1]))
    assert again.status_code == 201
    assert again.json()["name"] == "Source (copy) (2)"

    # The copy is independent: editing it leaves the source alone.
    _edit_with_lock(
        bob[0],
        bob[1],
        body["id"],
        {"name": "Bob copy", "steps": _steps(("z", "echo z")), "row_version": 1},
    )
    assert client.get(f"/api/templates/{source['id']}").json()["step_count"] == 2
    assert client.get(f"/api/templates/{source['id']}").json()["current_version"] == 1


def test_duplicate_requires_view_access(app_client, alice, bob) -> None:
    client, token = alice
    private = _create(client, token).json()
    assert bob[0].post(
        f"/api/templates/{private['id']}/duplicate", headers=csrf(bob[1])
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Concurrency: locks and stale updates
# --------------------------------------------------------------------------- #

def test_edit_without_lock_is_rejected(app_client, alice) -> None:
    client, token = alice
    created = _create(client, token).json()
    response = client.put(
        f"/api/templates/{created['id']}",
        json={"name": "no lock", "steps": []},
        headers=csrf(token),
    )
    assert response.status_code == 423
    assert client.get(f"/api/templates/{created['id']}").json()["name"] == "Disk check"


def test_second_editor_cannot_take_the_lock(app_client, alice, bob, users) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    client.post(
        f"/api/templates/{template_id}/shares",
        json={"user_id": users["bob"], "level": "edit"},
        headers=csrf(token),
    )
    assert client.post(
        f"/api/locks/template/{template_id}", headers=csrf(token)
    ).status_code == 201

    conflict = bob[0].post(f"/api/locks/template/{template_id}", headers=csrf(bob[1]))
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["lock"]["username"] == "alice"
    # Bob's write is refused rather than overwriting Alice's work.
    assert (
        bob[0].put(
            f"/api/templates/{template_id}",
            json={"name": "bob", "steps": []},
            headers=csrf(bob[1]),
        ).status_code
        == 423
    )

    state = bob[0].get(f"/api/locks/template/{template_id}").json()
    assert state["lock"]["username"] == "alice"
    assert state["lock"]["is_mine"] is False


def test_stale_row_version_is_rejected(app_client, alice) -> None:
    client, token = alice
    created = _create(client, token).json()
    template_id = created["id"]
    assert client.post(f"/api/locks/template/{template_id}", headers=csrf(token)).status_code == 201

    first = client.put(
        f"/api/templates/{template_id}",
        json={"name": "first", "steps": _steps(("a", "echo a")), "row_version": 1},
        headers=csrf(token),
    )
    assert first.status_code == 200

    # A second save still claiming row_version 1 must not silently win.
    stale = client.put(
        f"/api/templates/{template_id}",
        json={"name": "stale", "steps": _steps(("b", "echo b")), "row_version": 1},
        headers=csrf(token),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_row_version"] == 2
    current = client.get(f"/api/templates/{template_id}").json()
    assert current["name"] == "first", "the stale write must not have been applied"
    assert current["current_version"] == 2


def test_expired_lock_blocks_the_write(app_client, alice) -> None:
    client, token = alice
    created = _create(client, token).json()
    template_id = created["id"]
    client.post(f"/api/locks/template/{template_id}", headers=csrf(token))

    # Simulate the lease having elapsed while the editor was idle.
    session = get_session_factory()()
    try:
        lock = locks._lookup_active(session, ResourceType.TEMPLATE, template_id)
        lock.expires_at = utcnow() - timedelta(seconds=5)
        session.commit()
    finally:
        session.close()

    response = client.put(
        f"/api/templates/{template_id}",
        json={"name": "after expiry", "steps": [], "row_version": 1},
        headers=csrf(token),
    )
    assert response.status_code == 423
    assert "expired" in response.json()["detail"].lower()


def test_lock_can_be_renewed_then_released(app_client, alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]

    acquired = client.post(f"/api/locks/template/{template_id}", headers=csrf(token))
    assert acquired.status_code == 201
    first_expiry = acquired.json()["lock"]["expires_at"]

    renewed = client.put(f"/api/locks/template/{template_id}/renew", headers=csrf(token))
    assert renewed.status_code == 200
    assert renewed.json()["expires_at"] >= first_expiry

    released = client.delete(
        f"/api/locks/template/{template_id}", headers=csrf(token)
    )
    assert released.status_code == 204
    # Releasing then writing must fail: the lock is gone.
    assert (
        client.put(
            f"/api/templates/{template_id}",
            json={"name": "after release", "steps": [], "row_version": 1},
            headers=csrf(token),
        ).status_code
        == 423
    )


def test_concurrent_editors_have_exactly_one_winner(app_client, alice, bob, users) -> None:
    """Two threads racing for the same template lock: one wins, never both."""
    import threading

    from app.services.sessions import create_session

    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    client.post(
        f"/api/templates/{template_id}/shares",
        json={"user_id": users["bob"], "level": "edit"},
        headers=csrf(token),
    )

    alice_sid = create_session(users["alice"]).session_id
    bob_sid = create_session(users["bob"]).session_id

    for _ in range(6):
        barrier = threading.Barrier(2)
        wins: list[int] = []
        errors: list[str] = []

        def contend(
            user_id: int,
            session_id: int,
            *,
            gate: threading.Barrier = barrier,
            won: list[int] = wins,
            failed: list[str] = errors,
        ) -> None:
            gate.wait()
            try:
                locks.acquire(
                    ResourceType.TEMPLATE,
                    template_id,
                    user_id=user_id,
                    session_id=session_id,
                )
                won.append(user_id)
            except locks.LockConflict:
                pass
            except Exception as exc:  # noqa: BLE001
                failed.append(f"{type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(target=contend, args=(users["alice"], alice_sid)),
            threading.Thread(target=contend, args=(users["bob"], bob_sid)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, errors
        assert len(wins) == 1, f"{len(wins)} winners"

        session = get_session_factory()()
        try:
            active = session.execute(
                __import__("sqlalchemy").text(
                    "SELECT COUNT(*) FROM resource_locks WHERE resource_type='template' "
                    "AND resource_id=:rid AND released_at IS NULL"
                ),
                {"rid": template_id},
            ).scalar_one()
        finally:
            session.close()
        assert active == 1

        locks.release(ResourceType.TEMPLATE, template_id, force=True, reason="test_reset")


# --------------------------------------------------------------------------- #
# Deletion and references
# --------------------------------------------------------------------------- #

def test_delete_is_refused_while_a_workflow_pins_a_version(app_client, alice, users) -> None:
    from app.models.workflow import Workflow, WorkflowStep

    client, token = alice
    created = _create(client, token).json()
    version = client.get(f"/api/templates/{created['id']}/versions/1").json()

    session = get_session_factory()()
    try:
        workflow = Workflow(name="wf", owner_id=users["alice"], row_version=1)
        session.add(workflow)
        session.flush()
        session.add(
            WorkflowStep(workflow_id=workflow.id, template_version_id=version["id"], position=0)
        )
        session.commit()
    finally:
        session.close()

    blocked = client.delete(f"/api/templates/{created['id']}", headers=csrf(token))
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["workflow_steps"] == 1
    assert client.get(f"/api/templates/{created['id']}").status_code == 200


def test_delete_succeeds_and_keeps_history_when_only_executions_reference_it(
    app_client, alice, users
) -> None:
    from app.models.enums import ExecutionStatus
    from app.models.execution import Execution, ExecutionTemplateSnapshot

    client, token = alice
    created = _create(client, token).json()
    version = client.get(f"/api/templates/{created['id']}/versions/1").json()

    session = get_session_factory()()
    try:
        execution = Execution(workflow_name="run", status=ExecutionStatus.SUCCEEDED, host_count=1)
        session.add(execution)
        session.flush()
        session.add(
            ExecutionTemplateSnapshot(
                execution_id=execution.id,
                template_id=created["id"],
                template_version_id=version["id"],
                template_name=created["name"],
                template_version=1,
                script="df -h",
                script_checksum=TemplateVersion.compute_checksum("df -h"),
            )
        )
        session.commit()
        execution_id = execution.id
    finally:
        session.close()

    assert client.delete(f"/api/templates/{created['id']}", headers=csrf(token)).status_code == 204

    session = get_session_factory()()
    try:
        snapshot = session.query(ExecutionTemplateSnapshot).filter_by(
            execution_id=execution_id
        ).one()
        assert snapshot.script == "df -h"
        assert snapshot.template_id is None, "lineage id is nulled, content is kept"
        assert snapshot.template_version_id is None
    finally:
        session.close()


def test_deleting_a_template_drops_its_locks(app_client, alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]
    client.post(f"/api/locks/template/{template_id}", headers=csrf(token))
    assert client.delete(f"/api/templates/{template_id}", headers=csrf(token)).status_code == 204

    session = get_session_factory()()
    try:
        active = session.execute(
            __import__("sqlalchemy").text(
                "SELECT COUNT(*) FROM resource_locks WHERE resource_type='template' "
                "AND resource_id=:rid AND released_at IS NULL"
            ),
            {"rid": template_id},
        ).scalar_one()
    finally:
        session.close()
    assert active == 0


# --------------------------------------------------------------------------- #
# Audit and secret redaction
# --------------------------------------------------------------------------- #

def test_audit_records_the_whole_lifecycle(app_client, alice, bob, users) -> None:
    client, token = alice
    created = _create(client, token, name="Audited").json()
    template_id = created["id"]

    _edit_with_lock(
        client,
        token,
        template_id,
        {"name": "Audited v2", "steps": _steps(("a", "echo a")), "row_version": 1},
    )
    client.patch(
        f"/api/templates/{template_id}/visibility",
        json={"visibility": "shared"},
        headers=csrf(token),
    )
    client.post(
        f"/api/templates/{template_id}/shares",
        json={"user_id": users["bob"], "level": "view"},
        headers=csrf(token),
    )
    client.delete(f"/api/templates/{template_id}/shares/{users['bob']}", headers=csrf(token))
    client.post(f"/api/templates/{template_id}/duplicate", headers=csrf(token))
    client.delete(f"/api/templates/{template_id}", headers=csrf(token))

    connection = sqlite3.connect(str(get_settings().sqlite_path))
    try:
        rows = connection.execute(
            "SELECT action, actor_user_id, actor_username, session_id FROM audit_events "
            "WHERE resource_id = ? AND resource_type = 'template' ORDER BY id",
            (template_id,),
        ).fetchall()
    finally:
        connection.close()

    actions = [row[0] for row in rows]
    for expected in (
        AuditAction.TEMPLATE_CREATED.value,
        AuditAction.TEMPLATE_VERSION_CREATED.value,
        AuditAction.TEMPLATE_UPDATED.value,
        AuditAction.TEMPLATE_VISIBILITY_CHANGED.value,
        AuditAction.TEMPLATE_SHARED.value,
        AuditAction.TEMPLATE_UNSHARED.value,
        AuditAction.TEMPLATE_DELETED.value,
    ):
        assert expected in actions, f"{expected} missing from {actions}"

    for _action, actor_id, actor_username, session_id in rows:
        assert actor_id == users["alice"], "every event must name the acting user"
        assert actor_username == "Alice"
        assert session_id is not None, "template events must carry the acting session"

    connection = sqlite3.connect(str(get_settings().sqlite_path))
    try:
        dup = connection.execute(
            "SELECT actor_user_id, detail FROM audit_events WHERE action = ?",
            (AuditAction.TEMPLATE_DUPLICATED.value,),
        ).fetchall()
    finally:
        connection.close()
    assert dup, "duplication must be audited"
    assert dup[0][0] == users["alice"]
    assert '"source_template_id"' in (dup[0][1] or "")


def test_denied_access_is_audited_against_the_attacker(app_client, alice, bob, users) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]
    assert bob[0].get(f"/api/templates/{template_id}").status_code == 404

    connection = sqlite3.connect(str(get_settings().sqlite_path))
    try:
        rows = connection.execute(
            "SELECT actor_user_id, actor_username, outcome FROM audit_events "
            "WHERE action = ? AND resource_id = ?",
            (AuditAction.ACCESS_DENIED.value, template_id),
        ).fetchall()
    finally:
        connection.close()
    assert rows
    assert all(row[0] == users["bob"] and row[1] == "Bob" for row in rows)
    assert all(row[2] == "denied" for row in rows)


def test_api_never_returns_credential_or_secret_fields(app_client, alice) -> None:
    """Template payloads must carry no key material, path or credential data."""
    client, token = alice
    body = _create(client, token).json()
    detail = client.get(f"/api/templates/{body['id']}").json()
    listing = client.get("/api/templates").json()

    forbidden = {
        "private_key",
        "private_key_path",
        "passphrase",
        "sudo_password",
        "password",
        "password_hash",
        "secret",
        "ciphertext",
        "encrypted",
        "log_path",
        "key_path",
    }

    def walk(node: object, path: str = "") -> list[str]:
        found: list[str] = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key in forbidden:
                    found.append(f"{path}.{key}")
                found.extend(walk(value, f"{path}.{key}"))
        elif isinstance(node, list):
            for index, item in enumerate(node):
                found.extend(walk(item, f"{path}[{index}]"))
        return found

    assert walk(detail) == []
    assert walk(listing) == []
    assert walk(body) == []


def test_step_scripts_are_only_visible_to_people_with_access(app_client, alice, bob, users) -> None:
    client, token = alice
    created = _create(
        client, token, steps=_steps(("secret-ish", "echo TOPSECRET_MARKER"))
    ).json()
    assert "TOPSECRET_MARKER" in client.get(f"/api/templates/{created['id']}").text

    assert bob[0].get(f"/api/templates/{created['id']}").status_code == 404
    assert "TOPSECRET_MARKER" not in bob[0].get("/api/templates").text
    # Version history is gated the same way.
    assert bob[0].get(f"/api/templates/{created['id']}/versions").status_code == 404
    assert bob[0].get(f"/api/templates/{created['id']}/versions/1").status_code == 404


def test_unauthenticated_access_is_rejected(app_client) -> None:
    assert app_client.get("/api/templates").status_code == 401
    assert app_client.post("/api/templates", json={"name": "x"}).status_code == 401
    assert app_client.get("/api/templates/1").status_code == 401


def test_mutations_require_csrf(app_client, alice) -> None:
    client, _token = alice
    assert client.post("/api/templates", json={"name": "x"}).status_code == 403
    assert client.post(
        "/api/templates", json={"name": "x"}, headers={"X-CSRF-Token": "wrong"}
    ).status_code == 403


# --------------------------------------------------------------------------- #
# Per-step Run-As persistence
# --------------------------------------------------------------------------- #
#
# The executor reads ``step.run_as_username`` when it builds a host's command
# list, so a write path that accepts the field and then drops it would run the
# step as the login user while the UI showed a target. These cover the whole
# round trip: create, read back, edit, and the immutable version snapshot.


def _run_as_steps() -> list[dict[str, object]]:
    return [
        {
            "name": "privileged",
            "script": "id -un",
            "interpreter": "bash",
            "timeout_seconds": 60,
            "run_as_username": "deploy",
            "run_as_method": "nopasswd",
        },
        {
            "name": "plain",
            "script": "id -un",
            "interpreter": "bash",
            "timeout_seconds": 60,
        },
    ]


def test_run_as_survives_the_round_trip(app_client, alice) -> None:
    client, token = alice
    created = _create(client, token, steps=_run_as_steps())
    assert created.status_code == 201, created.text
    body = created.json()

    assert body["steps"][0]["run_as_username"] == "deploy"
    assert body["steps"][0]["run_as_method"] == "nopasswd"
    # Omitted means "run as the SSH login user", which must stay None rather
    # than defaulting to something the operator never asked for.
    assert body["steps"][1]["run_as_username"] is None
    assert body["steps"][1]["run_as_method"] is None

    read = client.get(f"/api/templates/{body['id']}").json()
    assert read["steps"][0]["run_as_username"] == "deploy"


def test_run_as_is_captured_in_the_version_snapshot(app_client, alice) -> None:
    """History is what proves what a past run actually executed."""
    client, token = alice
    created = _create(client, token, steps=_run_as_steps()).json()

    versions = client.get(f"/api/templates/{created['id']}/versions").json()
    assert versions
    snapshot = client.get(
        f"/api/templates/{created['id']}/versions/{versions[0]['version']}"
    ).json()

    assert snapshot["steps"][0]["run_as_username"] == "deploy"
    assert snapshot["steps"][0]["run_as_method"] == "nopasswd"


def test_editing_run_as_appends_a_new_version(app_client, alice) -> None:
    """Changing only the Run-As target is a content change, not a no-op save."""
    client, token = alice
    created = _create(client, token, steps=_run_as_steps()).json()

    changed = [dict(step) for step in _run_as_steps()]
    changed[0]["run_as_username"] = "opspw"
    changed[0]["run_as_method"] = "password"

    saved = _edit_with_lock(
        client,
        token,
        created["id"],
        {
            "name": created["name"],
            "description": created["description"],
            "steps": changed,
            "row_version": created["row_version"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["current_version"] == created["current_version"] + 1
    assert saved.json()["steps"][0]["run_as_username"] == "opspw"
    assert saved.json()["steps"][0]["run_as_method"] == "password"


def test_a_no_op_save_does_not_inflate_history(app_client, alice) -> None:
    """The checksum must ignore Run-As for steps that never used it."""
    client, token = alice
    created = _create(client, token, steps=_steps(("usage", "df -h"))).json()

    saved = _edit_with_lock(
        client,
        token,
        created["id"],
        {
            "name": created["name"],
            "description": created["description"],
            "steps": _steps(("usage", "df -h")),
            "row_version": created["row_version"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["current_version"] == created["current_version"]
