"""Tests for the application metrics poller.

The JSON bodies served by the local http.server in these tests are TEST INPUTS written by hand
to exercise the delta math and failure paths. They are not captured from the real application.
The scripted server returns the bodies in order and repeats the last one once they run out.
"""

import json
import math
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from serverlab.metrics.app_metrics import (
    CONTRACT_KEYS,
    THREAD_NAME,
    AppMetricsCollector,
    AppMetricsStats,
)

ROW_KEYS = {
    "run_id",
    "t_host_utc",
    "interval_s_actual",
    "pool_size",
    "pool_available",
    "requests_waiting",
    "in_flight",
    "counter_reset",
    "delta_requests_num",
    "delta_requests_queued",
    "delta_requests_wait_ms",
    "delta_ok",
    "delta_errors",
    "mean_wait_ms",
}


def _snap(**overrides: int) -> bytes:
    body: dict[str, int] = {key: 0 for key in CONTRACT_KEYS}
    body.update(pool_min=2, pool_max=10, pool_size=10, pool_available=10)
    body.update(overrides)
    return json.dumps(body).encode("utf-8")


def _wait_for(predicate: Callable[[], bool], timeout_s: float = 15.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@contextmanager
def _scripted_server(bodies: list[bytes]) -> Iterator[str]:
    served = [0]
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            with lock:
                index = min(served[0], len(bodies) - 1)
                served[0] += 1
            payload = bodies[index]
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/metrics"
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def _closed_port_url() -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}/metrics"


# --- construction ------------------------------------------------------------------------


@pytest.mark.parametrize("interval", [0.0, 0.5, 0.999, -1.0, float("nan"), float("inf")])
def test_interval_below_one_second_raises(interval: float) -> None:
    with pytest.raises(ValueError):
        AppMetricsCollector("http://127.0.0.1:1/metrics", interval_s=interval)


def test_default_interval_is_one_second() -> None:
    assert AppMetricsCollector("http://127.0.0.1:1/metrics").stats().interval_s == 1.0


def test_stop_without_start_returns_empty_stats() -> None:
    stats = AppMetricsCollector("http://127.0.0.1:1/metrics", interval_s=2.0).stop()
    assert stats == AppMetricsStats(samples=0, interval_s=2.0, errors=[])


# --- delta math --------------------------------------------------------------------------


def test_delta_math_and_first_poll_primes_only(tmp_path: Path) -> None:
    prime = _snap(
        requests_num=100,
        requests_queued=10,
        requests_wait_ms=500,
        ok=100,
        errors=0,
        in_flight=6,
        requests_waiting=3,
        pool_available=4,
    )
    second = _snap(
        requests_num=130,
        requests_queued=14,
        requests_wait_ms=900,
        ok=128,
        errors=2,
        in_flight=8,
        requests_waiting=5,
        pool_available=2,
    )
    out = tmp_path / "app.jsonl"
    with _scripted_server([prime, second]) as url:
        col = AppMetricsCollector(url, interval_s=1.0)
        col.start("run-a", out)
        _wait_for(lambda: col.stats().samples >= 1 or bool(col.stats().errors))
        stats = col.stop()

    assert stats.errors == []
    assert stats.missed_samples == 0
    assert stats.samples == 1  # the first poll wrote nothing
    rows = _rows(out)
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == ROW_KEYS
    assert row["run_id"] == "run-a"
    assert row["t_host_utc"].endswith("+00:00")
    assert row["interval_s_actual"] >= 0.9
    assert row["pool_size"] == 10
    assert row["pool_available"] == 2
    assert row["requests_waiting"] == 5
    assert row["in_flight"] == 8
    assert row["counter_reset"] is False
    assert row["delta_requests_num"] == 30
    assert row["delta_requests_queued"] == 4
    assert row["delta_requests_wait_ms"] == 400
    assert row["delta_ok"] == 28
    assert row["delta_errors"] == 2
    assert row["mean_wait_ms"] == 100.0


def test_mean_wait_is_null_when_no_request_queued(tmp_path: Path) -> None:
    same = _snap(requests_num=50, requests_queued=7, requests_wait_ms=70, ok=50)
    out = tmp_path / "app.jsonl"
    with _scripted_server([same, same]) as url:
        col = AppMetricsCollector(url, interval_s=1.0)
        col.start("run-b", out)
        _wait_for(lambda: col.stats().samples >= 1 or bool(col.stats().errors))
        stats = col.stop()

    assert stats.errors == []
    rows = _rows(out)
    assert rows[0]["delta_requests_queued"] == 0
    assert rows[0]["delta_requests_num"] == 0
    assert rows[0]["mean_wait_ms"] is None


