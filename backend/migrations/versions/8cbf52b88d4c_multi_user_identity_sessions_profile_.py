"""multi-user identity, sessions, SSH profiles, edit locks, audit

Revision ID: 8cbf52b88d4c
Revises: 049e9a0d77e9
Create Date: 2026-11-11 14:56:57.088044

Hand-edited after autogenerate, for three reasons:

* Autogenerate emitted ``NOT NULL`` columns with no default. On SQLite that fails
  the moment a table has rows. Each such column is added with a server default (or
  backfilled) here.
* There was no owner for pre-existing rows. A single disabled ``legacy`` account
  adopts them, so the NOT NULL constraint holds without discarding data and an
  operator can reassign later.
* The append-only guard on ``audit_events`` is SQL, not schema metadata, so
  autogenerate could not know about it.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '8cbf52b88d4c'
down_revision: str | Sequence[str] | None = '049e9a0d77e9'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_LEGACY_USERNAME = "legacy"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table('users',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('username', sa.String(length=120), nullable=False),
    sa.Column('display_name', sa.String(length=120), nullable=True),
    sa.Column('external_id', sa.String(length=255), nullable=True),
    sa.Column('password_hash', sa.Text(), nullable=True),
    sa.Column('password_changed_at', sa.DateTime(), nullable=True),
    sa.Column('is_active', sa.Boolean(), nullable=False),
    sa.Column('last_login_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_users')),
    sa.UniqueConstraint('external_id', name=op.f('uq_users_external_id')),
    sa.UniqueConstraint('username', name=op.f('uq_users_username'))
    )

    op.create_table('audit_events',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('actor_user_id', sa.Integer(), nullable=True),
    sa.Column('actor_username', sa.String(length=120), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('outcome', sa.String(length=16), nullable=False),
    sa.Column('resource_type', sa.String(length=32), nullable=True),
    sa.Column('resource_id', sa.Integer(), nullable=True),
    sa.Column('detail', sa.Text(), nullable=True),
    sa.Column('remote_addr', sa.String(length=64), nullable=True),
    sa.Column('session_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['actor_user_id'], ['users.id'], name=op.f('fk_audit_events_actor_user_id_users'), ondelete='SET NULL'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_audit_events'))
    )
    with op.batch_alter_table('audit_events', schema=None) as batch_op:
        batch_op.create_index('ix_audit_events_action_created_at', ['action', 'created_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_audit_events_actor_user_id'), ['actor_user_id'], unique=False)
        batch_op.create_index('ix_audit_events_created_at', ['created_at'], unique=False)
        batch_op.create_index('ix_audit_events_resource', ['resource_type', 'resource_id'], unique=False)

    op.create_table('resource_permissions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('resource_type', sa.Enum('template', 'workflow', name='resource_type', native_enum=False, length=32), nullable=False),
    sa.Column('resource_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('level', sa.Enum('view', 'edit', name='permission_level', native_enum=False, length=32), nullable=False),
    sa.Column('granted_by_id', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['granted_by_id'], ['users.id'], name=op.f('fk_resource_permissions_granted_by_id_users'), ondelete='SET NULL'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_resource_permissions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resource_permissions')),
    sa.UniqueConstraint('resource_type', 'resource_id', 'user_id', name='uq_resource_permissions_resource_type_resource_id_user_id')
    )
    with op.batch_alter_table('resource_permissions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_resource_permissions_user_id'), ['user_id'], unique=False)

    op.create_table('user_sessions',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('csrf_token', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('last_seen_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_reason', sa.String(length=120), nullable=True),
    sa.Column('user_agent', sa.String(length=255), nullable=True),
    sa.Column('remote_addr', sa.String(length=64), nullable=True),
    sa.Column('auth_source', sa.String(length=32), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_sessions_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_sessions')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_user_sessions_token_hash'))
    )
    with op.batch_alter_table('user_sessions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_sessions_user_id'), ['user_id'], unique=False)

    op.create_table('user_ssh_profiles',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('ssh_username', sa.String(length=120), nullable=False),
    sa.Column('default_port', sa.Integer(), nullable=False),
    sa.Column('connect_timeout_seconds', sa.Integer(), nullable=True),
    sa.Column('command_timeout_seconds', sa.Integer(), nullable=True),
    sa.Column('keepalive_seconds', sa.Integer(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_ssh_profiles_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_ssh_profiles'))
    )
    with op.batch_alter_table('user_ssh_profiles', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_ssh_profiles_user_id'), ['user_id'], unique=True)

    op.create_table('user_ssh_secrets',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('private_key_path', sa.String(length=255), nullable=True),
    sa.Column('private_key_fingerprint', sa.String(length=120), nullable=True),
    sa.Column('private_key_type', sa.String(length=40), nullable=True),
    sa.Column('private_key_comment', sa.String(length=255), nullable=True),
    sa.Column('key_passphrase_encrypted', sa.Text(), nullable=True),
    sa.Column('sudo_password_encrypted', sa.Text(), nullable=True),
    sa.Column('sudo_password_updated_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_user_ssh_secrets_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_user_ssh_secrets'))
    )
    with op.batch_alter_table('user_ssh_secrets', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_user_ssh_secrets_user_id'), ['user_id'], unique=True)

    op.create_table('resource_locks',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('resource_type', sa.Enum('template', 'workflow', name='resource_type', native_enum=False, length=32), nullable=False),
    sa.Column('resource_id', sa.Integer(), nullable=False),
    sa.Column('user_id', sa.Integer(), nullable=False),
    sa.Column('session_id', sa.Integer(), nullable=False),
    sa.Column('acquired_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('renewed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('released_at', sa.DateTime(), nullable=True),
    sa.Column('released_reason', sa.String(length=40), nullable=True),
    sa.ForeignKeyConstraint(['session_id'], ['user_sessions.id'], name=op.f('fk_resource_locks_session_id_user_sessions'), ondelete='CASCADE'),
    sa.ForeignKeyConstraint(['user_id'], ['users.id'], name=op.f('fk_resource_locks_user_id_users'), ondelete='CASCADE'),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_resource_locks'))
    )
    with op.batch_alter_table('resource_locks', schema=None) as batch_op:
        batch_op.create_index('ix_resource_locks_expires_at', ['expires_at'], unique=False)
        batch_op.create_index(batch_op.f('ix_resource_locks_user_id'), ['user_id'], unique=False)
        # The partial index is the concurrency guarantee: at most one unreleased
        # lock per resource, enforced by the database rather than by a check.
        batch_op.create_index('uq_resource_locks_active', ['resource_type', 'resource_id'], unique=True, sqlite_where=sa.text('released_at IS NULL'))

    with op.batch_alter_table('executions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('created_by_id', sa.Integer(), nullable=True))
        batch_op.create_index(batch_op.f('ix_executions_created_by_id'), ['created_by_id'], unique=False)
        batch_op.create_foreign_key(batch_op.f('fk_executions_created_by_id_users'), 'users', ['created_by_id'], ['id'], ondelete='SET NULL')

    # --- ownership -------------------------------------------------------- #
    # A pre-existing installation has credentials with no owner. Adopt them into a
    # disabled ``legacy`` account so NOT NULL can be applied without destroying
    # data. The account has no password, so it cannot be used to sign in.
    bind = op.get_bind()
    has_credentials = bind.execute(
        sa.text("SELECT COUNT(*) FROM ssh_credentials")
    ).scalar_one() > 0

    legacy_id: int | None = None
    if has_credentials:
        bind.execute(
            sa.text(
                "INSERT INTO users (username, display_name, external_id, password_hash, "
                "is_active, created_at, updated_at) "
                "VALUES (:username, :display, NULL, NULL, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"username": _LEGACY_USERNAME, "display": "Legacy (pre-multi-user)"},
        )
        legacy_id = bind.execute(
            sa.text("SELECT id FROM users WHERE username = :username"),
            {"username": _LEGACY_USERNAME},
        ).scalar_one()

    # SQLite cannot ALTER a column to NOT NULL, so batch mode rebuilds the table.
    # ``legacy_id`` is None only on an empty database, where a scalar default of 0
    # is never observed because there are no rows to copy.
    with op.batch_alter_table('ssh_credentials', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'owner_id',
                sa.Integer(),
                nullable=False,
                server_default=str(legacy_id if legacy_id is not None else 0),
            )
        )
        batch_op.drop_constraint(batch_op.f('uq_ssh_credentials_name'), type_='unique')
        batch_op.create_index(batch_op.f('ix_ssh_credentials_owner_id'), ['owner_id'], unique=False)
        batch_op.create_unique_constraint('uq_ssh_credentials_owner_id_name', ['owner_id', 'name'])
        batch_op.create_foreign_key(batch_op.f('fk_ssh_credentials_owner_id_users'), 'users', ['owner_id'], ['id'], ondelete='CASCADE')

    with op.batch_alter_table('ssh_credentials', schema=None) as batch_op:
        batch_op.alter_column('owner_id', server_default=None)

    # ``row_version`` must default to 1 for existing rows. The server default is
    # left in place so an INSERT that bypasses the ORM still gets a sane value.
    with op.batch_alter_table('templates', schema=None) as batch_op:
        batch_op.add_column(sa.Column('owner_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'))
        batch_op.drop_constraint(batch_op.f('uq_templates_name'), type_='unique')
        batch_op.create_index(batch_op.f('ix_templates_owner_id'), ['owner_id'], unique=False)
        batch_op.create_unique_constraint('uq_templates_owner_id_name', ['owner_id', 'name'])
        batch_op.create_foreign_key(batch_op.f('fk_templates_owner_id_users'), 'users', ['owner_id'], ['id'], ondelete='SET NULL')

    with op.batch_alter_table('workflows', schema=None) as batch_op:
        batch_op.add_column(sa.Column('owner_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('row_version', sa.Integer(), nullable=False, server_default='1'))
        batch_op.drop_constraint(batch_op.f('uq_workflows_name'), type_='unique')
        batch_op.create_index(batch_op.f('ix_workflows_owner_id'), ['owner_id'], unique=False)
        batch_op.create_unique_constraint('uq_workflows_owner_id_name', ['owner_id', 'name'])
        batch_op.create_foreign_key(batch_op.f('fk_workflows_owner_id_users'), 'users', ['owner_id'], ['id'], ondelete='SET NULL')

    # Pre-existing resources belong to the legacy account too, so the per-owner
    # uniqueness rule is meaningful from the first day.
    if legacy_id is not None:
        bind.execute(
            sa.text("UPDATE templates SET owner_id = :legacy WHERE owner_id IS NULL"),
            {"legacy": legacy_id},
        )
        bind.execute(
            sa.text("UPDATE workflows SET owner_id = :legacy WHERE owner_id IS NULL"),
            {"legacy": legacy_id},
        )

    # --- append-only audit ------------------------------------------------ #
    # The service layer offers no update/delete, but history must survive a future
    # programming mistake, so the database refuses to rewrite it.
    bind.execute(
        sa.text(
            "CREATE TRIGGER audit_events_no_update "
            "BEFORE UPDATE ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
        )
    )
    bind.execute(
        sa.text(
            "CREATE TRIGGER audit_events_no_delete "
            "BEFORE DELETE ON audit_events "
            "BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END"
        )
    )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    bind.execute(sa.text("DROP TRIGGER IF EXISTS audit_events_no_delete"))
    bind.execute(sa.text("DROP TRIGGER IF EXISTS audit_events_no_update"))

    with op.batch_alter_table('workflows', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_workflows_owner_id_users'), type_='foreignkey')
        batch_op.drop_constraint('uq_workflows_owner_id_name', type_='unique')
        batch_op.drop_index(batch_op.f('ix_workflows_owner_id'))
        batch_op.create_unique_constraint(batch_op.f('uq_workflows_name'), ['name'])
        batch_op.drop_column('row_version')
        batch_op.drop_column('owner_id')

    with op.batch_alter_table('templates', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_templates_owner_id_users'), type_='foreignkey')
        batch_op.drop_constraint('uq_templates_owner_id_name', type_='unique')
        batch_op.drop_index(batch_op.f('ix_templates_owner_id'))
        batch_op.create_unique_constraint(batch_op.f('uq_templates_name'), ['name'])
        batch_op.drop_column('row_version')
        batch_op.drop_column('owner_id')

    with op.batch_alter_table('ssh_credentials', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_ssh_credentials_owner_id_users'), type_='foreignkey')
        batch_op.drop_constraint('uq_ssh_credentials_owner_id_name', type_='unique')
        batch_op.drop_index(batch_op.f('ix_ssh_credentials_owner_id'))
        batch_op.create_unique_constraint(batch_op.f('uq_ssh_credentials_name'), ['name'])
        batch_op.drop_column('owner_id')

    with op.batch_alter_table('executions', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_executions_created_by_id_users'), type_='foreignkey')
        batch_op.drop_index(batch_op.f('ix_executions_created_by_id'))
        batch_op.drop_column('created_by_id')

    op.drop_table('resource_locks')
    op.drop_table('user_ssh_secrets')
    op.drop_table('user_ssh_profiles')
    op.drop_table('user_sessions')
    op.drop_table('resource_permissions')
    op.drop_table('audit_events')
    op.drop_table('users')
