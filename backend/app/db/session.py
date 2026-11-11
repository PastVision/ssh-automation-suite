from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


def _apply_sqlite_pragmas(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection, _record) -> None:  # noqa: ANN001
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def build_engine(database_url: str) -> Engine:
    is_sqlite = database_url.startswith("sqlite")
    engine = create_engine(
        database_url,
        future=True,
        echo=False,
        connect_args={"check_same_thread": False} if is_sqlite else {},
    )
    if is_sqlite:
        _apply_sqlite_pragmas(engine)
    return engine


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    return build_engine(get_settings().resolved_database_url)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), autoflush=False, expire_on_commit=False)


def reset_engine() -> None:
    """Drop cached engine/session factory (used by tests and tooling)."""
    get_session_factory.cache_clear()
    get_engine.cache_clear()


def get_db() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def check_database() -> tuple[bool, str | None]:
    try:
        with get_engine().connect() as connection:
            connection.execute(text("SELECT 1"))
        return True, None
    except Exception as exc:  # pragma: no cover - depends on external state
        return False, str(exc)
