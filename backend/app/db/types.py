"""Shared column helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime) -> datetime:
    """Normalise a stored timestamp to an aware UTC datetime.

    SQLite has no timezone type: ``DateTime(timezone=True)`` is stored as text and
    comes back **naive** on read. Comparing such a value to ``utcnow()`` raises
    ``TypeError`` ("can't compare offset-naive and offset-aware datetimes"), so
    every Python-level comparison of a persisted timestamp goes through here.

    Values are written as UTC, so attaching UTC is a reinterpretation, not a
    conversion. Do not use this on a timestamp written in a non-UTC zone.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def timestamp_column(*, onupdate: bool = False) -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow if onupdate else None,
        nullable=False,
    )


def enum_column(enum_cls: type, name: str) -> SAEnum:
    """Persist an enum as its short string value, validated in Python."""
    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=32,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )

