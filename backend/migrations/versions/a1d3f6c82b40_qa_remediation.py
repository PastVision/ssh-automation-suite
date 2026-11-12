"""post-release QA remediation: variable sources, per-step Run-As, visibility

Revision ID: a1d3f6c82b40
Revises: a7c4e91b58d3
Create Date: 2026-11-11 23:40:00.000000

Four additive groups, each with a default that reproduces the pre-migration
behaviour exactly, so no existing row changes meaning:

``workflow_variables.source``/``static_value``
    ``source`` defaults to ``command_output``, which is what every existing row
    already is: it has a discovery command and no static value. ``static_value``
    starts NULL.

``template_steps.run_as_username``/``run_as_method``
    NULL means "run as the SSH login user", which is how every existing template
    step already behaved. A workflow-level Run-As block still applies to a step
    with NULL here, so nothing about existing workflows changes.

``workflows.visibility``/``execution_permission``
    ``private`` and ``owner_only`` are the effective pre-migration semantics, so
    every existing workflow stays exactly as isolated as it was.

``executions.log_visibility`` + cancellation columns
    ``private`` logs match the pre-migration behaviour (only the creator could
    read transcripts). The cancellation columns start NULL because no existing
    run was ever cancelled.

Alembic has no native notion of a SQLite enum, and the project stores every enum
as a length-32 string (``app.db.types.enum_column``), so the columns here are
plain ``sa.String(32)`` with a server default -- matching what the ORM writes.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1d3f6c82b40"
down_revision: str | Sequence[str] | None = "a7c4e91b58d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ENUM_LENGTH = 32
_RUN_AS_METHODS = ("nopasswd", "password")


def upgrade() -> None:
    # -- variable sources --------------------------------------------------- #
    with op.batch_alter_table("workflow_variables", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "source",
                sa.String(length=_ENUM_LENGTH),
                nullable=False,
                server_default="command_output",
            )
        )
        batch_op.add_column(sa.Column("static_value", sa.Text(), nullable=True))

    # The per-host frozen copy carries the source too, so a run in flight keeps
    # behaving as it did even if the workflow's variables are edited mid-run.
    with op.batch_alter_table("execution_variable_discoveries", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "source",
                sa.String(length=_ENUM_LENGTH),
                nullable=False,
                server_default="command_output",
            )
        )
        batch_op.add_column(sa.Column("static_value", sa.Text(), nullable=True))

    # -- per-step Run-As ---------------------------------------------------- #
    with op.batch_alter_table("template_steps", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("run_as_username", sa.String(length=120), nullable=True)
        )
        batch_op.add_column(
            sa.Column("run_as_method", sa.String(length=_ENUM_LENGTH), nullable=True)
        )

    # -- workflow visibility and execution permission ----------------------- #
    with op.batch_alter_table("workflows", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "visibility",
                sa.String(length=_ENUM_LENGTH),
                nullable=False,
                server_default="private",
            )
        )
        batch_op.add_column(
            sa.Column(
                "execution_permission",
                sa.String(length=_ENUM_LENGTH),
                nullable=False,
                server_default="owner_only",
            )
        )

    # -- execution cancellation and log visibility -------------------------- #
    with op.batch_alter_table("executions", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("cancel_requested_by_id", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("cancel_reason", sa.String(length=120), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "log_visibility",
                sa.String(length=_ENUM_LENGTH),
                nullable=False,
                server_default="private",
            )
        )
        batch_op.create_foreign_key(
            batch_op.f("fk_executions_cancel_requested_by_id_users"),
            "users",
            ["cancel_requested_by_id"],
            ["id"],
            ondelete="SET NULL",
        )

    # Server defaults are kept deliberately: they make a raw INSERT (or a rollback
    # followed by re-upgrade) consistent with the ORM, and cost nothing per row.


def downgrade() -> None:
    """Forward-only by policy; provided so a local database can be rewound.

    Dropping these columns discards data this revision introduced, including any
    per-step Run-As target and recorded cancellation, which is why production
    upgrades are one-way.
    """
    with op.batch_alter_table("executions", schema=None) as batch_op:
        batch_op.drop_constraint(
            batch_op.f("fk_executions_cancel_requested_by_id_users"), type_="foreignkey"
        )
        batch_op.drop_column("log_visibility")
        batch_op.drop_column("cancel_reason")
        batch_op.drop_column("cancel_requested_by_id")
        batch_op.drop_column("cancel_requested_at")

    with op.batch_alter_table("workflows", schema=None) as batch_op:
        batch_op.drop_column("execution_permission")
        batch_op.drop_column("visibility")

    with op.batch_alter_table("template_steps", schema=None) as batch_op:
        batch_op.drop_column("run_as_method")
        batch_op.drop_column("run_as_username")

    with op.batch_alter_table("workflow_variables", schema=None) as batch_op:
        batch_op.drop_column("static_value")
        batch_op.drop_column("source")

    with op.batch_alter_table("execution_variable_discoveries", schema=None) as batch_op:
        batch_op.drop_column("static_value")
        batch_op.drop_column("source")
