"""execution rules: conditional step flow

Revision ID: c4a8d1e70f52
Revises: b3f7c1d92a4e
Create Date: 2026-11-11 19:05:00.000000

Adds the execution-rule columns in two places, plus the per-host outcome columns.

* ``template_steps`` and ``execution_template_snapshots`` both gain the same seven
  condition columns via ``services.conditions``' shared shape, so a run can be
  explained from its own frozen copy.
* ``execution_step_runs`` gains the *outcome*: what the evaluator saw and what the
  engine did, so the history can show why a branch was taken without re-reading
  the transcript.

Every column is nullable or has a server default. That matters twice over: SQLite
rejects ``NOT NULL`` without a default on a table that already has rows, and an
existing step must keep reading as ``CONTINUE`` (its real, prior behaviour) rather
than becoming conditional.

``host_run_status`` gains the value ``terminated``. The column is a plain
``VARCHAR(32)`` with no CHECK constraint in this schema, so no table rebuild is
required for a new member; the comment is here so a future reader does not assume
one was forgotten.

No data backfill is needed: ``action`` defaults to ``continue`` and every condition
column stays NULL, which ``RuleColumns.to_rule()`` reads back as ``CONTINUE``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4a8d1e70f52"
down_revision: str | Sequence[str] | None = "b3f7c1d92a4e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The seven condition columns, defined once and copied to both rule owners so the
# definition site and the snapshot cannot drift apart.
_CONDITION_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("condition_stream", sa.String(length=16)),
    ("condition_location", sa.String(length=16)),
    ("condition_operator", sa.String(length=16)),
    ("condition_expected", sa.Text()),
    ("condition_line_number", sa.Integer()),
    ("condition_anchor", sa.String(length=1000)),
)

# Outcome columns recorded per host, per step.
_OUTCOME_COLUMNS: tuple[tuple[str, sa.types.TypeEngine], ...] = (
    ("decision", sa.String(length=16)),
    ("rule_action", sa.String(length=32)),
    ("rule_stream", sa.String(length=16)),
    ("rule_location", sa.String(length=16)),
    ("rule_operator", sa.String(length=16)),
    ("rule_anchor", sa.String(length=1000)),
    ("rule_line_number", sa.Integer()),
    ("condition_matched", sa.Boolean()),
    ("anchor_found", sa.Boolean()),
    ("evaluated_line", sa.Integer()),
    ("observed", sa.String(length=500)),
    ("evaluation_error", sa.Text()),
)


def _add_rule_columns(table: str) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "action",
                sa.String(length=32),
                nullable=False,
                server_default="continue",
            )
        )
        for name, type_ in _CONDITION_COLUMNS:
            batch_op.add_column(sa.Column(name, type_, nullable=True))
        batch_op.add_column(
            sa.Column(
                "condition_case_sensitive",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("0"),
            )
        )


def _drop_rule_columns(table: str) -> None:
    with op.batch_alter_table(table, schema=None) as batch_op:
        batch_op.drop_column("condition_case_sensitive")
        for name, _type in reversed(_CONDITION_COLUMNS):
            batch_op.drop_column(name)
        batch_op.drop_column("action")


def upgrade() -> None:
    """Upgrade schema."""
    _add_rule_columns("template_steps")
    _add_rule_columns("execution_template_snapshots")

    with op.batch_alter_table("execution_step_runs", schema=None) as batch_op:
        for name, type_ in _OUTCOME_COLUMNS:
            batch_op.add_column(sa.Column(name, type_, nullable=True))
        batch_op.add_column(
            sa.Column("rule_case_sensitive", sa.Boolean(), nullable=True)
        )


def downgrade() -> None:
    """Downgrade schema.

    Lossy by necessity: a conditional step becomes an unconditional one, so any
    run that branched loses the record of why. It is a downgrade of a feature the
    schema then cannot express, not a recoverable state.
    """
    with op.batch_alter_table("execution_step_runs", schema=None) as batch_op:
        batch_op.drop_column("rule_case_sensitive")
        for name, _type in reversed(_OUTCOME_COLUMNS):
            batch_op.drop_column(name)

    _drop_rule_columns("execution_template_snapshots")
    _drop_rule_columns("template_steps")
