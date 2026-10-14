"""Out-of-band container metrics: one host-side `docker stats` stream written as JSON lines.

The collector never raises into the caller. Every failure (docker missing, a crash of the
reader, an unwritable output file, a non-zero exit) is recorded in CollectorStats.errors.
Unparseable values become null and are counted in CollectorStats.parse_errors.
"""

import json
import math
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import IO

from pydantic import BaseModel, Field

MIN_INTERVAL_S = 1.0
DOCKER_STATS_ARGV: tuple[str, ...] = ("docker", "stats", "--format", "{{json .}}")

_TERMINATE_TIMEOUT_S = 5.0
_JOIN_TIMEOUT_S = 10.0
_MAX_STDERR_LINES = 20

# Terminal control sequences that docker emits even when stdout is a pipe.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_SIZE_RE = re.compile(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z]+)\s*")
_COUNT_RE = re.compile(r"\s*[0-9]+\s*")
# Units as printed by docker: decimal kB/MB/GB/TB for net and block IO, binary for memory.
_UNITS: dict[str, int] = {
    "B": 1,
    "kB": 10**3,
    "MB": 10**6,
    "GB": 10**9,
    "TB": 10**12,
    "KiB": 2**10,
    "MiB": 2**20,
    "GiB": 2**30,
    "TiB": 2**40,
}


class CollectorStats(BaseModel):
    samples: int = 0
    parse_errors: int = 0
    dropped_by_downsampling: int = 0
    interval_s: float
    errors: list[str] = Field(default_factory=list)


def parse_percent(text: str) -> float:
    """'12.34%' -> 12.34. Docker reports above 100 for multi-core use ('205.29%')."""
    stripped = text.strip()
    if not stripped.endswith("%"):
        raise ValueError(f"not a percentage: {text!r}")
    value = float(stripped[:-1])
    if not math.isfinite(value):
        raise ValueError(f"non-finite percentage: {text!r}")
    return value


def parse_size(text: str) -> int:
    """'10.5MiB' -> 11010048, '1.2kB' -> 1200, '0B' -> 0."""
    match = _SIZE_RE.fullmatch(text)
    if match is None or match.group(2) not in _UNITS:
        raise ValueError(f"not a size: {text!r}")
    return round(float(match.group(1)) * _UNITS[match.group(2)])


def parse_pair(text: str) -> tuple[int, int]:
    """'10.5MiB / 7.7GiB' -> (bytes used, bytes limit)."""
    parts = text.split("/")
    if len(parts) != 2:
        raise ValueError(f"not a 'a / b' pair: {text!r}")
    return parse_size(parts[0]), parse_size(parts[1])


def parse_count(text: str) -> int:
    if _COUNT_RE.fullmatch(text) is None:
        raise ValueError(f"not a count: {text!r}")
    return int(text)


def _field[T](
    obj: Mapping[str, object], key: str, parse: Callable[[str], T], errors: list[str]
) -> T | None:
    raw = obj.get(key)
    if not isinstance(raw, str):
        errors.append(f"{key}: missing or not a string")
        return None
    try:
        return parse(raw)
    except ValueError as exc:
        errors.append(f"{key}: {exc}")
        return None


@dataclass(frozen=True)
class Frame:
    container: str
    cpu_percent: float | None
    mem_bytes: int | None
    mem_limit_bytes: int | None
    net_rx_bytes: int | None
    net_tx_bytes: int | None
    block_read_bytes: int | None
    block_write_bytes: int | None
    pids: int | None
    field_errors: tuple[str, ...]


def parse_frame(obj: Mapping[str, object]) -> Frame | None:
    """Parse one `docker stats --format '{{json .}}'` object. None if it has no container name."""
    name = obj.get("Name") or obj.get("Container")
    if not isinstance(name, str) or not name.strip():
        return None
    errors: list[str] = []
    cpu = _field(obj, "CPUPerc", parse_percent, errors)
    mem = _field(obj, "MemUsage", parse_pair, errors)
    net = _field(obj, "NetIO", parse_pair, errors)
    blk = _field(obj, "BlockIO", parse_pair, errors)
    pids = _field(obj, "PIDs", parse_count, errors)
    return Frame(
        container=name.strip(),
        cpu_percent=cpu,
        mem_bytes=mem[0] if mem else None,
        mem_limit_bytes=mem[1] if mem else None,
        net_rx_bytes=net[0] if net else None,
        net_tx_bytes=net[1] if net else None,
        block_read_bytes=blk[0] if blk else None,
        block_write_bytes=blk[1] if blk else None,
        pids=pids,
        field_errors=tuple(errors),
    )


