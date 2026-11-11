"""Global host-workflow concurrency control.

The product limit is 20 host workflows running at once across *all* executions,
not per workflow. A single process-wide ``asyncio.Semaphore`` is the right
primitive here: one FastAPI process owns scheduling and SSH work, so no
cross-process coordination (and therefore no Redis) is needed.

The executor acquires one slot per host workflow, so the limit spans all
executions rather than being per workflow:

    async with limiter.slot():
        await run_host_workflow(...)
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class ConcurrencySnapshot:
    limit: int
    in_use: int
    available: int


class HostWorkflowLimiter:
    def __init__(self, limit: int | None = None) -> None:
        settings: Settings = get_settings()
        self._limit = limit if limit is not None else settings.max_concurrent_host_workflows
        if self._limit < 1:
            raise ValueError("Concurrency limit must be at least 1")
        self._semaphore: asyncio.Semaphore | None = None
        self._in_use = 0

    @property
    def limit(self) -> int:
        return self._limit

    def _sem(self) -> asyncio.Semaphore:
        # Created lazily so the limiter can be constructed outside a running loop.
        if self._semaphore is None:
            self._semaphore = asyncio.Semaphore(self._limit)
        return self._semaphore

    @asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        async with self._sem():
            self._in_use += 1
            try:
                yield
            finally:
                self._in_use -= 1

    def snapshot(self) -> ConcurrencySnapshot:
        return ConcurrencySnapshot(
            limit=self._limit,
            in_use=self._in_use,
            available=max(self._limit - self._in_use, 0),
        )


_limiter: HostWorkflowLimiter | None = None


def get_limiter() -> HostWorkflowLimiter:
    global _limiter
    if _limiter is None:
        _limiter = HostWorkflowLimiter()
    return _limiter


def reset_limiter() -> None:
    global _limiter
    _limiter = None
