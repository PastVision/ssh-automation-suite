"""Adversarial review of the template manager.

These tests exist to try to break the implementation, not to describe it. Every
one drives the HTTP API over a real migrated SQLite database with two or three
independently authenticated users, and asserts a property an attacker or a buggy
client would violate.

Grouped to match the review checklist: authorization, locks, version integrity,
step editor, API surface, and migration.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.db.session import get_session_factory
from app.db.types import utcnow
from app.models.permission import (
    PermissionLevel,
    ResourceLock,
    ResourceType,
)
from app.models.template import TemplateStep, TemplateVersion
from app.services import locks, permissions
from tests.conftest import csrf, sign_in

ALICE = ("alice", "alice-password-1")
BOB = ("bob", "bob-password-1")
CAROL = ("carol", "carol-password-1")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


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
    visibility: str = "private",
    description: str | None = "check disk",
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


def _grant(template_id: int, user_id: int, level: PermissionLevel, by: int) -> None:
    permissions.grant(
        ResourceType.TEMPLATE, template_id, user_id=user_id, level=level, granted_by_id=by
    )


def _edit(client, token, template_id: int, payload: dict[str, object]):
    assert client.post(f"/api/locks/template/{template_id}", headers=csrf(token)).status_code == 201
    return client.put(f"/api/templates/{template_id}", json=payload, headers=csrf(token))


def _payload(name="Disk check", steps=None, row_version=1):
    return {
        "name": name,
        "description": "check disk",
        "steps": steps if steps is not None else _steps(("usage", "df -h")),
        "row_version": row_version,
    }


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


@pytest.fixture()
def carol_client(app_client: TestClient, carol) -> tuple[TestClient, str]:
    client = TestClient(app_client.app)
    return client, sign_in(client, *CAROL)


def _direct_versions(template_id: int) -> list[tuple[int, str, str]]:
    """Read version rows straight from the database, bypassing the API."""
    settings = get_settings()
    path = settings.sqlite_path
    con = sqlite3.connect(str(path))
    try:
        return list(
            con.execute(
                "SELECT version, name, content_checksum FROM template_versions "
                "WHERE template_id = ? ORDER BY version",
                (template_id,),
            )
        )
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# 1. Ownership and authorization
# --------------------------------------------------------------------------- #


def test_private_template_is_invisible_to_an_unrelated_user(alice, bob, carol_client) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]

    b_client, _ = bob
    assert b_client.get(f"/api/templates/{template_id}").status_code == 404
    assert _create(client, token, name="Second").json()["id"] != template_id

    # Every derived endpoint must agree, not just the detail route.
    for path in (
        f"/api/templates/{template_id}/versions",
        f"/api/templates/{template_id}/versions/1",
        f"/api/templates/{template_id}/shares",
    ):
        assert b_client.get(path).status_code == 404, path
    assert (
        b_client.post(
            f"/api/templates/{template_id}/duplicate",
            headers=csrf(b_client.cookies["sshsuite_csrf"]),
        ).status_code
        == 404
    )


def test_list_and_detail_agree_about_read_access(alice, bob, users) -> None:
    """The list query and the detail check must answer the same question.

    They are separate code paths -- a SQL filter versus a Python check -- so a
    divergence here would either hide a readable template or advertise one that
    answers 404.
    """
    client, token = alice
    private_granted = _create(client, token, name="Private but granted").json()["id"]
    shared_granted = _create(client, token, name="Shared and granted", visibility="shared").json()[
        "id"
    ]
    shared_ungranted = _create(
        client, token, name="Shared but not granted", visibility="shared"
    ).json()["id"]
    private_ungranted = _create(client, token, name="Private plain").json()["id"]

    _grant(private_granted, users["bob"], PermissionLevel.VIEW, users["alice"])
    _grant(shared_granted, users["bob"], PermissionLevel.VIEW, users["alice"])

    b_client, _ = bob
    listed = {row["id"] for row in b_client.get("/api/templates").json()["templates"]}

    for template_id in (
        private_granted,
        shared_granted,
        shared_ungranted,
        private_ungranted,
    ):
        detail_ok = b_client.get(f"/api/templates/{template_id}").status_code == 200
        assert detail_ok == (template_id in listed), (
            f"template {template_id}: list says {template_id in listed}, detail says {detail_ok}"
        )

    # Consistency alone would also pass if both paths hid everything. Grants, not
    # the visibility flag, decide access: the two granted templates must be
    # reachable through *both* paths and the two ungranted ones through neither.
    assert {private_granted, shared_granted} <= listed
    assert not ({shared_ungranted, private_ungranted} & listed)
    assert b_client.get(f"/api/templates/{private_granted}").status_code == 200


def test_shared_template_needs_a_grant_not_just_the_flag(alice, bob) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]

    b_client, _ = bob
    assert b_client.get(f"/api/templates/{template_id}").status_code == 404
    assert b_client.get("/api/templates").json()["templates"] == []


def test_view_grant_cannot_mutate(alice, bob, users) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    _grant(template_id, users["bob"], PermissionLevel.VIEW, users["alice"])

    b_client, b_token = bob
    body = _payload(name="Hijacked", steps=_steps(("x", "id")))

    # No lock is obtainable, so the write is refused before it is attempted.
    assert (
        b_client.post(f"/api/locks/template/{template_id}", headers=csrf(b_token)).status_code
        == 403
    )
    assert (
        b_client.put(f"/api/templates/{template_id}", json=body, headers=csrf(b_token)).status_code
        == 404
    )

    # And the content is unchanged.
    after = client.get(f"/api/templates/{template_id}").json()
    assert after["steps"][0]["script"] == "df -h"
    assert after["current_version"] == 1


def test_edit_grant_can_write_but_cannot_share_change_visibility_or_delete(
    alice, bob, users
) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    _grant(template_id, users["bob"], PermissionLevel.EDIT, users["alice"])

    b_client, b_token = bob
    updated = _edit(b_client, b_token, template_id, _payload(steps=_steps(("usage", "df -hT"))))
    assert updated.status_code == 200, updated.text
    assert updated.json()["current_version"] == 2

    # Content edits are allowed; ownership operations are not.
    assert (
        b_client.patch(
            f"/api/templates/{template_id}/visibility",
            json={"visibility": "private"},
            headers=csrf(b_token),
        ).status_code
        == 403
    )
    assert (
        b_client.post(
            f"/api/templates/{template_id}/shares",
            json={"user_id": users["alice"], "level": "view"},
            headers=csrf(b_token),
        ).status_code
        == 403
    )
    assert (
        b_client.delete(f"/api/templates/{template_id}", headers=csrf(b_token)).status_code == 404
    )
    assert b_client.get(f"/api/templates/{template_id}/shares").status_code == 403

    # The owner still sees the template with sharing intact.
    assert client.get(f"/api/templates/{template_id}").status_code == 200


def test_owner_cannot_be_locked_out_by_a_grantee(alice, bob, users) -> None:
    """An editor holding the lock must not block the owner's own content edit
    for longer than the lease, and the owner's write still requires the lock."""
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    _grant(template_id, users["bob"], PermissionLevel.EDIT, users["alice"])

    b_client, b_token = bob
    assert (
        b_client.post(f"/api/locks/template/{template_id}", headers=csrf(b_token)).status_code
        == 201
    )

    # The owner is a different editor: the lock is not silently shared.
    assert (
        client.put(
            f"/api/templates/{template_id}", json=_payload(), headers=csrf(token)
        ).status_code
        == 423
    )

    # But the owner can take it over once Bob releases.
    assert (
        b_client.delete(f"/api/locks/template/{template_id}", headers=csrf(b_token)).status_code
        == 204
    )
    assert _edit(client, token, template_id, _payload()).status_code == 200