class DockerStatsCollector:
    """Streams `docker stats` on the host and writes one JSON line per container per sample.

    Usage: start(run_id, out_path) returns immediately; stop() terminates the subprocess,
    joins the reader, closes the file and returns CollectorStats.
    """

    def __init__(
        self,
        interval_s: float = 1.0,
        containers: list[str] | None = None,
        command: Sequence[str] | None = None,
    ) -> None:
        if not interval_s >= MIN_INTERVAL_S:
            raise ValueError(f"interval_s must be >= {MIN_INTERVAL_S}, got {interval_s!r}")
        self._interval_s = float(interval_s)
        self._argv: list[str] = list(command) if command is not None else list(DOCKER_STATS_ARGV)
        if containers:
            self._argv.extend(containers)
        self._lock = threading.Lock()
        self._errors: list[str] = []
        self._samples = 0
        self._parse_errors = 0
        self._dropped = 0
        self._stderr_lines = 0
        self._last_written: dict[str, float] = {}
        self._run_id = ""
        self._proc: subprocess.Popen[str] | None = None
        self._out: IO[str] | None = None
        self._out_closed = False
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stop_requested = threading.Event()
        self._started = False
        self._stopped = False

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def start(self, run_id: str, out_path: Path) -> None:
        try:
            self._start(run_id, out_path)
        except Exception as exc:
            self._add_error(f"start failed: {type(exc).__name__}: {exc}")

    def stop(self) -> CollectorStats:
        try:
            self._stop()
        except Exception as exc:
            self._add_error(f"stop failed: {type(exc).__name__}: {exc}")
        return self.stats()

    def stats(self) -> CollectorStats:
        with self._lock:
            return CollectorStats(
                samples=self._samples,
                parse_errors=self._parse_errors,
                dropped_by_downsampling=self._dropped,
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
        try:
            proc: subprocess.Popen[str] = subprocess.Popen(
                self._argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except (OSError, ValueError) as exc:
            self._add_error(f"failed to launch {self._argv[0]!r}: {exc}")
            self._close_out()
            return
        self._proc = proc
        self._reader = threading.Thread(
            target=self._read_loop, args=(proc,), name="docker-stats-reader", daemon=True
        )
        self._stderr_reader = threading.Thread(
            target=self._drain_stderr, args=(proc,), name="docker-stats-stderr", daemon=True
        )
        self._reader.start()
        self._stderr_reader.start()

    def _stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        self._stop_requested.set()
        proc = self._proc
        if proc is not None:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=_TERMINATE_TIMEOUT_S)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
            except OSError as exc:
                self._add_error(f"terminate failed: {exc}")
        for thread in (self._reader, self._stderr_reader):
            if thread is not None:
                thread.join(timeout=_JOIN_TIMEOUT_S)
                if thread.is_alive():
                    self._add_error(f"thread {thread.name} did not exit after stop")
        if proc is not None:
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
        self._close_out()

    def _read_loop(self, proc: subprocess.Popen[str]) -> None:
        try:
            if proc.stdout is None:
                raise RuntimeError("docker stats has no stdout pipe")
            for raw in proc.stdout:
                self._handle_line(raw)
            returncode = proc.wait()
            if returncode != 0 and not self._stop_requested.is_set():
                self._add_error(f"docker stats exited with code {returncode}")
        except Exception as exc:
            self._add_error(f"reader crashed: {type(exc).__name__}: {exc}")

    def _drain_stderr(self, proc: subprocess.Popen[str]) -> None:
        try:
            if proc.stderr is None:
                return
            for raw in proc.stderr:
                text = raw.strip()
                if not text:
                    continue
                with self._lock:
                    self._stderr_lines += 1
                    if self._stderr_lines <= _MAX_STDERR_LINES:
                        self._errors.append(f"docker stats stderr: {text}")
        except Exception as exc:
            self._add_error(f"stderr reader crashed: {type(exc).__name__}: {exc}")

    def _handle_line(self, raw: str) -> None:
        text = _ANSI_RE.sub("", raw).strip()
        if not text:
            return
        received_at = datetime.now(UTC)
        received_mono = time.monotonic()
        try:
            obj: object = json.loads(text)
        except json.JSONDecodeError:
            self._count_parse_errors(1)
            return
        frame = parse_frame(obj) if isinstance(obj, dict) else None
        if frame is None:
            self._count_parse_errors(1)
            return
        self._count_parse_errors(len(frame.field_errors))
        with self._lock:
            if self._out_closed or self._out is None:
                return
            last = self._last_written.get(frame.container)
            if last is not None and received_mono - last < self._interval_s:
                self._dropped += 1
                return
            self._last_written[frame.container] = received_mono
            record = {
                "run_id": self._run_id,
                "t_host_utc": received_at.isoformat(timespec="milliseconds"),
                "container": frame.container,
                "cpu_percent": frame.cpu_percent,
                "mem_bytes": frame.mem_bytes,
                "mem_limit_bytes": frame.mem_limit_bytes,
                "net_rx_bytes": frame.net_rx_bytes,
                "net_tx_bytes": frame.net_tx_bytes,
                "block_read_bytes": frame.block_read_bytes,
                "block_write_bytes": frame.block_write_bytes,
                "pids": frame.pids,
            }
            self._out.write(json.dumps(record) + "\n")
            self._out.flush()
            self._samples += 1

    def _count_parse_errors(self, n: int) -> None:
        if n:
            with self._lock:
                self._parse_errors += n

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
