"""FastAPI target: unit tests with a fake pool, plus a docker integration test."""

import asyncio
import math
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

import httpx
import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg_pool import AsyncConnectionPool, PoolTimeout

from targets.api.app import Settings, create_app

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCKERFILE = REPO_ROOT / "targets" / "api" / "Dockerfile"
PG_IMAGE = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"

STATS = {
    "pool_min": 2,
    "pool_max": 2,
    "pool_size": 2,
    "pool_available": 1,
    "requests_waiting": 0,
    "requests_num": 5,
    "requests_queued": 1,
    "requests_wait_ms": 3,
    "requests_wait_max": 2,
    "extra_pool_key": 7,
}


class FakeCursor:
    async def fetchone(self) -> tuple[int]:
        return (1,)


class FakeConn:
    def __init__(self, pool: "FakePool") -> None:
        self._pool = pool

    async def execute(self, query: str, params: Any = None) -> FakeCursor:
        self._pool.executed.append((query, params))
        return FakeCursor()


class FakePool:
    """Stands in for AsyncConnectionPool; only the methods the app uses."""

    def __init__(self, fail_with: BaseException | None = None) -> None:
        self.fail_with = fail_with
        self.opened = False
        self.closed = False
        self.executed: list[tuple[str, Any]] = []

    async def open(self, wait: bool = False, timeout: float = 30.0) -> None:
        self.opened = True

    async def close(self, timeout: float = 5.0) -> None:
        self.closed = True

    @asynccontextmanager
    async def connection(self, timeout: float | None = None) -> AsyncIterator[FakeConn]:
        if self.fail_with is not None:
            raise self.fail_with
        yield FakeConn(self)

    def get_stats(self) -> dict[str, int]:
        return dict(STATS)


def _settings() -> Settings:
    return Settings(database_url="postgresql://unused", pool_size=2, service_time_ms=100)


def _app(fake: FakePool) -> TestClient:
    return TestClient(create_app(_settings(), cast(AsyncConnectionPool, fake)))


def test_settings_from_env_requires_database_url_and_pool_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("POOL_SIZE", "2")
    with pytest.raises(ValueError, match="DATABASE_URL"):
        Settings.from_env()
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.delenv("POOL_SIZE", raising=False)
    with pytest.raises(ValueError, match="POOL_SIZE"):
        Settings.from_env()


def test_settings_from_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://x")
    monkeypatch.setenv("POOL_SIZE", "3")
    monkeypatch.delenv("SERVICE_TIME_MS", raising=False)
    monkeypatch.delenv("POOL_TIMEOUT_S", raising=False)
    cfg = Settings.from_env()
    assert (cfg.pool_size, cfg.service_time_ms, cfg.pool_timeout_s) == (3, 50, 30.0)


def test_settings_reject_invalid_values() -> None:
    with pytest.raises(ValueError):
        Settings(database_url="postgresql://x", pool_size=0)
    with pytest.raises(ValueError):
        Settings(database_url="postgresql://x", pool_size=1, service_time_ms=-1)


def test_metrics_shape_is_numeric_and_includes_pool_and_app_counters() -> None:
    with _app(FakePool()) as client:
        body = client.get("/metrics").json()
    assert body == {**STATS, "in_flight": 0, "ok": 0, "errors": 0}
    assert all(isinstance(v, int) for v in body.values())


def test_work_runs_pg_sleep_with_service_time_and_counts_ok() -> None:
    fake = FakePool()
    with _app(fake) as client:
        resp = client.get("/work")
        metrics = client.get("/metrics").json()
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    # startup check first, then exactly one pg_sleep for the one /work request
    assert fake.executed == [("SELECT 1", None), ("SELECT pg_sleep(%s)", (0.1,))]
    assert metrics["ok"] == 1 and metrics["errors"] == 0 and metrics["in_flight"] == 0


def test_work_pool_timeout_returns_503_and_counts_error() -> None:
    with _app(FakePool(fail_with=PoolTimeout("couldn't get a connection"))) as client:
        resp = client.get("/work")
        metrics = client.get("/metrics").json()
    assert resp.status_code == 503
    assert metrics["errors"] == 1 and metrics["ok"] == 0 and metrics["in_flight"] == 0


def test_work_db_error_returns_503_and_process_keeps_serving() -> None:
    fake = FakePool(fail_with=psycopg.OperationalError("connection refused"))
    with _app(fake) as client:
        assert client.get("/work").status_code == 503
        fake.fail_with = None
        assert client.get("/work").status_code == 200
        metrics = client.get("/metrics").json()
    assert metrics["errors"] == 1 and metrics["ok"] == 1


def test_healthz_is_503_before_pool_is_opened() -> None:
    fake = FakePool()
    client = TestClient(create_app(_settings(), cast(AsyncConnectionPool, fake)))
    assert client.get("/healthz").status_code == 503
    assert not fake.opened


