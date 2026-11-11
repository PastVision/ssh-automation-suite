"""Migration-backed test databases and schema comparison.

Test databases are built by running the real Alembic migrations, never by
``Base.metadata.create_all``. The reason is concrete: the ORM metadata and the
migration chain are two separate descriptions of the same schema, and only the
migrations are what a deployment actually runs. Building tests from metadata
made the suite structurally blind to migration drift -- a missing column in a
migration passed 366 tests and then broke ``POST /api/templates`` at runtime.

Building the schema by migrating is not free, so it is paid once per session:
``upgrade head`` takes roughly 0.6s, while copying the resulting file costs a
couple of milliseconds. Each test therefore gets its own database file (full
isolation, including WAL sidecars) that is a byte copy of an already-migrated
template, rather than its own migration run.
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine

from app.core.config import BACKEND_DIR
from app.db.base import Base


def _discover_revisions() -> tuple[str, ...]:
    """Every revision in the chain, oldest first, read from Alembic itself.

    Deliberately not a hardcoded list: a constant would silently stop covering new
    migrations, and the chain tests would keep passing while testing less.
    """
    from alembic.script import ScriptDirectory

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", (BACKEND_DIR / "migrations").as_posix())
    script = ScriptDirectory.from_config(config)
    newest_first = [revision.revision for revision in script.walk_revisions("base", "heads")]
    return tuple(reversed(newest_first))


REVISIONS: tuple[str, ...] = _discover_revisions()

# ``sqlite_autoindex_*`` entries are created by SQLite itself for UNIQUE/PRIMARY
# KEY declarations. They are not declared anywhere, so comparing them as if they
# were would report permanent phantom differences.
_IMPLICIT_INDEX_PREFIX = "sqlite_autoindex_"


def alembic_config(database_url: str) -> Config:
    """An Alembic config pointed at ``database_url`` with logging suppressed."""
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", (BACKEND_DIR / "migrations").as_posix())
    config.set_main_option("sqlalchemy.url", database_url)
    # env.py reads this attribute; without it Alembic reconfigures the root logger
    # from alembic.ini on every migration and floods pytest's captured output.
    config.attributes["configure_logger"] = False
    return config


def upgrade_to(database_url: str, revision: str) -> None:
    command.upgrade(alembic_config(database_url), revision)


def downgrade_to(database_url: str, revision: str) -> None:
    command.downgrade(alembic_config(database_url), revision)


def sqlite_url(path: Path) -> str:
    return f"sqlite:///{path.as_posix()}"


@dataclass(frozen=True)
class SchemaDifference:
    """One concrete way the migrated schema diverges from the models."""

    kind: str
    location: str
    detail: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.location}: {self.detail}"




def _normalise_default(value: Any) -> str | None:
    """Reduce a server default to a comparable literal.

    Reflection reports ``'continue'`` (quoted, because that is the DDL SQLite
    stored) while the model holds ``continue``. Both mean the same default, so
    the comparison strips one layer of quoting rather than demanding identical
    rendering. Anything else is compared verbatim.
    """
    if value is None:
        return None
    text = str(value).strip()
    if text.startswith("'") and text.endswith("'") and len(text) >= 2:
        return text[1:-1]
    return text


def alembic_differences(engine: Engine) -> list[SchemaDifference]:
    """Type/nullability/PK/FK/unique/index differences reported by Alembic.

    This is Alembic's own ``compare_metadata``, the same code ``alembic revision
    --autogenerate`` uses, so it agrees with what a developer would be told when
    generating the next migration. ``compare_type=True`` is essential: without it
    a ``VARCHAR(16)`` column described as ``VARCHAR(32)`` is invisible.
    """
    with engine.connect() as connection:
        context = MigrationContext.configure(
            connection,
            opts={"compare_type": True, "render_as_batch": True},
        )
        raw = compare_metadata(context, Base.metadata)

    differences: list[SchemaDifference] = []
    for entry in raw:
        # compare_metadata yields either a tuple describing one change or a list
        # of tuples for a grouped change (e.g. several columns added at once).
        for item in entry if isinstance(entry, list) else [entry]:
            kind = str(item[0])
            if kind in {"add_column", "remove_column"}:
                table, column = item[2], getattr(item[3], "name", item[3])
                detail = (
                    "column missing from database"
                    if kind == "add_column"
                    else "column not declared in models"
                )
                differences.append(SchemaDifference(kind, f"{table}.{column}", detail))
            elif kind == "modify_type":
                table, column = item[2], item[3]
                # Tuple layout: (kind, schema, table, column, kw, existing_type, model_type)
                existing_type = item[5] if len(item) > 5 else None
                wanted_type = item[6] if len(item) > 6 else None
                differences.append(
                    SchemaDifference(
                        kind,
                        f"{table}.{column}",
                        f"database {existing_type!r} vs model {wanted_type!r}",
                    )
                )
            elif kind == "modify_nullable":
                table, column = item[2], item[3]
                existing = item[4].get("existing_nullable") if isinstance(item[4], dict) else None
                differences.append(
                    SchemaDifference(
                        kind,
                        f"{table}.{column}",
                        f"database nullable={existing} vs model nullable={item[6]}",
                    )
                )
            elif kind == "modify_default":
                table, column = item[2], item[3]
                differences.append(
                    SchemaDifference(
                        kind,
                        f"{table}.{column}",
                        f"database default={item[4]!r} vs model default={item[6]!r}",
                    )
                )
            else:
                # Constraint-level changes carry a SQLAlchemy object rather than a
                # table/column pair. render_item-free rendering keeps the name and
                # columns visible without importing Alembic's internal renderers.
                target = getattr(item[-1], "name", None) or str(item[-1])
                detail = (
                    f"columns={getattr(item[-1], 'columns', None)}"
                    if hasattr(item[-1], "columns")
                    else ""
                )
                differences.append(SchemaDifference(kind, str(target), detail))
    return differences


def server_default_differences(database: Path) -> list[SchemaDifference]:
    """Compare server defaults on their own terms.

    Alembic's ``compare_server_default`` cannot help here on SQLite: it renders the
    model side from Python defaults and the database side from stored DDL, so it
    reports ``templates.visibility`` as drifting merely because the model uses a
    Python ``default=`` and the migration used ``server_default=``. Both behaviours
    are correct, so the comparison is explicit instead:

    * a default the model declares as ``server_default`` must exist and match, and
    * a default that exists only in the database is listed so it stays deliberate.
    """
    connection = sqlite3.connect(str(database))
    try:
        actual: dict[str, str | None] = {}
        for table in Base.metadata.sorted_tables:
            for row in connection.execute(f"PRAGMA table_info({table.name})"):
                actual[f"{table.name}.{row[1]}"] = row[4]
    finally:
        connection.close()

    differences: list[SchemaDifference] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            key = f"{table.name}.{column.name}"
            declared = column.server_default
            if declared is None:
                continue
            if declared.arg is None:  # pragma: no cover - defensive
                continue
            expected = _normalise_default(getattr(declared.arg, "text", declared.arg))
            observed = _normalise_default(actual.get(key))
            if observed != expected:
                differences.append(
                    SchemaDifference(
                        "server_default",
                        key,
                        f"expected {expected!r}, database has {observed!r}",
                    )
                )
    return differences


def undeclared_server_defaults(database: Path) -> set[str]:
    """Columns that carry a default in the database but not in the models.

    These are not defects -- they exist so an ALTER TABLE can add a NOT NULL
    column to a populated table -- but they are schema facts a migration test
    should surface rather than discover by surprise during a future migration.
    """
    connection = sqlite3.connect(str(database))
    try:
        actual = {
            f"{table.name}.{row[1]}"
            for table in Base.metadata.sorted_tables
            for row in connection.execute(f"PRAGMA table_info({table.name})")
            if row[4] is not None
        }
    finally:
        connection.close()

    declared = {
        f"{table.name}.{column.name}"
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.server_default is not None
    }
    return actual - declared




def sqlite_object_differences(database: Path) -> list[SchemaDifference]:
    """Differences Alembic cannot see: partial indexes, triggers, check clauses.

    ``compare_metadata`` compares names and columns, so it treats
    ``uq_resource_locks_active`` as identical whether or not it carries its
    ``WHERE released_at IS NULL`` predicate. That predicate is the entire point of
    the index -- dropping it would make every released lock block a new one. The
    audit triggers are SQLite ``CREATE TRIGGER`` statements that exist in no
    metadata at all. Both are checked here against the real database file.
    """
    differences: list[SchemaDifference] = []
    connection = sqlite3.connect(str(database))
    try:
        model_indexes: dict[str, Any] = {}
        for table in Base.metadata.sorted_tables:
            for index in table.indexes:
                model_indexes[str(index.name)] = index

        actual_indexes = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index'"
            )
            if not row[0].startswith(_IMPLICIT_INDEX_PREFIX)
        }

        for name, index in model_indexes.items():
            if name not in actual_indexes:
                differences.append(
                    SchemaDifference(
                        "missing_index", name, "declared in models, absent from database"
                    )
                )
                continue
            declared_where = index.dialect_options["sqlite"].get("where")
            actual_sql = actual_indexes[name] or ""
            if declared_where is not None:
                predicate = str(declared_where)
                if "where" not in actual_sql.lower() or predicate.lower() not in actual_sql.lower():
                    differences.append(
                        SchemaDifference(
                            "partial_index_predicate",
                            name,
                            f"expected WHERE {predicate}; database has {actual_sql!r}",
                        )
                    )
            elif " where " in actual_sql.lower():
                differences.append(
                    SchemaDifference(
                        "unexpected_index_predicate",
                        name,
                        "database index is partial but model declares no predicate: "
                        f"{actual_sql!r}",
                    )
                )

        for name in sorted(set(actual_indexes) - set(model_indexes)):
            differences.append(
                SchemaDifference(
                    "unexpected_index", name, "present in database, not declared in models"
                )
            )

        actual_triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        for required in REQUIRED_TRIGGERS:
            if required not in actual_triggers:
                differences.append(
                    SchemaDifference("missing_trigger", required, "required by migration, absent")
                )
    finally:
        connection.close()
    return differences


# Triggers the migrations are responsible for creating. They are not expressible
# in ORM metadata, so they are asserted by name from the real database.
REQUIRED_TRIGGERS: tuple[str, ...] = (
    "audit_events_no_update",
    "audit_events_no_delete",
)


def schema_fingerprint(database: Path) -> dict[str, Any]:
    """A structural fingerprint of the schema, independent of DDL text ordering.

    Comparing raw ``CREATE TABLE`` text is too strict: two databases built from
    the same revisions can list their ``FOREIGN KEY`` clauses in a different order
    (batch table rebuilds move the re-created constraint to the end), which is
    semantically identical. Comparing reflection output instead reports real
    differences -- columns, types, nullability, defaults, keys, indexes, triggers.
    """
    from sqlalchemy import inspect as sa_inspect

    engine = database_engine(database)
    try:
        inspector = sa_inspect(engine)
        tables: dict[str, Any] = {}
        for table in sorted(inspector.get_table_names()):
            if table.startswith("sqlite_"):
                continue
            tables[table] = {
                "columns": sorted(
                    (
                        column["name"],
                        str(column["type"]),
                        bool(column["nullable"]),
                        _normalise_default(column.get("default")),
                        bool(column.get("primary_key", 0)),
                    )
                    for column in inspector.get_columns(table)
                ),
                "primary_key": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
                "foreign_keys": sorted(
                    (
                        tuple(fk["constrained_columns"]),
                        fk["referred_table"],
                        tuple(fk["referred_columns"]),
                        (fk.get("options") or {}).get("ondelete"),
                    )
                    for fk in inspector.get_foreign_keys(table)
                ),
                "unique": sorted(
                    tuple(item["column_names"]) for item in inspector.get_unique_constraints(table)
                ),
                "indexes": sorted(
                    (
                        item["name"],
                        tuple(item["column_names"]),
                        bool(item["unique"]),
                    )
                    for item in inspector.get_indexes(table)
                    if not str(item["name"]).startswith(_IMPLICIT_INDEX_PREFIX)
                ),
            }
    finally:
        engine.dispose()

    connection = sqlite3.connect(str(database))
    try:
        triggers = sorted(
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")
        )
    finally:
        connection.close()

    return {"tables": tables, "triggers": triggers}


def build_migrated_template(destination: Path) -> Path:
    """Run every migration into ``destination`` and return it.

    Called once per test session; tests copy the result instead of migrating.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    upgrade_to(sqlite_url(destination), "head")
    # A copy of the file must not inherit a live WAL. Nothing wrote to this
    # database, so the checkpoint is a no-op, but doing it explicitly keeps a
    # future change (e.g. seeding data here) from silently producing copies with
    # unmerged pages.
    connection = sqlite3.connect(str(destination))
    try:
        connection.execute("PRAGMA wal_checkpoint(FULL)")
        # The template must be committed to disk before it is copied.
        connection.commit()
    finally:
        connection.close()
    return destination


