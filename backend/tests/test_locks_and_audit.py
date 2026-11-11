"""Edit locks and audit integrity.

Locks are tested at the service layer as well as over HTTP, because the interesting
property is that two concurrent acquirers cannot both win -- and that is decided by
the database, not by a Python check.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.db.session import get_session_factory
from app.db.types import utcnow
from app.models.audit_actions import AuditAction
from app.models.permission import PermissionLevel, ResourceType
from app.models.template import Template
from app.services import audit, locks, permissions
from app.services.audit import UnsafeAuditDetail
from tests.conftest import csrf, sign_in

PASSWORD = "alice-password-1"


@pytest.fixture()
def template_id(users) -> int:
    """An Alice-owned template to lock."""
    session = get_session_factory()()
    try:
        template = Template(name="alice-template", owner_id=users["alice"], row_version=1)
        session.add(template)
        session.commit()
        return template.id
    finally:
        session.close()


def _session_id(user_id: int) -> int:
    from app.services.sessions import create_session

    return create_session(user_id).session_id


# --------------------------------------------------------------------------- #
# Service-level locking
# --------------------------------------------------------------------------- #


def test_lock_acquire_and_release(users, template_id: int) -> None:
    sid = _session_id(users["alice"])

    info = locks.acquire(ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=sid)
    assert info.is_mine and info.user_id == users["alice"]

    holder = locks.current_holder(ResourceType.TEMPLATE, template_id, user_id=users["bob"])
    assert holder is not None and holder.user_id == users["alice"]

    assert locks.release(ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=sid)
    assert locks.current_holder(ResourceType.TEMPLATE, template_id, user_id=users["bob"]) is None


def test_second_user_cannot_acquire_a_held_lock(users, template_id: int) -> None:
    alice_sid = _session_id(users["alice"])
    bob_sid = _session_id(users["bob"])

    locks.acquire(ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=alice_sid)

    with pytest.raises(locks.LockConflict) as excinfo:
        locks.acquire(ResourceType.TEMPLATE, template_id, user_id=users["bob"], session_id=bob_sid)
    assert excinfo.value.holder.user_id == users["alice"]
    assert excinfo.value.holder.username == "alice"


def test_same_user_second_session_is_a_conflict(users, template_id: int) -> None:
    """Two browser tabs of one user are two editors.

    Treating them as one editor would let a silent overwrite happen inside a single
    account, which is exactly what the lock exists to prevent.
    """
    first = _session_id(users["alice"])
    second = _session_id(users["alice"])

    locks.acquire(ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=first)
    with pytest.raises(locks.LockConflict):
        locks.acquire(ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=second)


def test_same_session_reacquire_is_idempotent(users, template_id: int) -> None:
    sid = _session_id(users["alice"])
    first = locks.acquire(
        ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=sid
    )
    second = locks.acquire(
        ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=sid
    )
    assert second.expires_at >= first.expires_at


def test_concurrent_acquire_has_exactly_one_winner(users, template_id: int) -> None:
    """Two threads racing for the same lock: one wins, one conflicts, never both.

    The earlier suite covered this sequentially, which cannot exercise the race the
    partial unique index exists to settle. Each round uses a fresh resource so a
    leftover lock from a previous round cannot mask a failure.
    """
    import threading

    from app.services.sessions import create_session

    alice_sid = create_session(users["alice"]).session_id
    bob_sid = create_session(users["bob"]).session_id

    for round_number in range(10):
        resource_id = 1000 + round_number
        barrier = threading.Barrier(2)
        wins: list[int] = []
        conflicts: list[int] = []
        errors: list[str] = []

        # Bind the loop state as defaults: a closure over the loop variables would
        # work only because every thread is joined before the next round starts.
        def contend(
            user_id: int,
            session_id: int,
            barrier=barrier,
            resource_id=resource_id,
            wins=wins,
            conflicts=conflicts,
            errors=errors,
        ) -> None:
            barrier.wait()
            try:
                info = locks.acquire(
                    ResourceType.TEMPLATE, resource_id, user_id=user_id, session_id=session_id
                )
                wins.append(info.user_id)
            except locks.LockConflict as exc:
                conflicts.append(exc.holder.user_id)
            except Exception as exc:  # noqa: BLE001 - recorded and asserted below
                errors.append(f"{type(exc).__name__}: {exc}")

        threads = [
            threading.Thread(target=contend, args=(users["alice"], alice_sid)),
            threading.Thread(target=contend, args=(users["bob"], bob_sid)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert not errors, f"round {round_number} raised: {errors}"
        assert len(wins) == 1, f"round {round_number}: {len(wins)} winners ({wins})"
        assert len(conflicts) == 1, f"round {round_number}: {len(conflicts)} conflicts"

        session = get_session_factory()()
        try:
            active = session.execute(
                __import__("sqlalchemy").text(
                    "SELECT COUNT(*) FROM resource_locks "
                    "WHERE resource_id = :rid AND released_at IS NULL"
                ),
                {"rid": resource_id},
            ).scalar_one()
        finally:
            session.close()
        assert active == 1, f"round {round_number}: {active} active rows in the database"


def test_database_rejects_two_active_locks(users, template_id: int) -> None:
    """The guarantee is the partial unique index, not application logic.

    Inserting directly, bypassing every service function, must still fail.
    """
    alice_sid = _session_id(users["alice"])
    bob_sid = _session_id(users["bob"])
    locks.acquire(ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=alice_sid)

    session = get_session_factory()()
    try:
        from app.models.permission import ResourceLock

        now = utcnow()
        session.add(
            ResourceLock(
                resource_type=ResourceType.TEMPLATE,
                resource_id=template_id,
                user_id=users["bob"],
                session_id=bob_sid,
                acquired_at=now,
                renewed_at=now,
                expires_at=now + timedelta(minutes=5),
            )
        )
        import sqlalchemy.exc

        with pytest.raises(sqlalchemy.exc.IntegrityError):
            session.commit()
    finally:
        session.rollback()
        session.close()


def test_expired_lock_can_be_reclaimed(users, template_id: int) -> None:
    from sqlalchemy import update

    from app.models.permission import ResourceLock

    alice_sid = _session_id(users["alice"])
    bob_sid = _session_id(users["bob"])
    locks.acquire(ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=alice_sid)

    session = get_session_factory()()
    try:
        session.execute(update(ResourceLock).values(expires_at=utcnow() - timedelta(seconds=1)))
        session.commit()
    finally:
        session.close()

    # Nobody holds it any more...
    assert locks.current_holder(ResourceType.TEMPLATE, template_id, user_id=users["bob"]) is None

    # ...so Bob can take it.
    info = locks.acquire(
        ResourceType.TEMPLATE, template_id, user_id=users["bob"], session_id=bob_sid
    )
    assert info.user_id == users["bob"]


def test_renew_requires_current_holder(users, template_id: int) -> None:
    alice_sid = _session_id(users["alice"])
    bob_sid = _session_id(users["bob"])
    locks.acquire(ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=alice_sid)

    with pytest.raises(locks.LockRequired):
        locks.renew(ResourceType.TEMPLATE, template_id, user_id=users["bob"], session_id=bob_sid)

    renewed = locks.renew(
        ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=alice_sid
    )
    assert renewed.is_mine


def test_assert_holds_rejects_a_non_holder(users, template_id: int) -> None:
    alice_sid = _session_id(users["alice"])
    bob_sid = _session_id(users["bob"])
    locks.acquire(ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=alice_sid)

    with pytest.raises(locks.LockConflict):
        locks.assert_holds(
            ResourceType.TEMPLATE, template_id, user_id=users["bob"], session_id=bob_sid
        )

    # Alice still holds it, so this must not raise.
    locks.assert_holds(
        ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=alice_sid
    )


def test_release_all_for_session_frees_locks(users, template_id: int) -> None:
    """Logout must not leave a lock blocking other editors."""
    sid = _session_id(users["alice"])
    locks.acquire(ResourceType.TEMPLATE, template_id, user_id=users["alice"], session_id=sid)

    assert locks.release_all_for_session(sid, reason="logout") == 1
    assert locks.current_holder(ResourceType.TEMPLATE, template_id, user_id=users["alice"]) is None


def test_stale_version_is_detected() -> None:
    locks.check_version(3, 3)
    with pytest.raises(locks.StaleVersion) as excinfo:
        locks.check_version(2, 3)
    assert excinfo.value.current_version == 3
    # A client that sends nothing is not treated as stale.
    locks.check_version(None, 7)


def test_permissions_owner_versus_shared_user(users, template_id: int) -> None:
    """Owner has full access; another user needs an explicit grant."""
    assert permissions.can_view(ResourceType.TEMPLATE, template_id, users["alice"], users["alice"])
    assert not permissions.can_view(
        ResourceType.TEMPLATE, template_id, users["alice"], users["bob"]
    )

    permissions.grant(
        ResourceType.TEMPLATE,
        template_id,
        user_id=users["bob"],
        level=PermissionLevel.VIEW,
        granted_by_id=users["alice"],
    )
    assert permissions.can_view(ResourceType.TEMPLATE, template_id, users["alice"], users["bob"])
    # View does not imply edit.
    assert not permissions.can_edit(
        ResourceType.TEMPLATE, template_id, users["alice"], users["bob"]
    )

    permissions.grant(
        ResourceType.TEMPLATE,
        template_id,
        user_id=users["bob"],
        level=PermissionLevel.EDIT,
        granted_by_id=users["alice"],
    )
    assert permissions.can_edit(ResourceType.TEMPLATE, template_id, users["alice"], users["bob"])


# --------------------------------------------------------------------------- #
# Lock API
# --------------------------------------------------------------------------- #


def test_lock_api_conflict_returns_409_with_holder(
    app_client: TestClient, users, template_id: int
) -> None:
    shared = users["bob"]
    permissions.grant(
        ResourceType.TEMPLATE,
        template_id,
        user_id=shared,
        level=PermissionLevel.EDIT,
        granted_by_id=users["alice"],
    )

    alice_csrf = sign_in(app_client, "alice", PASSWORD)
    response = app_client.post(f"/api/locks/template/{template_id}", headers=csrf(alice_csrf))
    assert response.status_code == 201

    with TestClient(app_client.app) as bob_client:
        sign_in(bob_client, "bob", "bob-password-1")
        bob_csrf = bob_client.cookies.get("sshsuite_csrf") or ""

        state = bob_client.get(f"/api/locks/template/{template_id}").json()
        assert state["locked"] is True
        assert state["lock"]["is_mine"] is False
        assert state["lock"]["username"] == "alice"
        assert state["can_edit"] is True, "Bob has an explicit edit share"

        conflict = bob_client.post(f"/api/locks/template/{template_id}", headers=csrf(bob_csrf))
        assert conflict.status_code == 409
        body = conflict.json()["detail"]
        assert "alice" in body["message"].lower() or "Alice" in body["message"]
        assert body["lock"]["user_id"] == users["alice"]


def test_lock_api_denies_a_user_without_access(
    app_client: TestClient, users, template_id: int
) -> None:
    with TestClient(app_client.app) as bob_client:
        sign_in(bob_client, "bob", "bob-password-1")
        bob_csrf = bob_client.cookies.get("sshsuite_csrf") or ""

        response = bob_client.post(f"/api/locks/template/{template_id}", headers=csrf(bob_csrf))
        assert response.status_code == 403

        # And he cannot see it either.
        state = bob_client.get(f"/api/locks/template/{template_id}").json()
        assert state["can_view"] is False


def test_lock_api_release_frees_it_for_another_editor(
    app_client: TestClient, users, template_id: int
) -> None:
    permissions.grant(
        ResourceType.TEMPLATE,
        template_id,
        user_id=users["bob"],
        level=PermissionLevel.EDIT,
        granted_by_id=users["alice"],
    )
    alice_csrf = sign_in(app_client, "alice", PASSWORD)
    app_client.post(f"/api/locks/template/{template_id}", headers=csrf(alice_csrf))

    assert (
        app_client.delete(
            f"/api/locks/template/{template_id}", headers=csrf(alice_csrf)
        ).status_code
        == 204
    )

    with TestClient(app_client.app) as bob_client:
        sign_in(bob_client, "bob", "bob-password-1")
        bob_csrf = bob_client.cookies.get("sshsuite_csrf") or ""
        response = bob_client.post(f"/api/locks/template/{template_id}", headers=csrf(bob_csrf))
        assert response.status_code == 201


def test_lock_api_rejects_unknown_resource_type(app_client: TestClient, users) -> None:
    sign_in(app_client, "alice", PASSWORD)
    assert app_client.get("/api/locks/database/1").status_code == 404


def test_lock_api_404_for_missing_resource(app_client: TestClient, users) -> None:
    token = sign_in(app_client, "alice", PASSWORD)
    assert app_client.get("/api/locks/template/99999").status_code == 200
    # Reading is fine (it simply does not exist), acquiring is not.
    assert app_client.post("/api/locks/template/99999", headers=csrf(token)).status_code == 404


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


def test_audit_records_login_and_logout(app_client: TestClient, users) -> None:
    token = sign_in(app_client, "alice", PASSWORD)

    actions = [event["action"] for event in app_client.get("/api/audit").json()["events"]]
    assert "auth.login.succeeded" in actions

    app_client.post("/api/auth/logout", headers=csrf(token))

    # After logout the session is gone, so read the trail through the service: the
    # logout event must have been persisted before the session was torn down.
    actions = [event.action for event in audit.list_events(limit=50)]
    assert "auth.logout" in actions


def test_audit_records_failed_login(app_client: TestClient, users) -> None:
    app_client.post("/api/auth/login", json={"username": "alice", "password": "bad"})

    # The failed attempt has no session, so it is not visible through the
    # self-scoped endpoint. Assert it through the service instead.
    events = audit.list_events(limit=50)
    actions = [event.action for event in events]
    assert "auth.login.failed" in actions
    failed = next(event for event in events if event.action == "auth.login.failed")
    assert failed.outcome == "failure"


def test_audit_is_scoped_to_the_caller(app_client: TestClient, users) -> None:
    sign_in(app_client, "alice", PASSWORD)

    with TestClient(app_client.app) as bob_client:
        sign_in(bob_client, "bob", "bob-password-1")
        events = bob_client.get("/api/audit").json()["events"]
        assert events
        assert all(event["actor_user_id"] == users["bob"] for event in events)


def test_audit_rejects_secret_material() -> None:
    """A programming error that leaks a secret must fail loudly, not silently."""
    with pytest.raises(UnsafeAuditDetail):
        audit.record(
            AuditAction.SECRET_SET,
            actor_id=None,
            actor_username=None,
            detail={"password": "hunter2"},
        )

    with pytest.raises(UnsafeAuditDetail):
        audit.record(
            AuditAction.SECRET_SET,
            actor_id=None,
            actor_username=None,
            detail={"keyPassphrase": "hunter2"},
        )

    with pytest.raises(UnsafeAuditDetail):
        audit.record(
            AuditAction.SECRET_SET,
            actor_id=None,
            actor_username=None,
            detail={"note": "-----BEGIN OPENSSH PRIVATE KEY-----\nabc"},
        )


def test_audit_allows_boolean_status_flags() -> None:
    """``passphrase_set=True`` carries nothing; it must be usable."""
    audit.record(
        AuditAction.SECRET_SET,
        actor_id=None,
        actor_username=None,
        detail={"changed": "key_passphrase", "passphrase_set": True},
    )


def test_audit_table_is_append_only_in_the_database(settings, tmp_path) -> None:
    """Even raw SQL cannot rewrite history.

    This runs the real migration rather than ``create_all``: the guard is a pair of
    SQLite triggers, which schema metadata cannot express. Testing it through
    ``create_all`` would pass while production silently lacked the protection.
    """
    from alembic import command
    from alembic.config import Config

    from app.core.config import BACKEND_DIR

    db_path = tmp_path / "append_only.db"
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", (BACKEND_DIR / "migrations").as_posix())
    config.set_main_option("sqlalchemy.url", f"sqlite:///{db_path.as_posix()}")
    command.upgrade(config, "head")

    connection = sqlite3.connect(str(db_path))
    try:
        triggers = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
        assert {"audit_events_no_update", "audit_events_no_delete"} <= triggers

        connection.execute(
            "INSERT INTO audit_events (action, outcome, created_at) "
            "VALUES ('x', 'success', '2026-01-01 00:00:00')"
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE audit_events SET action = 'tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM audit_events")
    finally:
        connection.close()


def test_audit_endpoint_is_read_only(app_client: TestClient, users) -> None:
    """There is no way to write an audit event over HTTP."""
    token = sign_in(app_client, "alice", PASSWORD)
    assert app_client.post("/api/audit", headers=csrf(token)).status_code == 405
    assert app_client.delete("/api/audit/1", headers=csrf(token)).status_code == 404


def test_deleting_a_user_with_audit_history_is_refused(app_client: TestClient, users) -> None:
    """Account deletion is refused by the foreign key, not by a trigger accident.

    ``audit_events.actor_user_id`` is ``ON DELETE RESTRICT``. Before the audit FK
    revision it was ``ON DELETE SET NULL``, which contradicted the append-only
    trigger (``SET NULL`` is an ``UPDATE``) and made deletion fail with the
    misleading ``audit_events is append-only``. The refusal is now stated by the
    constraint that owns the relationship.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models.user import User

    sign_in(app_client, "alice", PASSWORD)

    session = get_session_factory()()
    try:
        user = session.get(User, users["alice"])
        session.delete(user)
        with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
            session.commit()
        session.rollback()
    finally:
        session.close()


