"""Migration coverage: the schema a deployment gets must match the models.

These tests treat the migration chain as the specification, because the chain is
what actually runs against production databases. They cover three questions the
older column-parity check did not:

* Does a fresh database reach head, one revision at a time?
* Is the resulting schema *structurally* identical to the models -- types,
  nullability, primary/foreign keys, uniques, indexes (including partial ones),
  defaults and triggers -- rather than merely having the same column names?
* Can every revision be downgraded and re-upgraded, so a failed deploy is
  recoverable?

Each comparison is also proven to detect a real defect in
``test_schema_drift_is_detected``; a comparison that can never fail is worthless.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import String

from app.db.base import Base
from tests import migration_support

pytestmark = pytest.mark.migration


# --------------------------------------------------------------------------- #
# Fresh creation and upgrade paths
# --------------------------------------------------------------------------- #


def test_migration_chain_is_linear_and_discovered() -> None:
    """The revision list the chain tests iterate is the real one, in order.

    ``REVISIONS`` is discovered from Alembic rather than hardcoded, so this pins
    the properties the chain tests rely on: non-empty, no duplicates, and exactly
    one head. A branched migration history would make ``upgrade head`` ambiguous.
    """
    from alembic.script import ScriptDirectory

    assert migration_support.REVISIONS, "no migrations were discovered"
    assert len(set(migration_support.REVISIONS)) == len(migration_support.REVISIONS)

    config = migration_support.alembic_config(
        migration_support.sqlite_url(Path("unused.db"))
    )
    script = ScriptDirectory.from_config(config)
    heads = list(script.get_heads())
    assert heads == [migration_support.REVISIONS[-1]], (
        f"expected exactly one head matching the last revision, found {heads}"
    )


def test_audit_actor_fk_is_restrict_not_set_null(migrated_template_db: Path) -> None:
    """The audit FK refuses account deletion instead of nulling attribution.

    ``ON DELETE SET NULL`` and the append-only trigger cannot coexist: the trigger
    rejects every ``UPDATE``, and ``SET NULL`` is an ``UPDATE``. RESTRICT states
    the policy -- immutable history, deactivated accounts -- without contradicting
    immutability.
    """
    connection = sqlite3.connect(str(migrated_template_db))
    try:
        fks = [
            (row[2], row[3], row[4], row[6])
            for row in connection.execute("PRAGMA foreign_key_list(audit_events)")
        ]
    finally:
        connection.close()

    assert fks == [("users", "actor_user_id", "id", "RESTRICT")], fks


def test_audit_fk_revision_preserved_the_append_only_triggers(tmp_path: Path) -> None:
    """Rebuilding the table for the FK change did not drop the audit guards.

    A SQLite batch rebuild re-creates the table from its reflected schema, and
    triggers are not part of that schema, so they are dropped silently. This
    compares the trigger bodies at head against the text the identity migration
    wrote, so a rebuild that forgets to restore them cannot pass by comparing
    against a revision that already had them.
    """
    database = tmp_path / "triggers.db"
    migration_support.upgrade_to(migration_support.sqlite_url(database), "head")

    connection = sqlite3.connect(str(database))
    try:
        actual = {
            row[0]: " ".join(row[1].split())
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type='trigger' AND tbl_name='audit_events'"
            )
        }
    finally:
        connection.close()

    # Restated here on purpose: the guard text is the security property, so the
    # test asserts it literally rather than trusting a migration to have copied it.
    assert actual == {
        "audit_events_no_update": (
            "CREATE TRIGGER audit_events_no_update BEFORE UPDATE ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
        ),
        "audit_events_no_delete": (
            "CREATE TRIGGER audit_events_no_delete BEFORE DELETE ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
        ),
    }, actual


def test_audit_history_survives_the_fk_revision(tmp_path: Path) -> None:
    """The table rebuild keeps every audit row and its attribution.

    The FK change rewrites ``audit_events``; history written before the upgrade
    must come through intact, still pointing at its actor.
    """
    database = tmp_path / "audit-history.db"
    url = migration_support.sqlite_url(database)
    migration_support.upgrade_to(url, "d7e5b2a91c38")

    connection = sqlite3.connect(str(database))
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO users (username, is_active, created_at, updated_at) "
            "VALUES ('legacy', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00')"
        )
        connection.execute(
            "INSERT INTO audit_events (actor_user_id, actor_username, action, outcome, created_at) "
            "VALUES (1, 'Legacy', 'auth.login.succeeded', 'success', '2026-01-01 00:00:00')"
        )
        connection.commit()
    finally:
        connection.close()

    migration_support.upgrade_to(url, "head")

    connection = sqlite3.connect(str(database))
    try:
        rows = connection.execute(
            "SELECT actor_user_id, actor_username, action FROM audit_events"
        ).fetchall()
    finally:
        connection.close()

    assert rows == [(1, "Legacy", "auth.login.succeeded")]


def test_downgrading_the_fk_revision_restores_set_null(tmp_path: Path) -> None:
    """The FK change is reversible, including the trigger restoration."""
    database = tmp_path / "fk-roundtrip.db"
    url = migration_support.sqlite_url(database)
    migration_support.upgrade_to(url, "head")

    migration_support.downgrade_to(url, "d7e5b2a91c38")

    connection = sqlite3.connect(str(database))
    try:
        fks = [
            (row[2], row[3], row[4], row[6])
            for row in connection.execute("PRAGMA foreign_key_list(audit_events)")
        ]
        triggers = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
    finally:
        connection.close()

    assert fks == [("users", "actor_user_id", "id", "SET NULL")], fks
    assert triggers == {"audit_events_no_update", "audit_events_no_delete"}


def test_fresh_database_reaches_head(tmp_path: Path) -> None:
    """A brand-new database is created entirely from the migration chain."""
    database = tmp_path / "fresh.db"
    migration_support.upgrade_to(migration_support.sqlite_url(database), "head")

    connection = sqlite3.connect(str(database))
    try:
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()

    assert revision == (migration_support.REVISIONS[-1],)
    assert {table.name for table in Base.metadata.sorted_tables} <= tables


@pytest.mark.parametrize("revision", migration_support.REVISIONS)
def test_each_revision_is_reachable_from_a_fresh_database(revision: str, tmp_path: Path) -> None:
    """Every revision applies cleanly on top of its predecessor.

    Parameterised over the whole chain, so a future revision that only works on a
    database already at head is caught here rather than during a deploy.
    """
    database = tmp_path / f"at-{revision}.db"
    migration_support.upgrade_to(migration_support.sqlite_url(database), revision)

    connection = sqlite3.connect(str(database))
    try:
        current = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        connection.close()

    assert current == (revision,)


def test_upgrading_incrementally_matches_upgrading_directly(tmp_path: Path) -> None:
    """Stepping revision by revision yields the same schema as one jump.

    Alembic applies the same scripts either way, so a mismatch would mean a
    revision reads state its predecessor did not necessarily create.
    """
    stepped = tmp_path / "stepped.db"
    direct = tmp_path / "direct.db"

    url = migration_support.sqlite_url(stepped)
    for revision in migration_support.REVISIONS:
        migration_support.upgrade_to(url, revision)
    migration_support.upgrade_to(migration_support.sqlite_url(direct), "head")

    assert migration_support.schema_fingerprint(stepped) == migration_support.schema_fingerprint(
        direct
    )


def test_representative_earlier_revision_carries_data_forward(tmp_path: Path) -> None:
    """A database written at an earlier revision upgrades without losing rows.

    The revision before the execution-rule columns is the representative case:
    it is the last schema that predates the M6 work, so it is exactly what an
    existing installation would be sitting on.
    """
    database = tmp_path / "legacy.db"
    url = migration_support.sqlite_url(database)
    migration_support.upgrade_to(url, "b3f7c1d92a4e")

    # Raw SQL naming only columns that existed at this revision. Writing through
    # the current ORM would emit every column it knows about, including ones this
    # revision does not have, and so would test the ORM rather than the migration.
    connection = sqlite3.connect(str(database))
    try:
        connection.execute(
            "INSERT INTO executions "
            "(workflow_name, status, \"trigger\", max_concurrent_hosts, host_count, "
            " succeeded_host_count, failed_host_count, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-run", "succeeded", "manual", 20, 1, 0, 0, "2026-01-01 00:00:00"),
        )
        connection.commit()
    finally:
        connection.close()

    migration_support.upgrade_to(url, "head")

    connection = sqlite3.connect(str(database))
    try:
        rows = connection.execute("SELECT workflow_name FROM executions").fetchall()
        # The new outcome columns must exist and be empty for the legacy row.
        step_runs = connection.execute("SELECT decision FROM execution_step_runs").fetchall()
    finally:
        connection.close()

    assert rows == [("legacy-run",)]
    assert step_runs == []


# --------------------------------------------------------------------------- #
# Structural compatibility
# --------------------------------------------------------------------------- #


def test_migrated_schema_matches_models_structurally(migrated_template_db: Path) -> None:
    """No type, nullability, key, unique, index or default differences remain.

    Uses Alembic's own ``compare_metadata`` with ``compare_type=True`` -- the same
    comparison ``alembic revision --autogenerate`` performs -- so this fails on
    exactly what the next autogenerate would try to change.
    """
    engine = migration_support.database_engine(migrated_template_db)
    try:
        differences = migration_support.alembic_differences(engine)
    finally:
        engine.dispose()

    assert not differences, "Schema drifted from the models:\n" + "\n".join(
        str(difference) for difference in differences
    )


def test_migrated_schema_matches_models_in_sqlite_specifics(migrated_template_db: Path) -> None:
    """Partial indexes and append-only triggers survive the migration.

    Alembic compares index *names and columns*, so it cannot tell whether
    ``uq_resource_locks_active`` still carries ``WHERE released_at IS NULL``. The
    audit triggers exist in no metadata whatsoever. Both are load-bearing: without
    the predicate a released lock keeps blocking, and without the triggers history
    can be rewritten.
    """
    differences = migration_support.sqlite_object_differences(migrated_template_db)
    assert not differences, "SQLite-specific schema drifted:\n" + "\n".join(
        str(difference) for difference in differences
    )


def test_every_declared_index_exists_in_the_migrated_database(
    migrated_template_db: Path,
) -> None:
    """Every index the models declare is actually created by the migrations."""
    connection = sqlite3.connect(str(migrated_template_db))
    try:
        present = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
    finally:
        connection.close()

    declared = {
        str(index.name) for table in Base.metadata.sorted_tables for index in table.indexes
    }
    assert declared <= present, f"indexes declared but never created: {sorted(declared - present)}"


def test_partial_index_actually_permits_a_new_lock_after_release(
    migrated_template_db: Path,
) -> None:
    """The predicate is functionally verified, not just textually compared.

    Two rows for the same resource coexist once the first is released. If the
    ``WHERE`` clause were dropped, the second insert would violate the unique
    index and this test would fail.
    """
    database = migrated_template_db.parent / "partial-index-check.db"
    migration_support.copy_database(migrated_template_db, database)

    from datetime import UTC, datetime, timedelta

    from sqlalchemy.orm import Session

    from app.models import ResourceLock, ResourceType, User, UserSession

    now = datetime.now(UTC)
    engine = migration_support.database_engine(database)
    try:
        with Session(engine) as session:
            session.add(User(username="lock-user", display_name="Lock", is_active=True))
            session.flush()
            user_id = session.query(User).one().id
            first = UserSession(
                user_id=user_id,
                token_hash="a" * 64,
                csrf_token="b" * 64,
                expires_at=now + timedelta(hours=1),
            )
            second = UserSession(
                user_id=user_id,
                token_hash="c" * 64,
                csrf_token="d" * 64,
                expires_at=now + timedelta(hours=1),
            )
            session.add_all([first, second])
            session.flush()

            # A released lock does not block a new one for the same resource.
            session.add(
                ResourceLock(
                    resource_type=ResourceType.TEMPLATE,
                    resource_id=1,
                    user_id=user_id,
                    session_id=first.id,
                    acquired_at=now,
                    renewed_at=now,
                    expires_at=now + timedelta(minutes=10),
                    released_at=now,
                )
            )
            session.add(
                ResourceLock(
                    resource_type=ResourceType.TEMPLATE,
                    resource_id=1,
                    user_id=user_id,
                    session_id=second.id,
                    acquired_at=now,
                    renewed_at=now,
                    expires_at=now + timedelta(minutes=10),
                    released_at=None,
                )
            )
            session.commit()

            # But a second *active* lock for the same resource is still refused.
            from sqlalchemy.exc import IntegrityError

            session.add(
                ResourceLock(
                    resource_type=ResourceType.TEMPLATE,
                    resource_id=1,
                    user_id=user_id,
                    session_id=first.id,
                    acquired_at=now,
                    renewed_at=now,
                    expires_at=now + timedelta(minutes=10),
                    released_at=None,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        engine.dispose()


def test_foreign_keys_are_declared_for_every_model_relationship(
    migrated_template_db: Path,
) -> None:
    """Model foreign keys exist in the migrated database with the right target."""
    connection = sqlite3.connect(str(migrated_template_db))
    try:
        for table in Base.metadata.sorted_tables:
            actual = migration_support.foreign_keys(connection, table.name)
            for column in table.columns:
                for fk in column.foreign_keys:
                    expected = (
                        column.name,
                        fk.column.table.name,
                        fk.column.name,
                        (fk.ondelete or "NO ACTION").upper(),
                    )
                    assert expected in actual, (
                        f"{table.name}.{column.name}: expected foreign key {expected}, "
                        f"database has {sorted(actual)}"
                    )
    finally:
        connection.close()


def test_primary_keys_match_the_models(migrated_template_db: Path) -> None:
    """Primary key columns are exactly what the models declare."""
    connection = sqlite3.connect(str(migrated_template_db))
    try:
        for table in Base.metadata.sorted_tables:
            info = migration_support.table_columns(connection, table.name)
            actual = tuple(name for name, (_, _, _, pk) in info.items() if pk)
            expected = tuple(column.name for column in table.primary_key.columns)
            assert actual == expected, f"{table.name}: pk {actual} != model {expected}"
    finally:
        connection.close()


def test_unique_constraints_are_enforced_in_the_migrated_database(
    migrated_template_db: Path,
) -> None:
    """Uniqueness is proven by trying to violate it, not by listing its name."""
    database = migrated_template_db.parent / "unique-check.db"
    migration_support.copy_database(migrated_template_db, database)

    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import Session

    from app.models import User

    engine = migration_support.database_engine(database)
    try:
        with Session(engine) as session:
            session.add(User(username="dup", display_name="Dup", is_active=True))
            session.commit()

            session.add(User(username="dup", display_name="Other", is_active=True))
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        engine.dispose()


# --------------------------------------------------------------------------- #
# Downgrade / re-upgrade
# --------------------------------------------------------------------------- #


def test_downgrade_to_base_and_reupgrade(tmp_path: Path) -> None:
    """The whole chain is reversible, so a bad deploy can be rolled back."""
    database = tmp_path / "roundtrip.db"
    url = migration_support.sqlite_url(database)

    migration_support.upgrade_to(url, "head")

    connection = sqlite3.connect(str(database))
    try:
        at_head = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    finally:
        connection.close()

    migration_support.downgrade_to(url, "base")

    connection = sqlite3.connect(str(database))
    try:
        after_downgrade = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        triggers = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        }
    finally:
        connection.close()

    # Only Alembic's bookkeeping table survives a full downgrade.
    assert after_downgrade <= {"alembic_version"}
    assert "users" not in after_downgrade
    assert triggers == set()

    migration_support.upgrade_to(url, "head")

    connection = sqlite3.connect(str(database))
    try:
        after_reupgrade = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        revision = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    finally:
        connection.close()

    assert after_reupgrade == at_head
    assert revision == (migration_support.REVISIONS[-1],)


@pytest.mark.parametrize("revision", migration_support.REVISIONS[:-1])
def test_each_revision_downgrades_stably(revision: str, tmp_path: Path) -> None:
    """Stepping down through each revision and back is lossless for the schema.

    A revision whose ``downgrade`` leaves an object behind makes a real rollback
    unsafe. Comparing the full ``sqlite_master`` before and after the round trip
    catches leftovers, not merely a final revision number.
    """
    database = tmp_path / f"down-{revision}.db"
    url = migration_support.sqlite_url(database)
    migration_support.upgrade_to(url, "head")

    before = migration_support.schema_fingerprint(database)
    migration_support.downgrade_to(url, revision)
    migration_support.upgrade_to(url, "head")
    after = migration_support.schema_fingerprint(database)

    assert after == before, f"round trip through {revision} did not restore the schema"


# --------------------------------------------------------------------------- #
# The comparisons must be able to fail
# --------------------------------------------------------------------------- #


def test_schema_drift_is_detected(migrated_template_db: Path) -> None:
    """A missing index in the migration is detected, not tolerated.

    This is the regression test for the failure mode that motivated migration
    testing in the first place: a schema object that exists on the models but is
    never created by a migration. The comparator is exercised against a database
    that is genuinely missing the object.
    """
    database = migrated_template_db.parent / "drift-index.db"
    migration_support.copy_database(migrated_template_db, database)

    connection = sqlite3.connect(str(database))
    try:
        connection.execute("DROP INDEX ix_templates_owner_id")
        connection.commit()
    finally:
        connection.close()

    engine = migration_support.database_engine(database)
    try:
        differences = migration_support.alembic_differences(engine)
    finally:
        engine.dispose()

    assert any("ix_templates_owner_id" in str(difference) for difference in differences), (
        "dropping an index went undetected: " f"{[str(d) for d in differences]}"
    )


def test_missing_partial_index_predicate_is_detected(migrated_template_db: Path) -> None:
    """Losing the ``WHERE`` clause on the lock index is detected.

    Alembic's metadata comparison cannot see this, which is exactly why the
    SQLite-specific comparator exists. The index is recreated without its
    predicate to prove the difference is caught.
    """
    database = migrated_template_db.parent / "drift-partial.db"
    migration_support.copy_database(migrated_template_db, database)

    connection = sqlite3.connect(str(database))
    try:
        connection.execute("DROP INDEX uq_resource_locks_active")
        connection.execute(
            "CREATE UNIQUE INDEX uq_resource_locks_active "
            "ON resource_locks (resource_type, resource_id)"
        )
        connection.commit()
    finally:
        connection.close()

    differences = migration_support.sqlite_object_differences(database)
    assert any(
        difference.kind == "partial_index_predicate" for difference in differences
    ), f"a missing partial-index predicate went undetected: {[str(d) for d in differences]}"


def test_missing_trigger_is_detected(migrated_template_db: Path) -> None:
    """Dropping the append-only audit trigger is detected."""
    database = migrated_template_db.parent / "drift-trigger.db"
    migration_support.copy_database(migrated_template_db, database)

    connection = sqlite3.connect(str(database))
    try:
        connection.execute("DROP TRIGGER audit_events_no_update")
        connection.commit()
    finally:
        connection.close()

    differences = migration_support.sqlite_object_differences(database)
    assert any(
        difference.kind == "missing_trigger" and "audit_events_no_update" in str(difference)
        for difference in differences
    ), f"a dropped trigger went undetected: {[str(d) for d in differences]}"


def test_type_drift_is_detected(migrated_template_db: Path) -> None:
    """A column narrower than the model declares is detected.

    This is the second real defect this suite had: the M6 migration created four
    outcome columns as ``VARCHAR(16)`` while the models declare ``VARCHAR(32)``.
    SQLite tolerates the mismatch and every runtime test passed, so only a
    type-aware comparison notices.
    """
    database = migrated_template_db.parent / "drift-type.db"
    migration_support.copy_database(migrated_template_db, database)

    # Narrow the column the way Alembic itself would, via batch mode.
    engine = migration_support.database_engine(database)
    try:
        with engine.begin() as connection:
            from alembic.migration import MigrationContext
            from alembic.operations import Operations

            context = MigrationContext.configure(connection, opts={"render_as_batch": True})
            operations = Operations(context)
            with operations.batch_alter_table("execution_step_runs") as batch:
                batch.alter_column("decision", type_=String(8), existing_nullable=True)

        differences = migration_support.alembic_differences(engine)
    finally:
        engine.dispose()

    assert any(
        difference.kind == "modify_type" and "execution_step_runs.decision" in str(difference)
        for difference in differences
    ), f"a narrowed column type went undetected: {[str(d) for d in differences]}"


def test_column_missing_from_migration_is_detected(migrated_template_db: Path) -> None:
    """A model column with no matching migration column is reported.

    This is precisely the original bug: the models gained execution-rule columns
    and the developer's database never got them, so every write failed with
    ``table template_steps has no column named action``. Removing the column here
    reproduces that state.
    """
    database = migrated_template_db.parent / "drift-column.db"
    migration_support.copy_database(migrated_template_db, database)

    engine = migration_support.database_engine(database)
    try:
        with engine.begin() as connection:
            from alembic.migration import MigrationContext
            from alembic.operations import Operations

            context = MigrationContext.configure(connection, opts={"render_as_batch": True})
            operations = Operations(context)
            with operations.batch_alter_table("template_steps") as batch:
                batch.drop_column("condition_operator")

        differences = migration_support.alembic_differences(engine)
    finally:
        engine.dispose()

    assert any(
        difference.kind == "add_column" and "template_steps.condition_operator" in str(difference)
        for difference in differences
    ), f"a model-only column went undetected: {[str(d) for d in differences]}"


def test_nullability_drift_is_detected(migrated_template_db: Path) -> None:
    """A NOT NULL constraint the models declare but the migration lost is caught."""
    database = migrated_template_db.parent / "drift-nullable.db"
    migration_support.copy_database(migrated_template_db, database)

    engine = migration_support.database_engine(database)
    try:
        with engine.begin() as connection:
            from alembic.migration import MigrationContext
            from alembic.operations import Operations

            context = MigrationContext.configure(connection, opts={"render_as_batch": True})
            operations = Operations(context)
            with operations.batch_alter_table("templates") as batch:
                batch.alter_column("name", existing_type=String(120), nullable=True)

        differences = migration_support.alembic_differences(engine)
    finally:
        engine.dispose()

    assert any(
        difference.kind == "modify_nullable" and "templates.name" in str(difference)
        for difference in differences
    ), f"a nullability change went undetected: {[str(d) for d in differences]}"


def test_declared_server_defaults_are_created_by_the_migration(
    migrated_template_db: Path,
) -> None:
    """Every ``server_default`` the models declare exists with the same value.

    Alembic's own default comparison is unusable on SQLite here (it mixes Python
    ``default=`` with ``server_default=``), so this asserts the narrower, true
    claim: what the models say the database should default to, the database does.
    """
    differences = migration_support.server_default_differences(migrated_template_db)
    assert not differences, "Server defaults drifted:\n" + "\n".join(
        str(difference) for difference in differences
    )


def test_undeclared_server_defaults_are_known(migrated_template_db: Path) -> None:
    """Database-only defaults are pinned, so a new one is a deliberate change.

    Each column here carries a default that the ORM does not declare. They are
    needed because SQLite cannot add a NOT NULL column to a populated table
    without one, and they remain useful as a safety net for raw inserts. Pinning
    the exact set means a future migration cannot quietly introduce another.

    The post-1.0.0 remediation migration adds five, one per newly non-null column:
    ``workflow_variables.source`` and ``execution_variable_discoveries.source``
    (command_output is the pre-existing behaviour), ``workflows.visibility`` and
    ``executions.log_visibility`` (private), and
    ``workflows.execution_permission`` (owner_only).
    """
    assert migration_support.undeclared_server_defaults(migrated_template_db) == {
        "templates.row_version",
        "templates.visibility",
        "workflows.row_version",
        "workflows.visibility",
        "workflows.execution_permission",
        "executions.log_visibility",
        "workflow_variables.source",
        "execution_variable_discoveries.source",
    }


def test_check_constraints_present_in_migrations_are_enumerated(migrated_template_db: Path) -> None:
    """Record which CHECK constraints the migrations actually create.

    The only CHECK today is the ``scheduler_lease`` single-row guard, which *is*
    declared in the model, so a diff against that list means a migration added one
    outside the ORM's knowledge. Pinned explicitly rather than skipped, so adding
    another is a deliberate, visible change.
    """
    import re

    connection = sqlite3.connect(str(migrated_template_db))
    try:
        definitions = {
            name: sql
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='table'"
            )
        }
    finally:
        connection.close()

    # Match the ``CHECK (`` keyword only: a substring search also matches column
    # names such as ``content_checksum``.
    pattern = re.compile(r"\bCHECK\s*\(", re.IGNORECASE)
    with_check = {
        table: sql for table, sql in definitions.items() if sql and pattern.search(sql)
    }
    assert set(with_check) == {"scheduler_lease"}, (
        "unexpected CHECK constraints discovered: "
        f"{sorted(set(with_check) - {'scheduler_lease'})}"
    )
    # The lease table must never accept a second row; that is its whole job.
    assert "id = 1" in with_check["scheduler_lease"]