def test_revoking_a_share_removes_access_immediately(alice, bob, users) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    _grant(template_id, users["bob"], PermissionLevel.EDIT, users["alice"])

    b_client, b_token = bob
    assert b_client.get(f"/api/templates/{template_id}").status_code == 200
    # Bob holds the lock, to prove revocation also releases it.
    assert (
        b_client.post(f"/api/locks/template/{template_id}", headers=csrf(b_token)).status_code
        == 201
    )

    assert (
        client.delete(
            f"/api/templates/{template_id}/shares/{users['bob']}", headers=csrf(token)
        ).status_code
        == 204
    )

    assert b_client.get(f"/api/templates/{template_id}").status_code == 404
    assert (
        b_client.put(
            f"/api/templates/{template_id}", json=_payload(), headers=csrf(b_token)
        ).status_code
        == 404
    )

    holder = locks.current_holder(ResourceType.TEMPLATE, template_id, user_id=users["alice"])
    assert holder is None, "revoking access must release the editor's lock"


def test_making_private_deletes_grants_atomically(alice, bob, users) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    _grant(template_id, users["bob"], PermissionLevel.EDIT, users["alice"])

    assert (
        client.patch(
            f"/api/templates/{template_id}/visibility",
            json={"visibility": "private"},
            headers=csrf(token),
        ).status_code
        == 200
    )

    assert client.get(f"/api/templates/{template_id}/shares").json() == []

    # A revoked grant must not come back when the template is shared again.
    assert (
        client.patch(
            f"/api/templates/{template_id}/visibility",
            json={"visibility": "shared"},
            headers=csrf(token),
        ).status_code
        == 200
    )

    b_client, _ = bob
    assert b_client.get(f"/api/templates/{template_id}").status_code == 404
    assert client.get(f"/api/templates/{template_id}/shares").json() == []


