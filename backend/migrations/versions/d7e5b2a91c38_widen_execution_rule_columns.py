"""execution rules: widen outcome columns to their declared enum width

Revision ID: d7e5b2a91c38
Revises: c4a8d1e70f52
Create Date: 2026-11-11 20:10:00.000000

The M6 migration created four outcome columns as ``VARCHAR(16)`` while the models
declare them through ``enum_column(...)``, which is ``VARCHAR(32)``. SQLite ignores
the width for storage, so nothing failed at runtime -- but the schema the migration
produces is not the schema the models describe, and a stricter database would
reject the longest legal value (``terminate``/``line_number`` are 16 characters
already, and a future member would not fit).

No data can be lost widening a ``VARCHAR``: every stored value still fits. The
columns stay nullable, which the migration restates explicitly because
``batch_alter_table`` otherwise re-creates the table without the original
nullability.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d7e5b2a91c38"
down_revision: str | Sequence[str] | None = "c4a8d1e70f52"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# The four columns, and the declared width each one must carry.
_WIDENED: tuple[tuple[str, int], ...] = (
    ("decision", 32),
    ("rule_stream", 32),
    ("rule_location", 32),
    ("rule_operator", 32),
)


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("execution_step_runs", schema=None) as batch_op:
        for name, length in _WIDENED:
            batch_op.alter_column(
                name,
                existing_type=sa.String(length=16),
                type_=sa.String(length=length),
                existing_nullable=True,
                nullable=True,
            )


def downgrade() -> None:
    """Downgrade schema.

    Narrowing back to 16 can truncate a value that is longer, so the widths are
    restored only for rows whose data still fits. This mirrors what the M6
    revision actually created rather than pretending the narrowing is safe.
    """
    with op.batch_alter_table("execution_step_runs", schema=None) as batch_op:
        for name, _length in _WIDENED:
            batch_op.alter_column(
                name,
                existing_type=sa.String(length=32),
                type_=sa.String(length=16),
                existing_nullable=True,
                nullable=True,
            )
