"""Host-side pg_stat_activity sampler: one JSON line per interval with wait-event counts.

Same lifecycle and error model as serverlab.metrics.collector.DockerStatsCollector: start()
returns immediately, stop() joins the sampler thread and returns PgActivityStats, and nothing
raises into the caller. A failed connect or query is recorded in PgActivityStats.errors and
counted in missed_samples; the connection is re-established on the next interval.
"""

import contextlib
import json
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

import psycopg
from pydantic import BaseModel, Field

from serverlab.metrics.collector import MIN_INTERVAL_S

SAMPLE_QUERY = (
    "SELECT state, wait_event_type, wait_event, count(*) FROM pg_stat_activity "
    "WHERE backend_type = 'client backend' AND pid <> pg_backend_pid() GROUP BY 1,2,3"
)

_STATEMENT_TIMEOUT_MS = 2000
_CONNECT_TIMEOUT_S = 5
_JOIN_TIMEOUT_S = 10.0
_THREAD_NAME = "pg-activity-collector"

type _Conn = psycopg.Connection[tuple[Any, ...]]


class PgActivityStats(BaseModel):
    samples: int = 0
    missed_samples: int = 0
    interval_s: float
    errors: list[str] = Field(default_factory=list)


def _close_quietly(conn: _Conn) -> None:
    with contextlib.suppress(psycopg.Error):
        conn.close()


class PgActivityCollector:
    """Samples wait events of client backends over one persistent connection.

    Usage: start(run_id, out_path) returns immediately; stop() joins the sampler thread,
    closes the connection and file, and returns PgActivityStats.
    """

    def __init__(self, dsn: str, interval_s: float = 1.0) -> None:
        if not interval_s >= MIN_INTERVAL_S:
            raise ValueError(f"interval_s must be >= {MIN_INTERVAL_S}, got {interval_s!r}")
        self._dsn = dsn
        self._interval_s = float(interval_s)
        self._lock = threading.Lock()
        self._errors: list[str] = []
        self._samples = 0
        self._missed = 0
        self._stop_event = threading.Event()
        self._out: IO[str] | None = None
        self._out_closed = False
        self._thread: threading.Thread | None = None
        self._started = False
        self._stopped = False

    def start(self, run_id: str, out_path: Path) -> None:
        try:
            self._start(run_id, out_path)
        except Exception as exc:
            self._add_error(f"start failed: {type(exc).__name__}: {exc}")

    def stop(self) -> PgActivityStats:
        try:
            self._stop()
        except Exception as exc:
            self._add_error(f"stop failed: {type(exc).__name__}: {exc}")
        return self.stats()

    def stats(self) -> PgActivityStats:
        with self._lock:
            return PgActivityStats(
                samples=self._samples,
                missed_samples=self._missed,
                interval_s=self._interval_s,
                errors=list(self._errors),
            )

    def _start(self, run_id: str, out_path: Path) -> None:
        if self._started:
            self._add_error("start() called more than once; ignored")
            return
        self._started = True
        try:
            self._out = out_path.open("w", encoding="utf-8")
        except OSError as exc:
            self._add_error(f"cannot open {out_path}: {exc}")
            return
        self._thread = threading.Thread(
            target=self._run, args=(run_id,), name=_THREAD_NAME, daemon=True
        )
        self._thread.start()

    def _stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=_JOIN_TIMEOUT_S)
            if thread.is_alive():
                self._add_error(f"thread {thread.name} did not exit after stop")
        self._close_out()

    def _run(self, run_id: str) -> None:
        conn: _Conn | None = None
        next_at = time.monotonic()
        try:
            while not self._stop_event.is_set():
                conn = self._sample(conn, run_id)
                now = time.monotonic()
                next_at = max(next_at + self._interval_s, now)
                self._stop_event.wait(next_at - now)
        except Exception as exc:
            self._add_error(f"sampler crashed: {type(exc).__name__}: {exc}")
        finally:
            if conn is not None:
                _close_quietly(conn)

    def _connect(self) -> _Conn:
        conn: _Conn = psycopg.connect(
            self._dsn, autocommit=True, connect_timeout=_CONNECT_TIMEOUT_S
        )
        try:
            conn.execute(f"SET statement_timeout = {_STATEMENT_TIMEOUT_MS}")
        except psycopg.Error:
            _close_quietly(conn)
            raise
        return conn

    def _sample(self, conn: _Conn | None, run_id: str) -> _Conn | None:
        try:
            if conn is None:
                conn = self._connect()
            sampled_at = datetime.now(UTC)
            with conn.cursor() as cur:
                cur.execute(SAMPLE_QUERY)
                fetched = cur.fetchall()
        except psycopg.Error as exc:
            self._add_missed(f"sample failed: {type(exc).__name__}: {exc}")
            if conn is not None:
                _close_quietly(conn)
            return None
        record: dict[str, object] = {
            "run_id": run_id,
            "t_host_utc": sampled_at.isoformat(timespec="milliseconds"),
            "rows": [
                {"state": state, "wait_event_type": wtype, "wait_event": wevent, "count": count}
                for state, wtype, wevent, count in fetched
            ],
        }
        self._write(record)
        return conn

    def _write(self, record: dict[str, object]) -> None:
        with self._lock:
            if self._out_closed or self._out is None:
                return
            try:
                self._out.write(json.dumps(record) + "\n")
                self._out.flush()
            except OSError as exc:
                self._errors.append(f"write failed: {exc}")
                return
            self._samples += 1

    def _add_missed(self, message: str) -> None:
        with self._lock:
            self._missed += 1
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
