"""Application metrics poller: GET a JSON /metrics endpoint each interval, write per-interval jsonl.

The endpoint carries cumulative counters and gauges (contract: docs/phases/week4.md section 2).
Each successful poll after the first becomes one JSON line with the gauges as read and the
deltas of the cumulative counters since the previous successful poll. The first successful poll
only primes the baseline.

The collector never raises into the caller. Failed polls (timeout, connection refused, bad JSON,
missing or non-numeric key) are counted in AppMetricsStats and never produce a line with
invented values. A counter that goes down means the app restarted: that poll writes a line with
counter_reset=true and null deltas, then re-baselines.
"""

import json
import math
import threading
import time
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from pydantic import BaseModel, Field

MIN_INTERVAL_S = 1.0
THREAD_NAME = "app-metrics-poller"

_POLL_TIMEOUT_S = 2.0
_JOIN_TIMEOUT_S = 10.0
_MAX_ERRORS = 20

# Gauges are written as read. Counters are diffed between polls.
GAUGE_KEYS: tuple[str, ...] = ("pool_size", "pool_available", "requests_waiting", "in_flight")
COUNTER_KEYS: tuple[str, ...] = (
    "requests_num",
    "requests_queued",
    "requests_wait_ms",
    "ok",
    "errors",
)
# Every key the endpoint must provide. A body missing any of them is a failed poll.
CONTRACT_KEYS: tuple[str, ...] = (
    "pool_min",
    "pool_max",
    "pool_size",
    "pool_available",
    "requests_waiting",
    "requests_num",
    "requests_queued",
    "requests_wait_ms",
    "requests_wait_max",
    "in_flight",
    "ok",
    "errors",
)

# Opener without proxy handling: the metrics endpoint is local or on the lab network, and a
# proxy from the environment must never sit between the collector and the application.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

Number = int | float


class AppMetricsStats(BaseModel):
    samples: int = 0
    missed_samples: int = 0
    counter_resets: int = 0
    interval_s: float
    errors: list[str] = Field(default_factory=list)


def parse_snapshot(body: object) -> dict[str, Number]:
    """Validate one /metrics body. Raises ValueError on anything that is not a full contract."""
    if not isinstance(body, Mapping):
        raise ValueError("body is not a JSON object")
    values: dict[str, Number] = {}
    for key in CONTRACT_KEYS:
        if key not in body:
            raise ValueError(f"missing key {key!r}")
        raw = body[key]
        if isinstance(raw, bool) or not isinstance(raw, int | float):
            raise ValueError(f"key {key!r} is not a number: {raw!r}")
        if isinstance(raw, float) and not math.isfinite(raw):
            raise ValueError(f"key {key!r} is not finite: {raw!r}")
        values[key] = raw
    return values


@dataclass(frozen=True)
class _Baseline:
    mono: float
    counters: Mapping[str, Number]