def test_healthz_is_200_after_open_and_does_not_touch_the_pool() -> None:
    fake = FakePool()
    with _app(fake) as client:
        assert fake.opened
        assert fake.executed == [("SELECT 1", None)]  # the single startup check
        for _ in range(5):
            assert client.get("/healthz").status_code == 200
        assert fake.executed == [("SELECT 1", None)]  # healthz never borrows a connection
    assert fake.closed


def test_failed_startup_check_keeps_the_service_not_ready() -> None:
    fake = FakePool(fail_with=psycopg.OperationalError("down"))
    with _app(fake) as client:
        assert client.get("/healthz").status_code == 503


def test_metrics_fills_missing_pool_counters_with_zero() -> None:
    class Sparse(FakePool):
        def get_stats(self) -> dict[str, int]:
            return {"pool_size": 2, "pool_available": 2, "requests_waiting": 0,
                    "pool_min": 2, "pool_max": 2}

    with _app(Sparse()) as client:
        body = client.get("/metrics").json()
    for key in ("requests_num", "requests_queued", "requests_wait_ms", "requests_wait_max"):
        assert body[key] == 0
    assert {"in_flight", "ok", "errors"} <= set(body)


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    return probe.returncode == 0


def _run(cmd: list[str], timeout: float = 600) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"{' '.join(cmd)} failed:\n{proc.stderr}")
    return proc


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_postgres(port: int, budget_s: float = 120) -> None:
    deadline = time.monotonic() + budget_s
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(
                f"host=127.0.0.1 port={port} user=postgres password=test dbname=postgres",
                connect_timeout=2,
            ) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.OperationalError as exc:
            last = exc
            time.sleep(0.5)
    raise AssertionError(f"postgres not ready on port {port}: {last}")


def _wait_for_healthz(base: str, budget_s: float = 120) -> None:
    deadline = time.monotonic() + budget_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/healthz", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"{base}/healthz did not return 200")


async def _burst(base: str, n: int) -> tuple[float, list[int]]:
    async with httpx.AsyncClient(base_url=base, timeout=120) as client:
        start = time.perf_counter()
        responses = await asyncio.gather(*(client.get("/work") for _ in range(n)))
        elapsed = time.perf_counter() - start
    return elapsed, [r.status_code for r in responses]


@pytest.mark.integration
def test_api_container_against_real_postgres() -> None:
    if not _docker_available():
        pytest.skip("docker is not available (docker info failed or docker not on PATH)")

    suffix = uuid.uuid4().hex[:10]
    image = f"sl-c6-api-img-{suffix}"
    pg_name = f"sl-c6-pg-{suffix}"
    api_name = f"sl-c6-api-{suffix}"
    net = f"sl-c6-net-{suffix}"
    pool_size, service_ms, n = 2, 100, 8
    pg_port, api_port = _free_port(), _free_port()
    try:
        _run(["docker", "network", "create", net])
        _run(
            [
                "docker", "run", "-d", "--name", pg_name, "--network", net,
                "-e", "POSTGRES_PASSWORD=test",
                "-p", f"127.0.0.1:{pg_port}:5432",
                PG_IMAGE, "-c", "max_connections=300",
            ]
        )  # fmt: skip
        _wait_for_postgres(pg_port)
        _run(
            ["docker", "build", "-f", str(DOCKERFILE), "-t", image, str(REPO_ROOT)],
            timeout=900,
        )
        _run(
            [
                "docker", "run", "-d", "--name", api_name, "--network", net,
                "-e", f"DATABASE_URL=postgresql://postgres:test@{pg_name}:5432/postgres",
                "-e", f"POOL_SIZE={pool_size}",
                "-e", f"SERVICE_TIME_MS={service_ms}",
                "-p", f"127.0.0.1:{api_port}:8000",
                image,
            ]
        )  # fmt: skip
        base = f"http://127.0.0.1:{api_port}"
        _wait_for_healthz(base)

        elapsed, statuses = asyncio.run(_burst(base, n))
        assert statuses == [200] * n
        # Each connection is held for service_ms, so pool_size lanes need at least
        # ceil(n / pool_size) * service_ms of wall time.
        assert elapsed >= math.ceil(n / pool_size) * service_ms / 1000

        metrics = httpx.get(f"{base}/metrics", timeout=10).json()
        assert metrics["requests_queued"] > 0
        assert metrics["requests_wait_ms"] > 0
        assert metrics["ok"] == n and metrics["errors"] == 0
        assert metrics["pool_max"] == pool_size and metrics["in_flight"] == 0
    finally:
        for name in (api_name, pg_name):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False)
        subprocess.run(["docker", "image", "rm", "-f", image], capture_output=True, check=False)
        subprocess.run(["docker", "network", "rm", net], capture_output=True, check=False)
