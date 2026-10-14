"""Tests for the docker stats collector.

Fixtures are verbatim lines captured from the real `docker stats --format '{{json .}}'`
(Docker 29.5.2 on macOS) against a running `alpine` container named sl-c4. The stream
prefixes each frame with terminal control codes; those are part of the fixture on purpose.
Subprocess behaviour is exercised with a small python -c command injected via `command=`.
"""

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from serverlab.metrics import collector as collector_module
from serverlab.metrics.collector import (
    CollectorStats,
    DockerStatsCollector,
    parse_frame,
    parse_pair,
    parse_percent,
    parse_size,
)

# Real capture, idle container (`sh` sleeping), first frame of the stream.
REAL_IDLE = (
    '\x1b[H{"BlockIO":"0B / 0B","CPUPerc":"0.00%","Container":"sl-c4","ID":"cf6e97a666b1",'
    '"MemPerc":"0.00%","MemUsage":"332KiB / 7.737GiB","Name":"sl-c4","NetIO":"516B / 126B",'
    '"PIDs":"1"}\x1b[K'
)
# Real capture, container running `yes` in the background; second sample of the stream.
REAL_BUSY_PREV = (
    '\x1b[H{"BlockIO":"0B / 0B","CPUPerc":"0.01%","Container":"sl-c4","ID":"fab5ca32559d",'
    '"MemPerc":"0.01%","MemUsage":"912KiB / 7.737GiB","Name":"sl-c4","NetIO":"516B / 126B",'
    '"PIDs":"3"}\x1b[K'
)
# Real capture, same container, later sample with multi-core CPU above 100%.
REAL_BUSY = (
    '\x1b[J\x1b[H{"BlockIO":"0B / 0B","CPUPerc":"204.14%","Container":"sl-c4",'
    '"ID":"fab5ca32559d","MemPerc":"0.01%","MemUsage":"912KiB / 7.737GiB","Name":"sl-c4",'
    '"NetIO":"516B / 126B","PIDs":"3"}\x1b[K'
)

ROW_KEYS = {
    "run_id",
    "t_host_utc",
    "container",
    "cpu_percent",
    "mem_bytes",
    "mem_limit_bytes",
    "net_rx_bytes",
    "net_tx_bytes",
    "block_read_bytes",
    "block_write_bytes",
    "pids",
}


def _py(script: str) -> list[str]:
    return [sys.executable, "-c", script]


def _printing(lines: list[str], gap_s: float = 0.0, exit_code: int = 0) -> list[str]:
    return _py(
        f"import sys, time\n"
        f"for line in {lines!r}:\n"
        f"    print(line, flush=True)\n"
        f"    time.sleep({gap_s!r})\n"
        f"sys.exit({exit_code})\n"
    )


def _wait_for(predicate: Callable[[], bool], timeout_s: float = 10.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    raise AssertionError("condition not reached in time")


def _rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# --- parsing -----------------------------------------------------------------------------


def test_real_idle_frame_parses() -> None:
    obj = json.loads(REAL_IDLE.replace("\x1b[H", "").replace("\x1b[K", ""))
    frame = parse_frame(obj)
    assert frame is not None
    assert frame.container == "sl-c4"
    assert frame.cpu_percent == 0.0
    assert frame.mem_bytes == 332 * 1024
    assert frame.mem_limit_bytes == round(7.737 * 2**30)
    assert frame.net_rx_bytes == 516
    assert frame.net_tx_bytes == 126
    assert frame.block_read_bytes == 0
    assert frame.block_write_bytes == 0
    assert frame.pids == 1
    assert frame.field_errors == ()


def test_real_busy_frame_parses_cpu_above_100() -> None:
    obj = json.loads(REAL_BUSY.replace("\x1b[J", "").replace("\x1b[H", "").replace("\x1b[K", ""))
    frame = parse_frame(obj)
    assert frame is not None
    assert frame.cpu_percent == 204.14
    assert frame.mem_bytes == 912 * 1024
    assert frame.pids == 3


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0B", 0),
        ("516B", 516),
        ("1.2kB", 1200),
        ("3kB", 3000),
        ("10.5MiB", round(10.5 * 2**20)),
        ("7.7GB", 7_700_000_000),
        ("332KiB", 332 * 1024),
        ("7.737GiB", round(7.737 * 2**30)),
    ],
)
def test_parse_size_units(text: str, expected: int) -> None:
    assert parse_size(text) == expected


