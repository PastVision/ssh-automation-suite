"""scheduling: schedules, firing ledger, and the scheduler lease

Revision ID: a7c4e91b58d3
Revises: f4b81c6d2a97
Create Date: 2026-11-12 09:20:00.000000

Adds the scheduling schema:

``schedules``
    One or more schedules per workflow. Soft-deleted (``deleted_at``) so the
    firing ledger stays readable after a schedule is retired. A **partial**
    unique index on ``(owner_id, name)`` where ``deleted_at IS NULL`` lets a name
    be reused once the earlier schedule is gone.

``schedule_runs``
    Append-only record of every logical firing time, including the ones that
    deliberately did nothing. ``UNIQUE(schedule_id, scheduled_for)`` is the
    idempotency barrier: the ledger row is written before any execution exists,
    so a duplicated firing is absorbed by the constraint.

``scheduler_lease``
    Single-row arbitration (``CHECK (id = 1)``) with a ``generation`` counter that
    increments on each acquisition, so a superseded holder can be fenced by a
    guarded write.

``executions.schedule_id``/``schedule_name``/``scheduled_for``
    Additive columns. ``schedule_id`` is ``NO ACTION`` because schedules are
    soft-deleted; the snapshots keep history readable.

The partial unique index on ``executions`` enforces the M7 overlap policy
(``skip``) in the database rather than by a check-then-insert, which is what
removes the time-of-check/time-of-use race.

Nothing here cascades into execution history: no foreign key targets
``executions``, ``execution_host_runs``, ``execution_step_runs``,
``execution_template_snapshots`` or ``schedule_runs`` with ``CASCADE``. The
additions are column-only on existing tables, so they do not rebuild a table and
therefore do not touch the audit append-only triggers.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c4e91b58d3"
down_revision: str | Sequence[str] | None = "f4b81c6d2a97"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEDULE_KIND = sa.Enum(
    "once",
    "daily",
    "weekly",
    "monthly",
    "cron",
    name="schedule_kind",
    native_enum=False,
    length=32,
)

_SCHEDULE_STATE = sa.Enum(
    "scheduled",
    "active",
    "paused",
    "disabled",
    "completed",
    "failed",
    "missed",
    name="schedule_state",
    native_enum=False,
    length=32,
)

_MISFIRE_POLICY = sa.Enum(
    "skip",
    "run_once",
    name="misfire_policy",
    native_enum=False,
    length=32,
)

_PAUSED_REASON = sa.Enum(
    "owner_inactive",
    "invalid_workflow",
    "unauthorized",
    name="paused_reason",
    native_enum=False,
    length=32,
)

_SCHEDULE_RUN_OUTCOME = sa.Enum(
    "dispatching",
    "dispatched",
    "duplicate",
    "skipped_overlap",
    "skipped_misfire",
    "skipped_owner_inactive",
    "skipped_disabled",
    "skipped_fenced",
    "invalid_workflow",
    "unauthorized",
    "error",
    name="schedule_run_outcome",
    native_enum=False,
    length=32,
)


def upgrade() -> None:
    op.create_table(
        "schedules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.Integer(), nullable=True),
        sa.Column("workflow_name", sa.String(length=120), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("schedule_kind", _SCHEDULE_KIND, nullable=False),
        sa.Column("cron_expression", sa.String(length=120), nullable=True),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("state", _SCHEDULE_STATE, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("paused_reason", _PAUSED_REASON, nullable=True),
        sa.Column("misfire_policy", _MISFIRE_POLICY, nullable=False),
        sa.Column("misfire_grace_seconds", sa.Integer(), nullable=False),
        sa.Column("last_fired_for", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("row_version", sa.Integer(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        # RESTRICT: physically deleting a user who owns schedules is refused.
        # Deactivation is the supported path; scheduled work must not vanish.
        sa.ForeignKeyConstraint(
            ["owner_id"],
            ["users.id"],
            name=op.f("fk_schedules_owner_id_users"),
            ondelete="RESTRICT",
        ),
        # SET NULL: ``workflows.delete`` is a hard delete. Clearing the pointer
        # keeps the schedule's ledger and its name snapshot readable, while a
        # cascade would erase the ledger and NO ACTION would make any workflow
        # with a schedule permanently undeletable.
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name=op.f("fk_schedules_workflow_id_workflows"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedules")),
    )
    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_schedules_owner_id"), ["owner_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_schedules_workflow_id"), ["workflow_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_schedules_state"), ["state"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_schedules_deleted_at"), ["deleted_at"], unique=False
        )
        batch_op.create_index(
            "ix_schedules_enabled_next_run_at", ["enabled", "next_run_at"], unique=False
        )
        # Partial unique: a soft-deleted name is free for reuse.
        batch_op.create_index(
            "uq_schedules_owner_id_name_active",
            ["owner_id", "name"],
            unique=True,
            sqlite_where=sa.text("deleted_at IS NULL"),
        )

    op.create_table(
        "schedule_runs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("schedule_id", sa.Integer(), nullable=False),
        sa.Column("execution_id", sa.Integer(), nullable=True),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", _SCHEDULE_RUN_OUTCOME, nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        # NO ACTION: the ledger is append-only history and must not cascade.
        sa.ForeignKeyConstraint(
            ["schedule_id"],
            ["schedules.id"],
            name=op.f("fk_schedule_runs_schedule_id_schedules"),
            ondelete="NO ACTION",
        ),
        # SET NULL: a ledger row outlives a purged execution.
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["executions.id"],
            name=op.f("fk_schedule_runs_execution_id_executions"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule_runs")),
        # Idempotency barrier for one logical firing.
        sa.UniqueConstraint(
            "schedule_id",
            "scheduled_for",
            name="uq_schedule_runs_schedule_id_scheduled_for",
        ),
    )
    with op.batch_alter_table("schedule_runs", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_schedule_runs_execution_id"), ["execution_id"], unique=False
        )
        batch_op.create_index(
            batch_op.f("ix_schedule_runs_schedule_id"), ["schedule_id"], unique=False
        )
        batch_op.create_index(
            "ix_schedule_runs_schedule_id_created_at",
            ["schedule_id", "created_at"],
            unique=False,
        )

    op.create_table(
        "scheduler_lease",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("holder_id", sa.String(length=64), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hostname", sa.String(length=255), nullable=True),
        sa.Column("pid", sa.Integer(), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("ck_scheduler_lease_single_row")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_scheduler_lease")),
    )

    with op.batch_alter_table("executions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("schedule_id", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("schedule_name", sa.String(length=120), nullable=True)
        )
        batch_op.add_column(
            sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.create_index(
            batch_op.f("ix_executions_schedule_id"), ["schedule_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_executions_schedule_id_schedules",
            "schedules",
            ["schedule_id"],
            ["id"],
            ondelete="NO ACTION",
        )
        # Overlap policy ``skip``, enforced by the database.
        batch_op.create_index(
            "uq_executions_one_active_per_schedule",
            ["schedule_id"],
            unique=True,
            sqlite_where=sa.text(
                "schedule_id IS NOT NULL AND status IN ('pending', 'running')"
            ),
        )


def downgrade() -> None:
    with op.batch_alter_table("executions", schema=None) as batch_op:
        batch_op.drop_index("uq_executions_one_active_per_schedule")
        batch_op.drop_constraint(
            "fk_executions_schedule_id_schedules", type_="foreignkey"
        )
        batch_op.drop_index(batch_op.f("ix_executions_schedule_id"))
        batch_op.drop_column("scheduled_for")
        batch_op.drop_column("schedule_name")
        batch_op.drop_column("schedule_id")

    op.drop_table("scheduler_lease")

    with op.batch_alter_table("schedule_runs", schema=None) as batch_op:
        batch_op.drop_index("ix_schedule_runs_schedule_id_created_at")
        batch_op.drop_index(batch_op.f("ix_schedule_runs_schedule_id"))
        batch_op.drop_index(batch_op.f("ix_schedule_runs_execution_id"))
    op.drop_table("schedule_runs")

    with op.batch_alter_table("schedules", schema=None) as batch_op:
        batch_op.drop_index("uq_schedules_owner_id_name_active")
        batch_op.drop_index("ix_schedules_enabled_next_run_at")
        batch_op.drop_index(batch_op.f("ix_schedules_deleted_at"))
        batch_op.drop_index(batch_op.f("ix_schedules_state"))
        batch_op.drop_index(batch_op.f("ix_schedules_workflow_id"))
        batch_op.drop_index(batch_op.f("ix_schedules_owner_id"))
    op.drop_table("schedules")