def copy_database(template: Path, destination: Path) -> Path:
    """Copy the migrated template to ``destination`` for one test to own."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(template, destination)
    return destination


_SESSION_TEMPLATE: Path | None = None


def session_template() -> Path:
    """A process-wide migrated database, built on first use.

    Some tests build their own client from a plain helper function rather than a
    fixture (``test_proxy_identity.proxy_client``), so they cannot ask pytest for
    the session template. This gives them the same migration-built schema without
    each helper rebuilding it, and without falling back to ``create_all``.
    """
    global _SESSION_TEMPLATE
    if _SESSION_TEMPLATE is None or not _SESSION_TEMPLATE.exists():
        import tempfile

        directory = Path(tempfile.mkdtemp(prefix="sshsuite-migrated-"))
        _SESSION_TEMPLATE = build_migrated_template(directory / "template.db")
    return _SESSION_TEMPLATE


def ensure_migrated_database(path: Path) -> Path:
    """Make ``path`` a freshly migrated database.

    Idempotent in the sense that matters: the destination is replaced by a clean
    copy of the session template, so calling it twice still yields an empty,
    fully-migrated database rather than an error.
    """
    return copy_database(session_template(), path)


def database_engine(path: Path) -> Engine:
    return create_engine(sqlite_url(path), future=True)


def table_columns(
    connection: sqlite3.Connection, table: str
) -> dict[str, tuple[str, int, Any, Any]]:
    """``PRAGMA table_info`` as ``{name: (type, notnull, default, pk)}``."""
    return {
        row[1]: (row[2], row[3], row[4], row[5])
        for row in connection.execute(f"PRAGMA table_info({table})")
    }


def foreign_keys(connection: sqlite3.Connection, table: str) -> set[tuple[str, str, str, str]]:
    """``PRAGMA foreign_key_list`` as ``{(column, parent_table, parent_column, on_delete)}``."""
    return {
        (row[3], row[2], row[4], row[6])
        for row in connection.execute(f"PRAGMA foreign_key_list({table})")
    }
