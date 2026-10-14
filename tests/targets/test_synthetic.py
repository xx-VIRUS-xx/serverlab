import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from targets.synthetic.app import SyntheticTarget, target_from_env


@asynccontextmanager
async def _client_for(
    target: SyntheticTarget,
) -> AsyncIterator[httpx.AsyncClient]:
    server = await asyncio.start_server(target.handle_client, "127.0.0.1", 0)
    if server.sockets is None:
        raise AssertionError("server did not expose its listening socket")
    port = server.sockets[0].getsockname()[1]
    async with server:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
            yield client


async def _timed_work(client: httpx.AsyncClient) -> float:
    started = time.perf_counter()
    response = await client.get("/work")
    assert response.status_code == 200
    return time.perf_counter() - started


async def _wait_for_saturation(client: httpx.AsyncClient) -> None:
    deadline = time.perf_counter() + 1.0
    latest_stats: object = {}
    while time.perf_counter() < deadline:
        response = await client.get("/stats")
        assert response.status_code == 200
        latest_stats = response.json()
        if latest_stats == {"in_flight": 2, "queued": 2, "completed": 2}:
            return
        await asyncio.sleep(0.005)
    raise AssertionError(f"target did not saturate; last stats: {latest_stats}")


def test_service_time_and_queue_growth() -> None:
    """Check 100 ms service within 70-400 ms and queued work above the limit."""

    async def exercise() -> None:
        target = SyntheticTarget(concurrency_limit=2, service_time_ms=100)
        async with _client_for(target) as client:
            health = await client.get("/healthz")
            assert health.status_code == 200
            assert health.json() == {"status": "ok"}

            below_limit = await asyncio.gather(
                _timed_work(client), _timed_work(client)
            )
            assert all(0.07 <= elapsed <= 0.4 for elapsed in below_limit)

            above_limit = [
                asyncio.create_task(_timed_work(client)) for _ in range(4)
            ]
            await _wait_for_saturation(client)
            queued_durations = await asyncio.gather(*above_limit)
            assert max(queued_durations) >= 0.15

            stats = await client.get("/stats")
            assert stats.json() == {"in_flight": 0, "queued": 0, "completed": 6}

    asyncio.run(exercise())


def test_configuration_comes_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONCURRENCY_LIMIT", "3")
    monkeypatch.setenv("SERVICE_TIME_MS", "25")

    target = target_from_env()

    assert target.concurrency_limit == 3
    assert target.service_time_ms == 25


@pytest.mark.parametrize(
    ("concurrency_limit", "service_time_ms"),
    [(0, 100), (1, 0)],
)
def test_configuration_must_be_positive(
    concurrency_limit: int, service_time_ms: int
) -> None:
    with pytest.raises(ValueError):
        SyntheticTarget(concurrency_limit, service_time_ms)