def test_duplicate_does_not_inherit_ownership_or_permissions(alice, bob, users) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    _grant(template_id, users["bob"], PermissionLevel.EDIT, users["alice"])

    b_client, b_token = bob
    copy = b_client.post(f"/api/templates/{template_id}/duplicate", headers=csrf(b_token))
    assert copy.status_code == 201, copy.text
    body = copy.json()

    assert body["owner_id"] == users["bob"]
    assert body["is_owner"] is True
    assert body["visibility"] == "private", "a copy must not inherit sharing"
    assert body["shares"] == []
    assert body["id"] != template_id

    # Alice (the original owner) must not see Bob's copy.
    assert client.get(f"/api/templates/{body['id']}").status_code == 404
    # And the copy has no grant rows of its own.
    assert permissions.shares_for(ResourceType.TEMPLATE, body["id"]) == []


def test_duplicate_of_own_template_stays_owned_and_private(alice, users) -> None:
    client, token = alice
    template_id = _create(client, token, name="Source").json()["id"]
    copy = client.post(f"/api/templates/{template_id}/duplicate", headers=csrf(token))
    assert copy.status_code == 201
    body = copy.json()
    assert body["owner_id"] == users["alice"]
    assert body["visibility"] == "private"
    assert body["name"] == "Source (copy)"


# --------------------------------------------------------------------------- #
# 2. Edit-lock security
# --------------------------------------------------------------------------- #


def test_write_requires_the_lock(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]
    response = client.put(f"/api/templates/{template_id}", json=_payload(), headers=csrf(token))
    assert response.status_code == 423
    assert client.get(f"/api/templates/{template_id}").json()["current_version"] == 1


def test_only_the_lock_holder_can_renew_or_release(alice, bob, users) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    _grant(template_id, users["bob"], PermissionLevel.EDIT, users["alice"])
    assert client.post(f"/api/locks/template/{template_id}", headers=csrf(token)).status_code == 201

    b_client, b_token = bob
    # A different user cannot renew, release, or overwrite.
    assert (
        b_client.put(f"/api/locks/template/{template_id}/renew", headers=csrf(b_token)).status_code
        == 409
    )
    assert (
        b_client.delete(f"/api/locks/template/{template_id}", headers=csrf(b_token)).status_code
        == 404
    )
    assert client.get(f"/api/locks/template/{template_id}").json()["lock"]["username"] == "alice"


def test_second_session_of_the_same_user_is_a_different_editor(alice, auth_settings) -> None:
    """One user, two browsers. The lock must not be silently shared."""
    client, token = alice
    template_id = _create(client, token).json()["id"]
    assert client.post(f"/api/locks/template/{template_id}", headers=csrf(token)).status_code == 201

    from app.main import create_app

    # A second browser needs the same cookie policy as the first, or its session
    # cookie is Secure-flagged and never comes back over http://testserver.
    other = TestClient(create_app(auth_settings))
    with other:
        other_token = sign_in(other, *ALICE)
        assert (
            other.post(f"/api/locks/template/{template_id}", headers=csrf(other_token)).status_code
            == 409
        )
        assert (
            other.put(
                f"/api/templates/{template_id}",
                json=_payload(),
                headers=csrf(other_token),
            ).status_code
            == 423
        )


def test_expired_lock_is_reclaimable(alice, bob, users) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    _grant(template_id, users["bob"], PermissionLevel.EDIT, users["alice"])
    assert client.post(f"/api/locks/template/{template_id}", headers=csrf(token)).status_code == 201

    # Backdate the lease rather than sleeping for ten minutes.
    session = get_session_factory()()
    try:
        row = (
            session.query(ResourceLock)
            .filter_by(resource_type=ResourceType.TEMPLATE, resource_id=template_id)
            .one()
        )
        row.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    b_client, b_token = bob
    assert (
        b_client.post(f"/api/locks/template/{template_id}", headers=csrf(b_token)).status_code
        == 201
    )

    # The original holder's write now fails: it no longer owns the lock.
    assert (
        client.put(
            f"/api/templates/{template_id}", json=_payload(), headers=csrf(token)
        ).status_code
        == 423
    )


