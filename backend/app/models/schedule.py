"""Workflow schedules, their firing ledger, and the scheduler lease.

A schedule is *live, owned infrastructure*, so it is never physically deleted by
the application: ``deleted_at`` retires it while ``schedule_runs`` keeps the
record of what was decided and why. That is what makes "why did my workflow not
run?" answerable after a schedule is gone from the UI.

Two unique indexes carry correctness rather than style:

* ``uq_schedules_owner_id_name_active`` is a **partial** index, so a name is free
  again once the earlier schedule is soft-deleted.
* ``uq_schedule_runs_schedule_id_scheduled_for`` is the idempotency barrier. The
  ledger row is inserted before any execution exists, so a retried or duplicated
  firing is absorbed by the constraint instead of running twice.

``SchedulerLease`` is a single-row arbitration record. ``generation`` increments
on every successful acquisition, which is what lets a superseded holder detect
that it lost ownership; see ``app.services.lease`` for the fencing protocol and
its documented limits.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.db.types import enum_column, timestamp_column


class ScheduleKind(StrEnum):
    ONCE = "once"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    CRON = "cron"


class ScheduleState(StrEnum):
    """Lifecycle state. The terminal states apply to one-time schedules only.

    ``ACTIVE`` and ``PAUSED``/``DISABLED`` describe recurring schedules, which
    have no end. A one-time schedule advances to exactly one terminal state, so a
    rebuild after restart can neither re-arm it nor lose track of it.
    """

    SCHEDULED = "scheduled"
    ACTIVE = "active"
    PAUSED = "paused"
    DISABLED = "disabled"
    COMPLETED = "completed"
    FAILED = "failed"
    MISSED = "missed"


TERMINAL_STATES = frozenset(
    {ScheduleState.COMPLETED, ScheduleState.FAILED, ScheduleState.MISSED}
)


class PausedReason(StrEnum):
    """Why a schedule stopped firing without being deliberately disabled."""

    OWNER_INACTIVE = "owner_inactive"
    INVALID_WORKFLOW = "invalid_workflow"
    UNAUTHORIZED = "unauthorized"


class MisfirePolicy(StrEnum):
    """What to do with logical firings that were missed while the app was down."""

    SKIP = "skip"
    RUN_ONCE = "run_once"


class ScheduleRunOutcome(StrEnum):
    """Decision recorded for one logical firing time.

    ``DISPATCHING`` is transient and must never be committed: the dispatch
    transaction always moves it to a final outcome before committing. A committed
    ``DISPATCHING`` row would mean the savepoint handling regressed.
    """

    DISPATCHING = "dispatching"
    DISPATCHED = "dispatched"
    DUPLICATE = "duplicate"
    SKIPPED_OVERLAP = "skipped_overlap"
    SKIPPED_MISFIRE = "skipped_misfire"
    SKIPPED_OWNER_INACTIVE = "skipped_owner_inactive"
    SKIPPED_DISABLED = "skipped_disabled"
    SKIPPED_FENCED = "skipped_fenced"
    INVALID_WORKFLOW = "invalid_workflow"
    UNAUTHORIZED = "unauthorized"
    ERROR = "error"


class Schedule(Base):
    __tablename__ = "schedules"
    __table_args__ = (
        # Partial: a soft-deleted name is free for reuse.
        Index(
            "uq_schedules_owner_id_name_active",
            "owner_id",
            "name",
            unique=True,
            sqlite_where=text("deleted_at IS NULL"),
        ),
        Index("ix_schedules_enabled_next_run_at", "enabled", "next_run_at"),
        Index("ix_schedules_workflow_id", "workflow_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # RESTRICT, not CASCADE: a schedule is live infrastructure, so physically
    # deleting a user who owns one is refused rather than silently destroying
    # scheduled work. Deactivation is the supported path.
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # SET NULL: ``workflows.delete`` is a hard delete, so this pointer is cleared
    # while the snapshot below keeps the schedule's history readable. A cascade
    # would erase the firing ledger; NO ACTION would make a scheduled workflow
    # impossible to delete.
    workflow_id: Mapped[int | None] = mapped_column(
        ForeignKey("workflows.id", ondelete="SET NULL"), nullable=True
    )
    # Snapshot so history still reads correctly after a workflow is deleted.
    workflow_name: Mapped[str] = mapped_column(String(120), nullable=False)

    name: Mapped[str] = mapped_column(String(120), nullable=False)

    schedule_kind: Mapped[ScheduleKind] = mapped_column(
        enum_column(ScheduleKind, "schedule_kind"), nullable=False
    )
    # Normalised for every recurring kind; NULL for ``once``.
    cron_expression: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # UTC instant. Set only for ``once``.
    run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")

    state: Mapped[ScheduleState] = mapped_column(
        enum_column(ScheduleState, "schedule_state"),
        nullable=False,
        default=ScheduleState.SCHEDULED,
        index=True,
    )
    # Derived flag kept in step with ``state`` so the rebuild query and the list
    # view can filter without decoding the enum.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    paused_reason: Mapped[PausedReason | None] = mapped_column(
        enum_column(PausedReason, "paused_reason"), nullable=True
    )

    misfire_policy: Mapped[MisfirePolicy] = mapped_column(
        enum_column(MisfirePolicy, "misfire_policy"),
        nullable=False,
        default=MisfirePolicy.SKIP,
    )
    misfire_grace_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300
    )

    # Lower bound for the misfire scan. Persisted rather than derived from the last
    # ledger row so a restart recomputes exactly the same missed set.
    last_fired_for: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)

    # Deliberately no delete-orphan: the ledger is append-only history, and a
    # cascade here would let one soft delete of a schedule erase the record that
    # explains what it did.
    runs: Mapped[list[ScheduleRun]] = relationship(
        back_populates="schedule",
        passive_deletes=True,
        order_by="ScheduleRun.scheduled_for",
    )

    @property
    def is_terminal(self) -> bool:
        return ScheduleState(self.state) in TERMINAL_STATES

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<Schedule id={self.id} name={self.name!r} "
            f"kind={self.schedule_kind} state={self.state}>"
        )


class ScheduleRun(Base):
    """Append-only ledger: one row per logical firing time.

    Every firing is recorded -- including the ones that deliberately did nothing --
    so the ledger is a complete answer to "what happened at 02:30?". A row is only
    ever updated from the transient ``DISPATCHING`` state to its final outcome.
    """

    __tablename__ = "schedule_runs"
    __table_args__ = (
        UniqueConstraint(
            "schedule_id",
            "scheduled_for",
            name="uq_schedule_runs_schedule_id_scheduled_for",
        ),
        Index("ix_schedule_runs_schedule_id_created_at", "schedule_id", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    schedule_id: Mapped[int] = mapped_column(
        ForeignKey("schedules.id", ondelete="NO ACTION"),
        nullable=False,
        index=True,
    )
    # SET NULL: the ledger row outlives a purged execution.
    execution_id: Mapped[int | None] = mapped_column(
        ForeignKey("executions.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # UTC instant of the logical firing, never a local wall time.
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    outcome: Mapped[ScheduleRunOutcome] = mapped_column(
        enum_column(ScheduleRunOutcome, "schedule_run_outcome"), nullable=False
    )
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()

    schedule: Mapped[Schedule] = relationship(back_populates="runs")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ScheduleRun schedule={self.schedule_id} "
            f"at={self.scheduled_for} outcome={self.outcome}>"
        )


class SchedulerLease(Base):
    """Single-row arbitration record for "which process may schedule?".

    ``generation`` increments on each successful acquisition so a superseded
    holder can be detected by a guarded write. This is single-writer arbitration
    on one SQLite file, not consensus; see ``app.services.lease``.
    """

    __tablename__ = "scheduler_lease"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    holder_id: Mapped[str] = mapped_column(String(64), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Plain DateTime: these are only ever compared with each other and with a
    # freshly computed UTC instant, and SQLite stores them as UTC text either way.
    acquired_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    heartbeat_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Diagnostics only: never used to decide ownership (PIDs are reused and
    # hostnames are not unique).
    hostname: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pid: Mapped[int | None] = mapped_column(Integer, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<SchedulerLease holder={self.holder_id!r} "
            f"generation={self.generation} expires={self.expires_at}>"
        )