@pytest.mark.parametrize("text", ["", "12", "12 XB", "MiB", "n/a", "-1MiB"])
def test_parse_size_rejects(text: str) -> None:
    with pytest.raises(ValueError):
        parse_size(text)


def test_parse_pair_and_percent() -> None:
    assert parse_pair("1.2kB / 3kB") == (1200, 3000)
    assert parse_pair("0B / 0B") == (0, 0)
    assert parse_percent("12.34%") == 12.34
    assert parse_percent(" 205.29% ") == 205.29
    with pytest.raises(ValueError):
        parse_pair("1.2kB")
    with pytest.raises(ValueError):
        parse_percent("12.34")
    with pytest.raises(ValueError):
        parse_percent("nan%")


def test_frame_with_bad_field_gets_null_and_error_recorded() -> None:
    obj = json.loads(REAL_IDLE.replace("\x1b[H", "").replace("\x1b[K", ""))
    obj["CPUPerc"] = "--"
    frame = parse_frame(obj)
    assert frame is not None
    assert frame.cpu_percent is None
    assert frame.mem_bytes == 332 * 1024
    assert len(frame.field_errors) == 1
    assert frame.field_errors[0].startswith("CPUPerc")


def test_frame_without_container_name_is_none() -> None:
    assert parse_frame({"CPUPerc": "0.00%"}) is None


# --- construction ------------------------------------------------------------------------


@pytest.mark.parametrize("interval", [0.0, 0.5, 0.999, -1.0, float("nan")])
def test_interval_below_one_second_raises(interval: float) -> None:
    with pytest.raises(ValueError):
        DockerStatsCollector(interval_s=interval, command=_py("pass"))


def test_default_interval_is_one_second() -> None:
    assert DockerStatsCollector(command=_py("pass")).stats().interval_s == 1.0


# --- runtime (canned subprocess, never raises) -------------------------------------------


def test_end_to_end_writes_jsonl_and_downsamples(tmp_path: Path) -> None:
    # docker emits ~1 frame/s; the canned stream emits 4 frames 0.05 s apart.
    # With interval_s=1.0 exactly one sample is written and three are downsampled.
    out = tmp_path / "run.jsonl"
    col = DockerStatsCollector(interval_s=1.0, command=_printing([REAL_IDLE] * 4, gap_s=0.05))
    col.start("run-1", out)
    time.sleep(1.0)
    stats = col.stop()

    assert stats.errors == []
    assert stats.samples == 1
    assert stats.dropped_by_downsampling == 3
    assert stats.parse_errors == 0
    rows = _rows(out)
    assert len(rows) == 1
    assert set(rows[0]) == ROW_KEYS
    assert rows[0]["run_id"] == "run-1"
    assert rows[0]["container"] == "sl-c4"
    assert rows[0]["cpu_percent"] == 0.0
    assert rows[0]["mem_bytes"] == 332 * 1024
    assert rows[0]["pids"] == 1
    assert rows[0]["t_host_utc"].endswith("+00:00")


def test_second_sample_written_after_interval(tmp_path: Path) -> None:
    out = tmp_path / "run.jsonl"
    col = DockerStatsCollector(
        interval_s=1.0, command=_printing([REAL_BUSY_PREV, REAL_BUSY], gap_s=1.2)
    )
    col.start("run-2", out)
    _wait_for(lambda: col.stats().samples == 2 or bool(col.stats().errors))
    stats = col.stop()

    assert stats.errors == []
    assert stats.samples == 2
    assert stats.dropped_by_downsampling == 0
    rows = _rows(out)
    assert [r["cpu_percent"] for r in rows] == [0.01, 204.14]


def test_downsampling_is_per_container(tmp_path: Path) -> None:
    other = REAL_IDLE.replace('"Name":"sl-c4"', '"Name":"sl-c5"')
    out = tmp_path / "run.jsonl"
    col = DockerStatsCollector(
        interval_s=1.0, command=_printing([REAL_IDLE, other, REAL_IDLE, other], gap_s=0.05)
    )
    col.start("run-3", out)
    time.sleep(0.8)
    stats = col.stop()

    assert stats.samples == 2
    assert stats.dropped_by_downsampling == 2
    assert sorted(r["container"] for r in _rows(out)) == ["sl-c4", "sl-c5"]


