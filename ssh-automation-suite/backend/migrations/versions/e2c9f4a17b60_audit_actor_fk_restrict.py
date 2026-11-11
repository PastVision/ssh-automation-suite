"""audit: refuse account deletion instead of nulling actor attribution

Revision ID: e2c9f4a17b60
Revises: d7e5b2a91c38
Create Date: 2026-11-11 21:05:00.000000

``audit_events.actor_user_id`` was declared ``ON DELETE SET NULL`` by the identity
migration, alongside an append-only trigger that rejects every ``UPDATE`` on
``audit_events``. The two cannot both hold: ``SET NULL`` *is* an ``UPDATE``, so
deleting any account that owns audit history aborted with
``audit_events is append-only``. Whichever statement ran second lost.

Policy is immutable audit history and account deactivation, never account
deletion, so the foreign key now says so directly: ``ON DELETE RESTRICT``. The
outcome for callers is unchanged in the short term -- deletion was already refused
-- but the reason becomes explicit and the FK no longer contradicts the trigger.

The triggers are *not* touched. They are read from ``sqlite_master`` before the
batch rebuild and recreated afterwards, because ``batch_alter_table`` re-creates
the table from its reflected schema and a rebuild silently drops triggers, which
live outside that schema. Losing them here would weaken immutability while every
column would still look correct.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e2c9f4a17b60"
down_revision: str | Sequence[str] | None = "d7e5b2a91c38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "audit_events"
_CONSTRAINT = "fk_audit_events_actor_user_id_users"
_COLUMN = "actor_user_id"
_REFERRED_TABLE = "users"
_REFERRED_COLUMN = "id"

_GUARDED_EVENTS = ("UPDATE", "DELETE")
_TRIGGER_SUFFIX = "_no_"


def _trigger_name(event: str) -> str:
    return f"{_TABLE}{_TRIGGER_SUFFIX}{event.lower()}"


def _capture_triggers() -> list[tuple[str, str]]:
    """The append-only trigger definitions, exactly as SQLite stored them.

    Reading them back rather than restating them means this migration cannot
    drift from what the identity migration created, and a future change to the
    guard text is preserved rather than overwritten with a stale copy.
    """
    rows = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'trigger' AND tbl_name = :table AND sql IS NOT NULL"
            ),
            {"table": _TABLE},
        )
        .fetchall()
    )
    return [(row[0], row[1]) for row in rows]


def _restore_triggers(triggers: list[tuple[str, str]]) -> None:
    bind = op.get_bind()
    for name, _sql in triggers:
        bind.execute(sa.text(f'DROP TRIGGER IF EXISTS "{name}"'))
    for _name, sql in triggers:
        bind.execute(sa.text(sql))


def _assert_append_only_guards_restored() -> None:
    """Fail loudly if the rebuild dropped a guard and it was not recreated.

    Checked inside the migration so a future edit to ``_capture_triggers`` cannot
    quietly ship an ``audit_events`` table that accepts ``UPDATE`` or ``DELETE``.
    """
    bind = op.get_bind()
    present = {
        row[0]
        for row in bind.execute(
            sa.text("SELECT name FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :table"),
            {"table": _TABLE},
        )
    }
    missing = [
        _trigger_name(event) for event in _GUARDED_EVENTS if _trigger_name(event) not in present
    ]
    if missing:
        raise RuntimeError(
            "refusing to complete: append-only audit triggers missing after rebuild: "
            + ", ".join(sorted(missing))
        )


def _swap_foreign_key(ondelete: str) -> None:
    triggers = _capture_triggers()
    with op.batch_alter_table(_TABLE, schema=None) as batch_op:
        batch_op.drop_constraint(_CONSTRAINT, type_="foreignkey")
        batch_op.create_foreign_key(
            _CONSTRAINT,
            _REFERRED_TABLE,
            [_COLUMN],
            [_REFERRED_COLUMN],
            ondelete=ondelete,
        )
    _restore_triggers(triggers)
    _assert_append_only_guards_restored()


def upgrade() -> None:
    """Upgrade schema."""
    _swap_foreign_key("RESTRICT")


def downgrade() -> None:
    """Downgrade schema.

    Restores the original ``SET NULL``. Doing so reinstates the contradiction this
    revision resolves: on a downgraded database, deleting an account with audit
    history fails again with ``audit_events is append-only`` rather than nulling
    the column. That is the historical behaviour, reproduced faithfully.
    """
    _swap_foreign_key("SET NULL")
