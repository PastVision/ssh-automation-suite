"""workflow manager: variables, Run-As context blocks, extended history

Revision ID: f4b81c6d2a97
Revises: e2c9f4a17b60
Create Date: 2026-11-11 22:10:00.000000

Adds the workflow-manager schema:

``workflow_variables``
    Workflow-level variable definitions (name, discovery command, timeout). The
    discovered *value* is deliberately absent: it lives only inside one host's
    execution context for the lifetime of that host workflow.

``workflow_run_as_blocks``
    A Run-As context switch occupies a slot in the workflow's single ``position``
    space. Its child templates are the ``workflow_steps`` rows that follow it, so
    nested Run-As is unrepresentable rather than merely rejected.

``execution_variables``/``execution_run_as_transitions``
    Per-host history of discovery and identity transitions. Neither stores a
    secret or a captured value.

``execution_template_snapshots.run_as_username``/``run_as_method``
    Which identity a frozen step runs under. The password is never copied here.

No table previously had a trigger, so these rebuild-free additions leave the
audit append-only guards untouched.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4b81c6d2a97"
down_revision: str | Sequence[str] | None = "e2c9f4a17b60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_AS_METHOD = sa.Enum(
    "nopasswd",
    "password",
    name="run_as_method",
    native_enum=False,
    length=32,
)
_STEP_RUN_STATUS = sa.Enum(
    "pending",
    "running",
    "succeeded",
    "failed",
    "skipped",
    "cancelled",
    name="step_run_status",
    native_enum=False,
    length=32,
)


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "workflow_variables",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("discovery_command", sa.Text(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="fk_workflow_variables_workflow_id_workflows",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_id", "name", name="uq_workflow_variables_workflow_id_name"
        ),
    )
    op.create_index(
        "ix_workflow_variables_workflow_id", "workflow_variables", ["workflow_id"]
    )

    op.create_table(
        "workflow_run_as_blocks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("target_username", sa.String(length=120), nullable=False),
        sa.Column("method", _RUN_AS_METHOD, nullable=False),
        sa.Column("password_encrypted", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["workflow_id"],
            ["workflows.id"],
            name="fk_workflow_run_as_blocks_workflow_id_workflows",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workflow_id",
            "position",
            name="uq_workflow_run_as_blocks_workflow_id_position",
        ),
    )
    op.create_index(
        "ix_workflow_run_as_blocks_workflow_id", "workflow_run_as_blocks", ["workflow_id"]
    )

    with op.batch_alter_table("execution_template_snapshots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("run_as_username", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("run_as_method", _RUN_AS_METHOD, nullable=True))
        batch_op.add_column(sa.Column("run_as_position", sa.Integer(), nullable=True))

    op.create_table(
        "execution_variable_discoveries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("host_run_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("discovery_command", sa.Text(), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", _STEP_RUN_STATUS, nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("value_length", sa.Integer(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["host_run_id"],
            ["execution_host_runs.id"],
            name="fk_execution_variable_discoveries_host_run_id_execution_host_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "host_run_id",
            "name",
            name="uq_execution_variable_discoveries_host_run_id_name",
        ),
    )
    op.create_index(
        "ix_execution_variable_discoveries_host_run_id",
        "execution_variable_discoveries",
        ["host_run_id"],
    )

    op.create_table(
        "execution_run_as_transitions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("host_run_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("target_username", sa.String(length=120), nullable=False),
        sa.Column("method", _RUN_AS_METHOD, nullable=False),
        sa.Column("status", _STEP_RUN_STATUS, nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["host_run_id"],
            ["execution_host_runs.id"],
            name="fk_execution_run_as_transitions_host_run_id_execution_host_runs",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "host_run_id",
            "position",
            name="uq_execution_run_as_transitions_host_run_id_position",
        ),
    )
    op.create_index(
        "ix_execution_run_as_transitions_host_run_id",
        "execution_run_as_transitions",
        ["host_run_id"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_execution_run_as_transitions_host_run_id",
        table_name="execution_run_as_transitions",
    )
    op.drop_table("execution_run_as_transitions")
    op.drop_index(
        "ix_execution_variable_discoveries_host_run_id",
        table_name="execution_variable_discoveries",
    )
    op.drop_table("execution_variable_discoveries")

    with op.batch_alter_table("execution_template_snapshots", schema=None) as batch_op:
        batch_op.drop_column("run_as_position")
        batch_op.drop_column("run_as_method")
        batch_op.drop_column("run_as_username")

    op.drop_index("ix_workflow_run_as_blocks_workflow_id", table_name="workflow_run_as_blocks")
    op.drop_table("workflow_run_as_blocks")
    op.drop_index("ix_workflow_variables_workflow_id", table_name="workflow_variables")
    op.drop_table("workflow_variables")
