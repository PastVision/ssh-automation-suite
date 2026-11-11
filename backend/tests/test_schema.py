"""The schema must exist, be migrated, and enforce the invariants we rely on.

Migration-vs-model comparison, including column parity, lives in
``tests/test_migrations.py``. This module covers the runtime side: that the
migrated database actually rejects the states the application must never reach.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError

from app.db import session as db_session
from app.models import (
    Execution,
    ExecutionHostRun,
    ExecutionStatus,
    ExecutionStepRun,
    ExecutionTemplateSnapshot,
    Template,
    TemplateVersion,
    Workflow,
    WorkflowHost,
    WorkflowStep,
)

EXPECTED_TABLES = {
    "ssh_credentials",
    "templates",
    "template_versions",
    "template_steps",
    "workflows",
    "workflow_steps",
    "workflow_hosts",
    "executions",
    "execution_host_runs",
    "execution_step_runs",
    "execution_template_snapshots",
}


def test_all_tables_created(migrated_db) -> None:
    tables = set(inspect(db_session.get_engine()).get_table_names())
    assert EXPECTED_TABLES <= tables


def test_template_version_uniqueness(migrated_db) -> None:
    with db_session.get_session_factory()() as session:
        template = Template(name="disk check")
        session.add(template)
        session.flush()
        session.add(
            TemplateVersion(
                template_id=template.id,
                version=1,
                name="disk check",
                content_checksum=TemplateVersion.compute_content_checksum(
                    name="disk check", description=None, steps=[]
                ),
            )
        )
        session.commit()

        session.add(
            TemplateVersion(
                template_id=template.id,
                version=1,
                name="disk check",
                content_checksum=TemplateVersion.compute_content_checksum(
                    name="disk check", description=None, steps=[]
                ),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()


def test_workflow_step_position_is_unique_per_workflow(migrated_db) -> None:
    with db_session.get_session_factory()() as session:
        template = Template(name="t")
        session.add(template)
        session.flush()
        version = TemplateVersion(
            template_id=template.id,
            version=1,
            name="t",
            content_checksum=TemplateVersion.compute_content_checksum(
                name="t", description=None, steps=[]
            ),
        )
        session.add(version)
        session.flush()

        workflow = Workflow(name="w")
        session.add(workflow)
        session.flush()
        first = WorkflowStep(workflow_id=workflow.id, template_version_id=version.id, position=0)
        duplicate = WorkflowStep(
            workflow_id=workflow.id, template_version_id=version.id, position=0
        )
        session.add(first)
        session.add(duplicate)

        with pytest.raises(IntegrityError):
            session.commit()


def test_deleting_template_cascades_versions_but_keeps_history(migrated_db) -> None:
    with db_session.get_session_factory()() as session:
        template = Template(name="uptime")
        session.add(template)
        session.flush()
        version = TemplateVersion(
            template_id=template.id,
            version=1,
            name="uptime",
            content_checksum="0" * 64,
        )
        session.add(version)
        session.flush()

        execution = Execution(workflow_name="nightly", status=ExecutionStatus.SUCCEEDED)
        session.add(execution)
        session.flush()
        session.add(
            ExecutionTemplateSnapshot(
                execution_id=execution.id,
                template_id=template.id,
                template_version_id=version.id,
                template_name="uptime",
                template_version=1,
                script="uptime",
                script_checksum=TemplateVersion.compute_checksum("uptime"),
            )
        )
        session.commit()

        session.delete(template)
        session.commit()

    with db_session.get_session_factory()() as session:
        snapshots = session.query(ExecutionTemplateSnapshot).all()
        assert len(snapshots) == 1
        assert snapshots[0].script == "uptime"
        assert snapshots[0].template_id is None
        assert session.query(TemplateVersion).count() == 0


def test_foreign_keys_are_enforced(migrated_db) -> None:
    with db_session.get_session_factory()() as session:
        session.add(WorkflowHost(workflow_id=9999, hostname="nope"))
        with pytest.raises(IntegrityError):
            session.commit()


def test_execution_history_links(migrated_db) -> None:
    with db_session.get_session_factory()() as session:
        execution = Execution(workflow_name="deploy", host_count=1, max_concurrent_hosts=20)
        session.add(execution)
        session.flush()

        host_run = ExecutionHostRun(
            execution_id=execution.id, hostname="web-1.example.com", port=22
        )
        session.add(host_run)
        session.flush()

        snapshot = ExecutionTemplateSnapshot(
            execution_id=execution.id,
            template_name="check",
            template_version=1,
            script="true",
            script_checksum=TemplateVersion.compute_checksum("true"),
            position=0,
        )
        session.add(snapshot)
        session.flush()

        session.add(
            ExecutionStepRun(
                host_run_id=host_run.id,
                snapshot_id=snapshot.id,
                position=0,
                template_name="check",
                exit_code=0,
            )
        )
        session.commit()

    with db_session.get_session_factory()() as session:
        loaded = session.query(ExecutionHostRun).one()
        assert loaded.hostname == "web-1.example.com"
        assert loaded.step_runs[0].snapshot_id == snapshot.id


def test_sqlite_pragmas_are_applied(migrated_db) -> None:
    with db_session.get_engine().connect() as connection:
        assert connection.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert connection.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
