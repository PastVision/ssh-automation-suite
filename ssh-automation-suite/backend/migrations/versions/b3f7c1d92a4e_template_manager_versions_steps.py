"""template manager: visibility, versioned step lists

Revision ID: b3f7c1d92a4e
Revises: 8cbf52b88d4c
Create Date: 2026-11-11 17:20:00.000000

Hand-edited after autogenerate, for two reasons:

* A template body moves from one ``script`` column on the version to an ordered
  ``template_steps`` list. Dropping the old columns first would destroy any
  existing body, so upgrade adds the new columns, backfills (turning each legacy
  script into a single step named "step 1"), and only then drops the old ones.
  ``content_checksum`` is computed in Python because SQLite cannot hash in SQL.
* Autogenerate emits ``NOT NULL`` without a default, which fails on SQLite as
  soon as a table has rows. New columns are added nullable with a server default
  and tightened after the backfill.
"""
import hashlib
import json
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f7c1d92a4e"
down_revision: str | Sequence[str] | None = "8cbf52b88d4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _content_checksum(name: str, description: str | None, steps: list[dict]) -> str:
    """Mirror of ``TemplateVersion.compute_content_checksum`` at this revision."""
    payload = {"name": name, "description": description, "steps": steps}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("templates", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "visibility",
                sa.String(length=32),
                nullable=False,
                server_default="private",
            )
        )

    op.create_table(
        "template_steps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("version_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("script", sa.Text(), nullable=False),
        sa.Column("interpreter", sa.String(length=40), nullable=False),
        sa.Column("timeout_seconds", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["version_id"],
            ["template_versions.id"],
            name=op.f("fk_template_steps_version_id_template_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_template_steps")),
        sa.UniqueConstraint(
            "version_id", "position", name="uq_template_steps_version_id_position"
        ),
    )
    with op.batch_alter_table("template_steps", schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f("ix_template_steps_version_id"), ["version_id"], unique=False
        )

    # New version columns, nullable so existing rows survive the ADD COLUMN.
    with op.batch_alter_table("template_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("name", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("description", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("content_checksum", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("created_by_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            batch_op.f("fk_template_versions_created_by_id_users"),
            "users",
            ["created_by_id"],
            ["id"],
            ondelete="SET NULL",
        )

    connection = op.get_bind()
    legacy = connection.execute(
        sa.text(
            "SELECT v.id, v.template_id, t.name, t.description, v.script, "
            "v.interpreter, v.timeout_seconds "
            "FROM template_versions v JOIN templates t ON t.id = v.template_id"
        )
    ).fetchall()

    for row in legacy:
        version_id, _template_id, name, description, script, interpreter, timeout = row
        if script is None:
            continue
        steps = [
            {
                "name": "step 1",
                "description": None,
                "script": script,
                "interpreter": interpreter or "bash",
                "timeout_seconds": timeout or 300,
            }
        ]
        connection.execute(
            sa.text(
                "INSERT INTO template_steps "
                "(version_id, position, name, description, script, interpreter, timeout_seconds) "
                "VALUES (:version_id, 0, :name, NULL, :script, :interpreter, :timeout)"
            ),
            {
                "version_id": version_id,
                "name": "step 1",
                "script": script,
                "interpreter": interpreter or "bash",
                "timeout": timeout or 300,
            },
        )
        connection.execute(
            sa.text(
                "UPDATE template_versions "
                "SET name = :name, description = :description, content_checksum = :checksum "
                "WHERE id = :version_id"
            ),
            {
                "name": name,
                "description": description,
                "checksum": _content_checksum(name, description, steps),
                "version_id": version_id,
            },
        )

    # Any version the loop skipped (no script) still needs the NOT NULL values.
    connection.execute(
        sa.text(
            "UPDATE template_versions SET "
            "name = COALESCE(name, (SELECT t.name FROM templates t "
            "                       WHERE t.id = template_versions.template_id), ''), "
            "content_checksum = COALESCE(content_checksum, :blank)"
        ),
        {"blank": "0" * 64},
    )

    # SQLite cannot ALTER a column to NOT NULL in place; batch mode rebuilds the
    # table, which is exactly what we want here.
    with op.batch_alter_table("template_versions", schema=None) as batch_op:
        batch_op.alter_column("name", existing_type=sa.String(length=120), nullable=False)
        batch_op.alter_column(
            "content_checksum", existing_type=sa.String(length=64), nullable=False
        )
        batch_op.drop_column("script")
        batch_op.drop_column("interpreter")
        batch_op.drop_column("timeout_seconds")
        batch_op.drop_column("script_checksum")


def downgrade() -> None:
    """Downgrade schema.

    Collapses each version's steps back into the single ``script`` column, keeping
    only position 0. Multi-step templates therefore lose their later steps on a
    downgrade; that is unavoidable when returning to a one-script-per-version
    schema, and is why this is a lossy downgrade rather than a perfect inverse.
    """
    with op.batch_alter_table("template_versions", schema=None) as batch_op:
        batch_op.add_column(sa.Column("script", sa.Text(), nullable=True))
        batch_op.add_column(
            sa.Column("interpreter", sa.String(length=40), nullable=False, server_default="bash")
        )
        batch_op.add_column(
            sa.Column("timeout_seconds", sa.Integer(), nullable=False, server_default="300")
        )
        batch_op.add_column(sa.Column("script_checksum", sa.String(length=64), nullable=True))

    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE template_versions SET "
            "script = COALESCE((SELECT s.script FROM template_steps s "
            "                   WHERE s.version_id = template_versions.id "
            "                   ORDER BY s.position LIMIT 1), ''), "
            "interpreter = COALESCE((SELECT s.interpreter FROM template_steps s "
            "                        WHERE s.version_id = template_versions.id "
            "                        ORDER BY s.position LIMIT 1), 'bash'), "
            "timeout_seconds = COALESCE((SELECT s.timeout_seconds FROM template_steps s "
            "                             WHERE s.version_id = template_versions.id "
            "                             ORDER BY s.position LIMIT 1), 300)"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE template_versions SET script_checksum = "
            "COALESCE(script_checksum, :blank)"
        ),
        {"blank": "0" * 64},
    )

    with op.batch_alter_table("template_versions", schema=None) as batch_op:
        batch_op.alter_column("script", existing_type=sa.Text(), nullable=False)
        batch_op.alter_column("script_checksum", existing_type=sa.String(length=64), nullable=False)
        batch_op.drop_constraint(
            batch_op.f("fk_template_versions_created_by_id_users"), type_="foreignkey"
        )
        batch_op.drop_column("created_by_id")
        batch_op.drop_column("content_checksum")
        batch_op.drop_column("description")
        batch_op.drop_column("name")

    with op.batch_alter_table("templates", schema=None) as batch_op:
        batch_op.drop_column("visibility")

    op.drop_index(
        op.f("ix_template_steps_version_id"), table_name="template_steps"
    )
    op.drop_table("template_steps")