class AppMetricsCollector:
    """Polls `url` every `interval_s` seconds and writes one JSON line per successful poll.

    Usage: start(run_id, out_path) returns immediately; stop() ends the poller thread, closes
    the file and returns AppMetricsStats.
    """

    def __init__(self, url: str, interval_s: float = 1.0) -> None:
        if not (math.isfinite(interval_s) and interval_s >= MIN_INTERVAL_S):
            raise ValueError(f"interval_s must be >= {MIN_INTERVAL_S}, got {interval_s!r}")
        self._url = url
        self._interval_s = float(interval_s)
        self._lock = threading.Lock()
        self._errors: list[str] = []
        self._samples = 0
        self._missed = 0
        self._resets = 0
        self._run_id = ""
        self._out: IO[str] | None = None
        self._out_closed = False
        self._thread: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._started = False
        self._stopped = False

    def start(self, run_id: str, out_path: Path) -> None:
        try:
            self._start(run_id, out_path)
        except Exception as exc:
            self._add_error(f"start failed: {type(exc).__name__}: {exc}")

    def stop(self) -> AppMetricsStats:
        try:
            self._stop()
        except Exception as exc:
            self._add_error(f"stop failed: {type(exc).__name__}: {exc}")
        return self.stats()

    def stats(self) -> AppMetricsStats:
        with self._lock:
            return AppMetricsStats(
                samples=self._samples,
                missed_samples=self._missed,
                counter_resets=self._resets,
                interval_s=self._interval_s,
                errors=list(self._errors),
            )

    def _start(self, run_id: str, out_path: Path) -> None:
        if self._started:
            self._add_error("start() called more than once; ignored")
            return
        self._started = True
        self._run_id = run_id
        try:
            self._out = out_path.open("w", encoding="utf-8")
        except OSError as exc:
            self._add_error(f"cannot open {out_path}: {exc}")
            return
        self._thread = threading.Thread(target=self._run, name=THREAD_NAME, daemon=True)
        self._thread.start()

    def _stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_requested.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=_JOIN_TIMEOUT_S)
            if thread.is_alive():
                self._add_error(f"thread {thread.name} did not exit after stop")
        self._close_out()

    def _run(self) -> None:
        try:
            self._poll_loop()
        except Exception as exc:
            self._add_error(f"poller crashed: {type(exc).__name__}: {exc}")

    def _poll_loop(self) -> None:
        baseline: _Baseline | None = None
        while not self._stop_requested.is_set():
            try:
                snapshot = self._fetch()
            except Exception as exc:
                self._count_missed(f"poll failed: {type(exc).__name__}: {exc}")
            else:
                now = time.monotonic()
                if baseline is None:
                    baseline = _Baseline(mono=now, counters=_counters(snapshot))
                else:
                    baseline = self._emit(snapshot, baseline, now)
            self._stop_requested.wait(self._interval_s)

    def _fetch(self) -> dict[str, Number]:
        timeout = min(_POLL_TIMEOUT_S, self._interval_s)
        with _OPENER.open(self._url, timeout=timeout) as response:
            raw = response.read()
        return parse_snapshot(json.loads(raw))

    def _emit(self, snapshot: dict[str, Number], baseline: _Baseline, now: float) -> _Baseline:
        counters = _counters(snapshot)
        reset = any(counters[k] < baseline.counters[k] for k in COUNTER_KEYS)
        record: dict[str, object] = {
            "run_id": self._run_id,
            "t_host_utc": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "interval_s_actual": round(now - baseline.mono, 6),
        }
        for key in GAUGE_KEYS:
            record[key] = snapshot[key]
        if reset:
            record["counter_reset"] = True
            for key in COUNTER_KEYS:
                record[f"delta_{key}"] = None
            record["mean_wait_ms"] = None
        else:
            deltas = {k: counters[k] - baseline.counters[k] for k in COUNTER_KEYS}
            record["counter_reset"] = False
            for key in COUNTER_KEYS:
                record[f"delta_{key}"] = deltas[key]
            queued = deltas["requests_queued"]
            record["mean_wait_ms"] = deltas["requests_wait_ms"] / queued if queued > 0 else None
        with self._lock:
            if reset:
                self._resets += 1
            if self._out is None or self._out_closed:
                return _Baseline(mono=now, counters=counters)
            try:
                self._out.write(json.dumps(record) + "\n")
                self._out.flush()
                self._samples += 1
            except OSError as exc:
                self._errors.append(f"write failed: {exc}")
        return _Baseline(mono=now, counters=counters)

    def _count_missed(self, message: str) -> None:
        with self._lock:
            self._missed += 1
            if len(self._errors) < _MAX_ERRORS:
                self._errors.append(message)

    def _add_error(self, message: str) -> None:
        with self._lock:
            self._errors.append(message)

    def _close_out(self) -> None:
        with self._lock:
            if self._out is not None and not self._out_closed:
                try:
                    self._out.close()
                except OSError as exc:
                    self._errors.append(f"closing output file failed: {exc}")
            self._out_closed = True


def _counters(snapshot: Mapping[str, Number]) -> dict[str, Number]:
    return {k: snapshot[k] for k in COUNTER_KEYS}