def test_unparseable_values_are_null_and_counted(tmp_path: Path) -> None:
    bad = REAL_IDLE.replace('"CPUPerc":"0.00%"', '"CPUPerc":"n/a"')
    out = tmp_path / "run.jsonl"
    col = DockerStatsCollector(interval_s=1.0, command=_printing(["garbage line", bad]))
    col.start("run-4", out)
    _wait_for(lambda: col.stats().samples == 1 or bool(col.stats().errors))
    stats = col.stop()

    assert stats.parse_errors == 2  # the non-JSON line and the bad CPU field
    assert stats.samples == 1
    rows = _rows(out)
    assert rows[0]["cpu_percent"] is None
    assert rows[0]["mem_bytes"] == 332 * 1024


def test_stop_terminates_subprocess_and_leaves_no_zombie(tmp_path: Path) -> None:
    endless = _py(
        "import time\n"
        f"line = {REAL_IDLE!r}\n"
        "while True:\n"
        "    print(line, flush=True)\n"
        "    time.sleep(0.1)\n"
    )
    col = DockerStatsCollector(interval_s=1.0, command=endless)
    col.start("run-5", tmp_path / "run.jsonl")
    _wait_for(lambda: col.stats().samples >= 1 or bool(col.stats().errors))
    pid = col.pid
    assert pid is not None

    began = time.monotonic()
    stats = col.stop()
    assert time.monotonic() - began < 10
    assert stats.errors == []
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)  # reaped: no process and no zombie left under this pid


def test_stop_kills_process_that_ignores_sigterm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(collector_module, "_TERMINATE_TIMEOUT_S", 0.5)
    stubborn = _py(
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"line = {REAL_IDLE!r}\n"
        "while True:\n"
        "    print(line, flush=True)\n"
        "    time.sleep(0.1)\n"
    )
    col = DockerStatsCollector(interval_s=1.0, command=stubborn)
    col.start("run-6", tmp_path / "run.jsonl")
    _wait_for(lambda: col.stats().samples >= 1 or bool(col.stats().errors))
    pid = col.pid
    assert pid is not None
    stats = col.stop()
    assert stats.errors == []
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_missing_binary_is_recorded_not_raised(tmp_path: Path) -> None:
    col = DockerStatsCollector(interval_s=1.0, command=["/nonexistent/serverlab-no-such-docker"])
    col.start("run-7", tmp_path / "run.jsonl")  # must return normally
    stats = col.stop()
    assert stats.samples == 0
    assert col.pid is None
    assert len(stats.errors) == 1
    assert "failed to launch" in stats.errors[0]


def test_unwritable_output_is_recorded_not_raised(tmp_path: Path) -> None:
    col = DockerStatsCollector(interval_s=1.0, command=_printing([REAL_IDLE]))
    col.start("run-8", tmp_path / "missing-dir" / "run.jsonl")
    stats = col.stop()
    assert stats.samples == 0
    assert any(e.startswith("cannot open") for e in stats.errors)
    assert col.pid is None


def test_nonzero_exit_and_stderr_are_recorded(tmp_path: Path) -> None:
    failing = _py(
        "import sys\n"
        "sys.stderr.write('Error response from daemon: No such container: nope\\n')\n"
        "sys.exit(3)\n"
    )
    col = DockerStatsCollector(interval_s=1.0, command=failing)
    col.start("run-9", tmp_path / "run.jsonl")
    _wait_for(lambda: any("exited with code" in e for e in col.stats().errors))
    stats = col.stop()
    assert any("exited with code 3" in e for e in stats.errors)
    assert any("No such container: nope" in e for e in stats.errors)
    assert stats.samples == 0


def test_stop_without_start_returns_empty_stats() -> None:
    stats = DockerStatsCollector(interval_s=2.0, command=_py("pass")).stop()
    assert stats == CollectorStats(samples=0, interval_s=2.0, errors=[])