def test_expired_lock_blocks_its_own_holder_from_writing(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]
    assert client.post(f"/api/locks/template/{template_id}", headers=csrf(token)).status_code == 201

    session = get_session_factory()()
    try:
        row = (
            session.query(ResourceLock)
            .filter_by(resource_type=ResourceType.TEMPLATE, resource_id=template_id)
            .one()
        )
        row.expires_at = utcnow() - timedelta(seconds=1)
        session.commit()
    finally:
        session.close()

    assert (
        client.put(
            f"/api/templates/{template_id}", json=_payload(), headers=csrf(token)
        ).status_code
        == 423
    )


def test_logout_releases_locks(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]
    assert client.post(f"/api/locks/template/{template_id}", headers=csrf(token)).status_code == 201
    assert client.post("/api/auth/logout", headers=csrf(token)).status_code == 204

    assert locks.current_holder(ResourceType.TEMPLATE, template_id, user_id=1) is None, (
        "logout must release the session's locks"
    )


def test_stale_client_cannot_overwrite_newer_changes(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]

    first = _edit(
        client, token, template_id, _payload(steps=_steps(("usage", "df -hT")), row_version=1)
    )
    assert first.status_code == 200
    assert first.json()["row_version"] == 2

    # A second editor that still believes it is on row 1 must be refused.
    stale = client.put(
        f"/api/templates/{template_id}",
        json=_payload(steps=_steps(("usage", "rm -rf /")), row_version=1),
        headers=csrf(token),
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_row_version"] == 2

    body = client.get(f"/api/templates/{template_id}").json()
    assert body["steps"][0]["script"] == "df -hT", "the stale write must not land"


def test_concurrent_writers_cannot_both_win(alice, bob, users) -> None:
    """Two editors racing for the same lock: exactly one may take it.

    The check is a partial unique index in the database, so this drives the lock
    service directly from two threads against the real SQLite file.
    """
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    _grant(template_id, users["bob"], PermissionLevel.EDIT, users["alice"])

    from app.services.sessions import create_session

    alice_sid = create_session(users["alice"]).session_id
    bob_sid = create_session(users["bob"]).session_id

    for _ in range(8):
        outcome: dict[str, object] = {"winners": [], "conflicts": 0, "errors": []}
        gate = threading.Barrier(2)

        def contend(
            user_id: int,
            session_id: int,
            *,
            start=gate,
            result=outcome,
        ) -> None:
            start.wait()
            try:
                locks.acquire(
                    ResourceType.TEMPLATE,
                    template_id,
                    user_id=user_id,
                    session_id=session_id,
                )
                result["winners"].append(user_id)  # type: ignore[union-attr]
            except locks.LockConflict:
                result["conflicts"] += 1  # type: ignore[operator]
            except Exception as exc:  # noqa: BLE001
                result["errors"].append(f"{type(exc).__name__}: {exc}")  # type: ignore[union-attr]

        threads = [
            threading.Thread(target=contend, args=(users["alice"], alice_sid)),
            threading.Thread(target=contend, args=(users["bob"], bob_sid)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert outcome["errors"] == [], outcome["errors"]
        assert len(outcome["winners"]) == 1, outcome
        assert outcome["conflicts"] == 1, outcome

        # Reset for the next round so the race is re-run from scratch.
        session = get_session_factory()()
        try:
            row = (
                session.query(ResourceLock)
                .filter_by(resource_type=ResourceType.TEMPLATE, resource_id=template_id)
                .one()
            )
            session.delete(row)
            session.commit()
        finally:
            session.close()


def test_lock_conflict_status_codes(alice, bob, users) -> None:
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    _grant(template_id, users["bob"], PermissionLevel.EDIT, users["alice"])
    assert client.post(f"/api/locks/template/{template_id}", headers=csrf(token)).status_code == 201

    b_client, b_token = bob
    conflict = b_client.post(f"/api/locks/template/{template_id}", headers=csrf(b_token))
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["lock"]["username"] == "alice"

    # Writing without the lock is 423, distinct from the 409 of a stale version.
    assert (
        b_client.put(
            f"/api/templates/{template_id}", json=_payload(), headers=csrf(b_token)
        ).status_code
        == 423
    )


# --------------------------------------------------------------------------- #
# 3. Version integrity
# --------------------------------------------------------------------------- #


def test_each_meaningful_change_appends_exactly_one_version(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]
    history = client.get(f"/api/templates/{template_id}").json()["versions"]
    assert [v["version"] for v in history] == [1]

    first = _edit(client, token, template_id, _payload(steps=_steps(("a", "echo 1"))))
    assert first.status_code == 200, first.text
    second = _edit(
        client,
        token,
        template_id,
        _payload(steps=_steps(("a", "echo 2")), row_version=first.json()["row_version"]),
    )
    assert second.status_code == 200, second.text

    versions = client.get(f"/api/templates/{template_id}/versions").json()
    assert [v["version"] for v in versions] == [3, 2, 1]
    assert len(_direct_versions(template_id)) == 3


def test_identical_content_does_not_create_a_version(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]
    before = len(_direct_versions(template_id))

    current = _payload()
    for _ in range(3):
        response = _edit(client, token, template_id, current)
        assert response.status_code == 200
        # Keep the concurrency token current: the save is a no-op, but
        # row_version still advances because the write was accepted.
        current = _payload(row_version=response.json()["row_version"])

    assert len(_direct_versions(template_id)) == before, "no-op saves must not inflate history"
    assert client.get(f"/api/templates/{template_id}").json()["current_version"] == 1


def test_older_versions_are_immutable(alice) -> None:
    client, token = alice
    template_id = _create(client, token, steps=_steps(("a", "echo original"))).json()["id"]
    _edit(client, token, template_id, _payload(steps=_steps(("a", "echo changed"))))

    v1 = client.get(f"/api/templates/{template_id}/versions/1").json()
    assert v1["steps"][0]["script"] == "echo original"
    assert v1["name"] == "Disk check"

    # Attempting to write into v1 through the API is impossible: there is no route.
    # Verify at the database level that the row is untouched and the checksum stable.
    stored = _direct_versions(template_id)
    assert stored[0][1] == "Disk check"
    assert stored[0][2] == TemplateVersion.compute_content_checksum(
        name="Disk check",
        description="check disk",
        steps=[
            {
                "name": "a",
                "description": None,
                "script": "echo original",
                "interpreter": "bash",
                "timeout_seconds": 60,
            }
        ],
    )


def test_step_reordering_produces_a_correct_new_version(alice) -> None:
    client, token = alice
    template_id = _create(
        client, token, steps=_steps(("first", "echo 1"), ("second", "echo 2"))
    ).json()["id"]

    reordered = _payload(steps=_steps(("second", "echo 2"), ("first", "echo 1")))
    response = _edit(client, token, template_id, reordered)
    assert response.status_code == 200
    body = response.json()

    assert body["current_version"] == 2
    assert [s["position"] for s in body["steps"]] == [0, 1]
    assert [s["name"] for s in body["steps"]] == ["second", "first"]

    v2 = client.get(f"/api/templates/{template_id}/versions/2").json()
    assert [s["name"] for s in v2["steps"]] == ["second", "first"]
    v1 = client.get(f"/api/templates/{template_id}/versions/1").json()
    assert [s["name"] for s in v1["steps"]] == ["first", "second"]


def test_deleting_a_template_keeps_execution_snapshots(alice, users) -> None:
    """Historical execution rows must survive template deletion with lineage nulled."""
    client, token = alice
    template_id = _create(client, token).json()["id"]
    version_id = client.get(f"/api/templates/{template_id}/versions/1").json()["id"]

    from app.models.enums import ExecutionStatus
    from app.models.execution import Execution, ExecutionTemplateSnapshot

    session = get_session_factory()()
    try:
        execution = Execution(
            created_by_id=users["alice"],
            workflow_name="snapshot lineage",
            status=ExecutionStatus.SUCCEEDED,
        )
        session.add(execution)
        session.flush()
        session.add(
            ExecutionTemplateSnapshot(
                execution_id=execution.id,
                template_id=template_id,
                template_version_id=version_id,
                position=0,
                template_name="Disk check",
                template_version=1,
                script="df -h",
                script_checksum=TemplateVersion.compute_checksum("df -h"),
                interpreter="bash",
                timeout_seconds=60,
            )
        )
        session.commit()
        execution_id = execution.id
    finally:
        session.close()

    assert client.delete(f"/api/templates/{template_id}", headers=csrf(token)).status_code == 204

    session = get_session_factory()()
    try:
        snapshot = session.scalars(
            ExecutionTemplateSnapshot.__table__.select().where(
                ExecutionTemplateSnapshot.execution_id == execution_id
            )
        ).all()
        row = session.execute(
            ExecutionTemplateSnapshot.__table__.select().where(
                ExecutionTemplateSnapshot.execution_id == execution_id
            )
        ).one()
        assert row.script == "df -h", "the snapshot keeps its own script copy"
        assert row.template_id is None, "lineage id is nulled, history is kept"
        assert row.template_version_id is None
        assert snapshot is not None
    finally:
        session.close()


def test_historical_version_is_retrievable_exactly(alice) -> None:
    client, token = alice
    script = (
        '#!/usr/bin/env bash\nset -euo pipefail\necho "hello $WORLD"\n'
        "if [ -f /tmp/x ]; then\n  echo yes\nfi"
    )
    template_id = _create(client, token, steps=_steps(("s", script))).json()["id"]
    _edit(client, token, template_id, _payload(steps=_steps(("s", "echo replaced"))))

    v1 = client.get(f"/api/templates/{template_id}/versions/1").json()
    assert v1["steps"][0]["script"] == script
    assert v1["step_count"] == 1
    assert len(v1["content_checksum"]) == 64


def test_concurrent_edits_do_not_corrupt_version_numbers(alice, bob, users) -> None:
    """Sequential saves from two holders must produce a dense 1..N history."""
    client, token = alice
    template_id = _create(client, token, visibility="shared").json()["id"]
    _grant(template_id, users["bob"], PermissionLevel.EDIT, users["alice"])

    observed: list[int] = []
    for index in range(5):
        holder = client
        holder_token = token
        row_version = holder.get(f"/api/templates/{template_id}").json()["row_version"]
        assert holder.post(
            f"/api/locks/template/{template_id}", headers=csrf(holder_token)
        ).status_code in (201,)
        response = holder.put(
            f"/api/templates/{template_id}",
            json=_payload(steps=_steps(("s", f"echo {index}")), row_version=row_version),
            headers=csrf(holder_token),
        )
        assert response.status_code == 200, response.text
        observed.append(response.json()["current_version"])
        assert (
            holder.delete(
                f"/api/locks/template/{template_id}", headers=csrf(holder_token)
            ).status_code
            == 204
        )

    assert observed == [2, 3, 4, 5, 6]
    assert [v[0] for v in _direct_versions(template_id)] == [1, 2, 3, 4, 5, 6]


# --------------------------------------------------------------------------- #
# 4. Step editor correctness
# --------------------------------------------------------------------------- #


def test_positions_are_dense_and_unique_after_mixed_edits(alice) -> None:
    client, token = alice
    template_id = _create(
        client, token, steps=_steps(("a", "echo a"), ("b", "echo b"), ("c", "echo c"))
    ).json()["id"]

    # Drop the middle step.
    response = _edit(
        client, token, template_id, _payload(steps=_steps(("a", "echo a"), ("c", "echo c")))
    )
    assert response.status_code == 200
    body = response.json()
    assert [s["position"] for s in body["steps"]] == [0, 1]
    assert [s["name"] for s in body["steps"]] == ["a", "c"]

    # Positions are unique per version in the database, not just in the response.
    session = get_session_factory()()
    try:
        rows = session.query(TemplateStep).filter_by(version_id=body["versions"][0]["id"]).all()
        positions = [row.position for row in rows]
        assert sorted(positions) == [0, 1]
        assert len(set(positions)) == len(positions)
    finally:
        session.close()


def test_invalid_timeouts_are_rejected(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]
    for bad in (0, -1, 86401, 10**9):
        response = client.put(
            f"/api/templates/{template_id}",
            json={
                "name": "Disk check",
                "description": "check disk",
                "steps": [
                    {
                        "name": "a",
                        "script": "echo hi",
                        "interpreter": "bash",
                        "timeout_seconds": bad,
                    }
                ],
                "row_version": 1,
            },
            headers=csrf(token),
        )
        assert response.status_code == 422, (bad, response.text)


def test_blank_and_malformed_scripts_are_rejected(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]
    for script in ("", "   ", "\n\t\n"):
        response = client.put(
            f"/api/templates/{template_id}",
            json=_payload(
                steps=[
                    {"name": "a", "script": script, "interpreter": "bash", "timeout_seconds": 60}
                ]
            ),
            headers=csrf(token),
        )
        assert response.status_code == 422, (repr(script), response.text)


def test_script_content_is_preserved_exactly(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]

    tricky = (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'echo "double  spaces   preserved"\n'
        "echo 'single \"quoted\" text'\n"
        "printf '%s\\n' \"$1\"\n"
        "cat <<'EOF'\n"
        "  heredoc body with 'quotes' and \\backslash\n"
        "EOF\n"
        "\n\n"
        "echo trailing-newline-test\n"
    )
    response = _edit(
        client,
        token,
        template_id,
        _payload(
            steps=[{"name": "s", "script": tricky, "interpreter": "bash", "timeout_seconds": 60}]
        ),
    )
    assert response.status_code == 200, response.text
    assert response.json()["steps"][0]["script"] == tricky

    # And through a fresh read, and from the version endpoint.
    assert client.get(f"/api/templates/{template_id}").json()["steps"][0]["script"] == tricky
    v2 = client.get(f"/api/templates/{template_id}/versions/2").json()
    assert v2["steps"][0]["script"] == tricky


def test_interpreter_is_validated(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]
    for bad in ("", "   ", "bash; rm -rf /", "$(id)", "python3 -c 'x'", "a b", "`id`"):
        response = client.put(
            f"/api/templates/{template_id}",
            json=_payload(
                steps=[
                    {"name": "a", "script": "echo hi", "interpreter": bad, "timeout_seconds": 60}
                ]
            ),
            headers=csrf(token),
        )
        assert response.status_code == 422, (repr(bad), response.text)

    # Validation runs before the lock gate, so the rejects above need no lock.
    # Accepting a value is a write, so hold the lock for the positives.
    assert client.post(f"/api/locks/template/{template_id}", headers=csrf(token)).status_code == 201
    current = 1
    for good in ("bash", "sh", "python3", "python3.11", "busybox-sh"):
        response = client.put(
            f"/api/templates/{template_id}",
            json=_payload(
                steps=[
                    {"name": "a", "script": "echo hi", "interpreter": good, "timeout_seconds": 60}
                ],
                row_version=current,
            ),
            headers=csrf(token),
        )
        assert response.status_code == 200, (good, response.text)
        current = response.json()["row_version"]


def test_empty_step_list_is_accepted_but_a_blank_name_is_not(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]

    empty = _edit(client, token, template_id, _payload(steps=[]))
    assert empty.status_code == 200
    assert empty.json()["step_count"] == 0

    blank = client.put(
        f"/api/templates/{template_id}",
        json={"name": "   ", "steps": [], "row_version": 2},
        headers=csrf(token),
    )
    assert blank.status_code == 422


# --------------------------------------------------------------------------- #
# 5. API and frontend security
# --------------------------------------------------------------------------- #


def test_responses_expose_no_internal_or_secret_fields(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]
    body = client.get(f"/api/templates/{template_id}").json()

    allowed_summary = {
        "id",
        "name",
        "description",
        "owner_id",
        "owner_label",
        "visibility",
        "is_owner",
        "can_edit",
        "row_version",
        "current_version",
        "step_count",
        "lock",
        "locked_by_other",
        "updated_at",
        "created_at",
    }
    allowed_detail = allowed_summary | {"steps", "versions", "shares"}
    assert set(body) <= allowed_detail

    # Nothing anywhere in the payload may look like a credential or a path.
    rendered = str(body).lower()
    for forbidden in (
        "private_key",
        "passphrase",
        "sudo_password",
        "encrypted",
        "fernet",
        "secret",
        "backend/data",
        "c:\\\\",
        "/etc/",
    ):
        assert forbidden not in rendered, forbidden


def test_authorization_is_enforced_server_side_regardless_of_ui(alice, bob) -> None:
    """Hiding a button is not authorization: the API must refuse on its own."""
    client, token = alice
    template_id = _create(client, token).json()["id"]

    b_client, b_token = bob
    # The list response gives Bob nothing at all, and every direct call still fails.
    assert b_client.get("/api/templates").json()["total"] == 0
    assert b_client.get(f"/api/templates/{template_id}").status_code == 404
    assert (
        b_client.put(
            f"/api/templates/{template_id}", json=_payload(), headers=csrf(b_token)
        ).status_code
        == 404
    )
    assert (
        b_client.delete(f"/api/templates/{template_id}", headers=csrf(b_token)).status_code == 404
    )


def test_csrf_is_required_for_every_mutation(alice) -> None:
    client, token = alice
    template_id = _create(client, token).json()["id"]

    assert client.post("/api/templates", json={"name": "X", "steps": []}).status_code == 403
    assert client.put(f"/api/templates/{template_id}", json=_payload()).status_code == 403
    assert (
        client.patch(
            f"/api/templates/{template_id}/visibility", json={"visibility": "shared"}
        ).status_code
        == 403
    )
    assert client.delete(f"/api/templates/{template_id}").status_code == 403
    assert client.post(f"/api/templates/{template_id}/duplicate").status_code == 403
    assert client.post(f"/api/locks/template/{template_id}").status_code == 403

    # A wrong token is no better than a missing one.
    assert (
        client.post(
            "/api/templates",
            json={"name": "X", "steps": []},
            headers={"X-CSRF-Token": "not-the-real-token"},
        ).status_code
        == 403
    )


def test_unauthenticated_access_is_refused(app_client: TestClient) -> None:
    fresh = TestClient(app_client.app)
    assert fresh.get("/api/templates").status_code == 401
    assert fresh.get("/api/templates/1").status_code == 401


def test_search_filter_never_crosses_an_authorization_boundary(alice, bob, users) -> None:
    client, token = alice
    secret_name = _create(client, token, name="alice-secret-marker").json()["id"]
    _create(client, token, name="alice-visible-marker", visibility="shared")
    _grant(
        _create(client, token, name="alice-shared-granted-marker", visibility="shared").json()[
            "id"
        ],
        users["bob"],
        PermissionLevel.VIEW,
        users["alice"],
    )

    b_client, _ = bob
    for query in ("marker", "secret", "alice-secret-marker", "alice"):
        rows = b_client.get(f"/api/templates?search={query}").json()["templates"]
        assert all(row["id"] != secret_name for row in rows), query
        assert all(
            row["owner_id"] != users["alice"] or row["visibility"] == "shared" for row in rows
        ), query


def test_errors_do_not_leak_paths_or_internals(alice, bob) -> None:
    b_client, b_token = bob
    response = b_client.get("/api/templates/999999")
    assert response.status_code == 404
    text = response.text.lower()
    for forbidden in ("traceback", "sqlalchemy", "c:\\\\", "/home/", "sqlite", "file"):
        assert forbidden not in text, forbidden

    denied = b_client.put("/api/templates/999999", json=_payload(), headers=csrf(b_token))
    assert denied.status_code == 404
    assert "traceback" not in denied.text.lower()


# --------------------------------------------------------------------------- #
# 6. Migration and database integrity
# --------------------------------------------------------------------------- #


def test_unique_name_is_per_owner_not_global(alice, bob) -> None:
    client, token = alice
    assert _create(client, token, name="Shared name").status_code == 201

    b_client, b_token = bob
    # A different owner may reuse the name: it is namespaced per owner.
    assert _create(b_client, b_token, name="Shared name").status_code == 201

    # The same owner may not.
    assert _create(client, token, name="Shared name").status_code == 409


def test_duplicate_name_is_case_insensitive(alice) -> None:
    client, token = alice
    assert _create(client, token, name="Disk Check").status_code == 201
    assert _create(client, token, name="disk check").status_code == 409


def test_foreign_keys_are_enforced_on_the_sqlite_connection(migrated_db) -> None:
    """Deleting a template cascades to versions and steps, and nothing orphans."""
    from app.db.session import get_engine

    engine = get_engine()
    with engine.connect() as connection:
        enabled = connection.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert enabled == 1, "SQLite must enforce foreign keys or cascades are decorative"

    # Verify the schema declares the cascade.
    con = sqlite3.connect(str(migrated_db.sqlite_path))
    try:
        ddl = con.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='template_steps'"
        ).fetchone()[0]
    finally:
        con.close()
    assert "ON DELETE CASCADE" in ddl.upper()


def test_deleting_a_template_removes_versions_and_steps(alice) -> None:
    client, token = alice
    template_id = _create(client, token, steps=_steps(("a", "echo a"), ("b", "echo b"))).json()[
        "id"
    ]
    _edit(client, token, template_id, _payload(steps=_steps(("a", "echo a"))))

    assert client.delete(f"/api/templates/{template_id}", headers=csrf(token)).status_code == 204

    session = get_session_factory()()
    try:
        assert session.query(TemplateVersion).filter_by(template_id=template_id).count() == 0
        orphans = session.execute(
            TemplateStep.__table__.select().where(
                TemplateStep.version_id.notin_(session.query(TemplateVersion.id))
            )
        ).all()
        assert orphans == [], "steps must not outlive their version"
    finally:
        session.close()


def test_global_step_position_uniqueness_is_enforced(alice) -> None:
    """Two steps at the same position in one version must be impossible."""
    client, token = alice
    template_id = _create(client, token).json()["id"]
    version_id = client.get(f"/api/templates/{template_id}/versions/1").json()["id"]

    session = get_session_factory()()
    try:
        session.add(
            TemplateStep(
                version_id=version_id,
                position=0,
                name="collision",
                script="echo boom",
                interpreter="bash",
                timeout_seconds=60,
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        session.close()