def test_counter_reset_writes_null_deltas_then_rebaselines(tmp_path: Path) -> None:
    prime = _snap(requests_num=500, requests_queued=40, requests_wait_ms=4000, ok=490, errors=10)
    restarted = _snap(requests_num=3, requests_queued=0, requests_wait_ms=0, ok=3, errors=0)
    after = _snap(requests_num=10, requests_queued=2, requests_wait_ms=50, ok=8, errors=0)
    out = tmp_path / "app.jsonl"
    with _scripted_server([prime, restarted, after]) as url:
        col = AppMetricsCollector(url, interval_s=1.0)
        col.start("run-c", out)
        _wait_for(lambda: col.stats().samples >= 2 or bool(col.stats().errors))
        stats = col.stop()

    assert stats.errors == []
    assert stats.counter_resets == 1
    rows = _rows(out)
    assert len(rows) == 2
    assert rows[0]["counter_reset"] is True
    assert rows[0]["delta_requests_num"] is None
    assert rows[0]["delta_requests_wait_ms"] is None
    assert rows[0]["mean_wait_ms"] is None
    assert rows[0]["pool_size"] == 10  # gauges are still written on a reset
    assert rows[1]["counter_reset"] is False
    assert rows[1]["delta_requests_num"] == 7  # measured from the re-baselined restart poll
    assert rows[1]["mean_wait_ms"] == 25.0


# --- failed polls ------------------------------------------------------------------------


def test_bad_json_and_missing_key_are_counted_and_never_written(tmp_path: Path) -> None:
    prime = _snap(requests_num=10, requests_queued=1, requests_wait_ms=5, ok=10)
    missing = json.loads(_snap(requests_num=11))
    del missing["in_flight"]
    good = _snap(requests_num=20, requests_queued=3, requests_wait_ms=30, ok=20)
    out = tmp_path / "app.jsonl"
    with _scripted_server([prime, b"{not json", json.dumps(missing).encode(), good]) as url:
        col = AppMetricsCollector(url, interval_s=1.0)
        col.start("run-d", out)
        _wait_for(lambda: col.stats().samples >= 1)
        stats = col.stop()

    assert stats.missed_samples == 2
    assert stats.samples == 1
    assert len(stats.errors) == 2
    rows = _rows(out)
    assert len(rows) == 1
    # The baseline is the last successful poll, so the delta spans the two failed intervals.
    assert rows[0]["delta_requests_num"] == 10
    assert rows[0]["delta_requests_queued"] == 2
    assert rows[0]["interval_s_actual"] >= 1.5


def test_connection_refused_is_counted_and_writes_nothing(tmp_path: Path) -> None:
    out = tmp_path / "app.jsonl"
    col = AppMetricsCollector(_closed_port_url(), interval_s=1.0)
    col.start("run-e", out)
    _wait_for(lambda: col.stats().missed_samples >= 2)
    stats = col.stop()

    assert stats.samples == 0
    assert stats.missed_samples >= 2
    assert all(e.startswith("poll failed") for e in stats.errors)
    assert out.read_text(encoding="utf-8") == ""


def test_unwritable_output_is_recorded_not_raised(tmp_path: Path) -> None:
    col = AppMetricsCollector("http://127.0.0.1:1/metrics", interval_s=1.0)
    col.start("run-f", tmp_path / "missing-dir" / "app.jsonl")
    stats = col.stop()
    assert stats.samples == 0
    assert any(e.startswith("cannot open") for e in stats.errors)


# --- lifecycle ---------------------------------------------------------------------------


def test_stop_twice_is_safe_and_thread_is_gone_after_stop(tmp_path: Path) -> None:
    prime = _snap(requests_num=1)
    with _scripted_server([prime]) as url:
        col = AppMetricsCollector(url, interval_s=1.0)
        col.start("run-g", tmp_path / "app.jsonl")
        _wait_for(lambda: any(t.name == THREAD_NAME for t in threading.enumerate()))
        first = col.stop()
        second = col.stop()

    assert first == second
    assert first.errors == []
    assert not any(t.name == THREAD_NAME and t.is_alive() for t in threading.enumerate())
    assert math.isclose(first.interval_s, 1.0)
