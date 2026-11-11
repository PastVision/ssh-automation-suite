"""The global cap of 20 concurrent host workflows must be enforced."""

from __future__ import annotations

import asyncio

import pytest

from app.services.concurrency import HostWorkflowLimiter


def test_default_limit_is_twenty(settings) -> None:
    assert HostWorkflowLimiter().limit == 20


def test_snapshot_tracks_usage(settings) -> None:
    limiter = HostWorkflowLimiter(limit=3)

    async def scenario() -> None:
        assert limiter.snapshot().available == 3
        async with limiter.slot():
            assert limiter.snapshot().in_use == 1
            assert limiter.snapshot().available == 2
        assert limiter.snapshot().in_use == 0

    asyncio.run(scenario())


def test_limit_is_never_exceeded(settings) -> None:
    limiter = HostWorkflowLimiter(limit=2)
    observed_peak = 0

    async def worker() -> None:
        nonlocal observed_peak
        async with limiter.slot():
            observed_peak = max(observed_peak, limiter.snapshot().in_use)
            await asyncio.sleep(0.01)

    async def scenario() -> None:
        await asyncio.gather(*(worker() for _ in range(10)))

    asyncio.run(scenario())

    assert observed_peak == 2
    assert limiter.snapshot().in_use == 0


def test_slot_is_released_on_error(settings) -> None:
    limiter = HostWorkflowLimiter(limit=1)

    async def scenario() -> None:
        with pytest.raises(RuntimeError):
            async with limiter.slot():
                raise RuntimeError("boom")
        assert limiter.snapshot().in_use == 0

    asyncio.run(scenario())


def test_usage_count_never_exceeds_limit(settings) -> None:
    """``in_use`` is incremented inside the semaphore and decremented before it is
    released, so a snapshot can never report more than the limit in use."""
    limiter = HostWorkflowLimiter(limit=2)
    observed_peak = 0
    stop = False

    async def worker() -> None:
        async with limiter.slot():
            await asyncio.sleep(0)

    async def sampler() -> None:
        nonlocal observed_peak
        while not stop:
            observed_peak = max(observed_peak, limiter.snapshot().in_use)
            await asyncio.sleep(0)

    async def scenario() -> None:
        nonlocal stop
        watcher = asyncio.ensure_future(sampler())
        try:
            for _ in range(20):
                await asyncio.gather(*(worker() for _ in range(8)))
        finally:
            stop = True
            await watcher

    asyncio.run(scenario())

    assert observed_peak <= limiter.limit
    assert limiter.snapshot().in_use == 0


def test_invalid_limit_rejected(settings) -> None:
    with pytest.raises(ValueError):
        HostWorkflowLimiter(limit=0)
