"""Tests for the pg_stat_activity collector.

Unit tests point the collector at a closed localhost port and never need a database.
The integration test starts a throwaway Postgres container named sl-a6-* and always removes it;
it is skipped with an explicit reason when the docker CLI or daemon is unavailable.
"""

import json
import shutil
import socket
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest

from serverlab.metrics.pg_activity import PgActivityCollector, PgActivityStats

POSTGRES_IMAGE = "postgres@sha256:721873c34ceb9f8d8fc265984940dc982404c105f19ad51be9fdc5970a6080ea"
THREAD_NAME = "pg-activity-collector"
LINE_KEYS = {"run_id", "t_host_utc", "rows"}
ROW_KEYS = {"state", "wait_event_type", "wait_event", "count"}


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _unreachable_dsn() -> str:
    return f"postgresql://postgres:test@127.0.0.1:{_free_port()}/postgres"


def _sampler_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name == THREAD_NAME and t.is_alive()]


def _wait_for(predicate: Callable[[], bool], timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


def _lines(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _docker_skip_reason() -> str | None:
    if shutil.which("docker") is None:
        return "docker CLI not found on PATH"
    try:
        probe = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"docker not usable: {exc}"
    if probe.returncode != 0:
        return f"docker daemon not reachable: {probe.stderr.strip()[:200]}"
    return None


_SKIP_REASON = _docker_skip_reason()


def _wait_for_postgres(dsn: str, timeout_s: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_s
    last: psycopg.Error | None = None
    while time.monotonic() < deadline:
        try:
            with psycopg.connect(dsn, connect_timeout=2) as conn:
                conn.execute("SELECT 1")
            return
        except psycopg.Error as exc:
            last = exc
            time.sleep(0.5)
    raise AssertionError(f"postgres not ready after {timeout_s}s: {last}")


@pytest.fixture
def pg_dsn() -> Iterator[str]:
    name = f"sl-a6-{uuid.uuid4().hex[:12]}"
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                name,
                "-e",
                "POSTGRES_PASSWORD=test",
                "-p",
                "127.0.0.1::5432",
                POSTGRES_IMAGE,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=600,
        )
        port_out = subprocess.run(
            ["docker", "port", name, "5432/tcp"],
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        ).stdout
        port = int(port_out.splitlines()[0].rsplit(":", 1)[1])
        dsn = f"postgresql://postgres:test@127.0.0.1:{port}/postgres"
        _wait_for_postgres(dsn)
        yield dsn
    finally:
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=120)


def _sleep_2s(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("SELECT pg_sleep(2)")


# --- construction ------------------------------------------------------------------------


@pytest.mark.parametrize("interval", [0.0, 0.5, 0.999, -1.0, float("nan")])
def test_interval_below_one_second_raises(interval: float) -> None:
    with pytest.raises(ValueError):
        PgActivityCollector(_unreachable_dsn(), interval_s=interval)


def test_default_interval_is_one_second() -> None:
    assert PgActivityCollector(_unreachable_dsn()).stats().interval_s == 1.0


# --- runtime (unreachable DSN: never raises) ---------------------------------------------


def test_unreachable_dsn_records_errors_and_retries(tmp_path: Path) -> None:
    col = PgActivityCollector(_unreachable_dsn(), interval_s=1.0)
    col.start("run-u", tmp_path / "pg.jsonl")  # must return normally
    _wait_for(lambda: col.stats().missed_samples >= 2)  # retried on the next interval
    stats = col.stop()

    assert stats.samples == 0
    assert stats.missed_samples >= 2
    assert stats.errors
    assert all(e.startswith("sample failed:") for e in stats.errors)
    assert _lines(tmp_path / "pg.jsonl") == []
    assert _sampler_threads() == []


def test_stop_twice_is_safe(tmp_path: Path) -> None:
    col = PgActivityCollector(_unreachable_dsn(), interval_s=1.0)
    col.start("run-t", tmp_path / "pg.jsonl")
    first = col.stop()
    second = col.stop()
    assert first == second
    assert _sampler_threads() == []


def test_thread_is_gone_after_stop(tmp_path: Path) -> None:
    col = PgActivityCollector(_unreachable_dsn(), interval_s=1.0)
    col.start("run-g", tmp_path / "pg.jsonl")
    assert len(_sampler_threads()) == 1
    col.stop()
    assert _sampler_threads() == []


def test_stop_without_start_returns_empty_stats() -> None:
    stats = PgActivityCollector(_unreachable_dsn(), interval_s=2.0).stop()
    assert stats == PgActivityStats(samples=0, missed_samples=0, interval_s=2.0, errors=[])


# --- integration (real Postgres in docker) -----------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(_SKIP_REASON is not None, reason=_SKIP_REASON or "")
def test_samples_show_other_connections_wait_events(pg_dsn: str, tmp_path: Path) -> None:
    out = tmp_path / "pg.jsonl"
    col = PgActivityCollector(pg_dsn, interval_s=1.0)
    col.start("run-a6", out)
    sleepers = [threading.Thread(target=_sleep_2s, args=(pg_dsn,), daemon=True) for _ in range(3)]
    for thread in sleepers:
        thread.start()
    time.sleep(4.0)
    stats = col.stop()
    for thread in sleepers:
        thread.join(timeout=10)

    assert stats.errors == []
    assert stats.missed_samples == 0
    assert _sampler_threads() == []

    lines = _lines(out)
    assert len(lines) >= 3
    for line in lines:
        assert set(line) == LINE_KEYS
        assert line["run_id"] == "run-a6"
        assert line["t_host_utc"].endswith("+00:00")
        for row in line["rows"]:
            assert set(row) == ROW_KEYS

    all_rows = [row for line in lines for row in line["rows"]]
    assert any(
        row["wait_event_type"] == "Timeout" and row["wait_event"] == "PgSleep" and row["count"] == 3
        for row in all_rows
    )
    # The collector's own backend is excluded by pg_backend_pid(): there are exactly 3 sleeper
    # connections, so no sample may ever show more than 3 client backends in total. (A sleeper
    # that just started its query can be briefly `active` with no wait event, so the absence of
    # such a row is not a valid check.)
    for line in lines:
        assert sum(row["count"] for row in line["rows"]) <= 3