def test_user_with_audit_history_disappears_from_nothing(
    app_client: TestClient, users
) -> None:
    """A refused deletion leaves the account, its history and its attribution.

    Rejection is only useful if nothing was partially applied: the user row, the
    audit row and the ``actor_user_id`` link must all survive.
    """
    from sqlalchemy.exc import IntegrityError

    from app.models.user import User

    token = sign_in(app_client, "alice", PASSWORD)
    assert token

    session = get_session_factory()()
    try:
        session.delete(session.get(User, users["alice"]))
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()
    finally:
        session.close()

    session = get_session_factory()()
    try:
        assert session.get(User, users["alice"]) is not None
    finally:
        session.close()

    login_events = [
        event for event in audit.list_events(limit=50) if event.action == "auth.login.succeeded"
    ]
    assert login_events
    assert all(event.actor_user_id == users["alice"] for event in login_events)
    assert all(event.actor_username == "Alice" for event in login_events)


def test_deactivating_a_user_succeeds_and_keeps_audit_history(
    app_client: TestClient, users
) -> None:
    """Deactivation is the supported account-removal path and is not blocked.

    The restrictive foreign key must not make deactivation collateral damage: a
    user with audit history deactivates normally, and the history keeps pointing
    at that user rather than being nulled or removed.
    """
    from app.models.user import User

    sign_in(app_client, "alice", PASSWORD)

    session = get_session_factory()()
    try:
        user = session.get(User, users["alice"])
        user.is_active = False
        session.commit()
    finally:
        session.close()

    session = get_session_factory()()
    try:
        reloaded = session.get(User, users["alice"])
        assert reloaded is not None
        assert reloaded.is_active is False
    finally:
        session.close()

    login_events = [
        event for event in audit.list_events(limit=50) if event.action == "auth.login.succeeded"
    ]
    assert login_events
    assert all(event.actor_user_id == users["alice"] for event in login_events)
    assert all(event.actor_username == "Alice" for event in login_events)


def test_audit_append_only_guards_still_hold(app_client: TestClient, users) -> None:
    """The audit FK revision must not have weakened immutability.

    The FK change rebuilds ``audit_events``, and a SQLite batch rebuild drops
    triggers because they live outside the reflected table schema. This asserts
    both guards are still enforced afterwards: rows cannot be rewritten or removed.
    """
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    sign_in(app_client, "alice", PASSWORD)

    session = get_session_factory()()
    try:
        event_id = session.execute(text("SELECT id FROM audit_events LIMIT 1")).scalar()
        assert event_id is not None

        with pytest.raises(IntegrityError, match="append-only"):
            session.execute(
                text("UPDATE audit_events SET action = 'tampered' WHERE id = :id"),
                {"id": event_id},
            )
            session.commit()
        session.rollback()

        with pytest.raises(IntegrityError, match="append-only"):
            session.execute(text("DELETE FROM audit_events WHERE id = :id"), {"id": event_id})
            session.commit()
        session.rollback()
    finally:
        session.close()
