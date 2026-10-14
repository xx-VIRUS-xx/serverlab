"""HDR histograms built from k6 per-request output (`--out json`, NDJSON).

k6 does not emit HDR histograms, so they are built here from the per-request
points of the ``http_req_duration`` metric.

Encoding and range (fixed, shared by every histogram this module creates):

* Values are recorded in **microseconds**. k6 reports durations as float
  milliseconds; each value is converted with ``round(value_ms * 1000)``.
* Trackable range: 1 us to 60 s (``LOWEST_US`` .. ``HIGHEST_US``).
* 3 significant digits (``SIG_FIGS``), i.e. the HDR bucket precision is at
  most 0.1% of the recorded value.
* Serialisation is the standard compressed HDR V2 encoding produced by
  ``HdrHistogram.encode()`` (base64 text of a zlib-compressed payload), so files
  written by ``write_hdr`` can be decoded by any HdrHistogram implementation and
  merged later with ``HdrHistogram.add``.

Range handling: the ``hdrhistogram`` package does NOT enforce the configured
range (``record_value`` accepts e.g. 0 and values slightly above the highest
trackable value). This module therefore checks the range itself. Values outside
``[LOWEST_US, HIGHEST_US]`` are never recorded and are never clamped; they are
counted in ``HdrBuild.below_range`` / ``HdrBuild.above_range`` and reported to
the caller.

Parsing: ``build_hdr_from_json`` reads the file line by line (memory is bounded
by one line plus the histogram, independent of file size). Only ``Point`` lines
of ``http_req_duration`` are recorded; other lines (``Metric`` definitions,
other metrics) are counted as ``other_lines``. Blank lines are counted as
``blank_lines``. A malformed line (invalid JSON, a non-object, a
``http_req_duration`` point without a finite numeric ``data.value``) raises
``ValueError`` naming the file and line number; malformed lines are never
skipped silently.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hdrh.histogram import HdrHistogram  # type: ignore[import-untyped]

__all__ = [
    "HIGHEST_US",
    "LOWEST_US",
    "METRIC_NAME",
    "SIG_FIGS",
    "HdrBuild",
    "build_hdr",
    "build_hdr_from_json",
    "read_hdr",
    "write_hdr",
]

LOWEST_US = 1
HIGHEST_US = 60_000_000  # 60 s
SIG_FIGS = 3
METRIC_NAME = "http_req_duration"


@dataclass(frozen=True)
class HdrBuild:
    """Result of building a histogram, including every value that was not recorded."""

    histogram: HdrHistogram
    recorded: int
    below_range: int  # values whose rounded microsecond value is < LOWEST_US
    above_range: int  # values whose microsecond value is > HIGHEST_US
    lines: int = 0  # total lines read (file input only)
    blank_lines: int = 0
    other_lines: int = 0  # Metric lines and Point lines of other metrics


class _Accumulator:
    def __init__(self) -> None:
        self.histogram: HdrHistogram = HdrHistogram(LOWEST_US, HIGHEST_US, SIG_FIGS)
        self.recorded = 0
        self.below_range = 0
        self.above_range = 0

    def add_ms(self, value_ms: float) -> None:
        if not math.isfinite(value_ms):
            raise ValueError(f"non-finite duration value {value_ms!r}")
        value_us = round(value_ms * 1000)
        if value_us < LOWEST_US:
            self.below_range += 1
            return
        if value_us > HIGHEST_US:
            self.above_range += 1
            return
        if not self.histogram.record_value(value_us):
            raise ValueError(f"histogram rejected in-range value {value_us} us")
        self.recorded += 1

    def result(self, **extra: int) -> HdrBuild:
        return HdrBuild(
            histogram=self.histogram,
            recorded=self.recorded,
            below_range=self.below_range,
            above_range=self.above_range,
            **extra,
        )


def build_hdr(request_durations_ms: Iterable[float]) -> HdrBuild:
    """Build a histogram from in-memory durations given in milliseconds."""
    acc = _Accumulator()
    for value in request_durations_ms:
        acc.add_ms(float(value))
    return acc.result()


def _as_value(obj: Any, where: str) -> float:
    if not isinstance(obj, dict):
        raise ValueError(f"{where}: JSON line is not an object")
    data = obj.get("data")
    if not isinstance(data, dict):
        raise ValueError(f"{where}: Point has no 'data' object")
    value = data.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{where}: Point 'data.value' is not a number")
    if not math.isfinite(value):
        raise ValueError(f"{where}: Point 'data.value' is not finite")
    return float(value)


def build_hdr_from_json(path: Path) -> HdrBuild:
    """Stream a k6 ``--out json`` NDJSON file and build an ``http_req_duration`` histogram.

    Raises ``ValueError`` (with file and line number) on the first malformed line.
    Values outside 1 us .. 60 s are reported in the result, not recorded or clamped.
    """
    acc = _Accumulator()
    lines = blank = other = 0
    where_prefix = str(path)
    with path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            lines += 1
            text = raw.strip()
            if not text:
                blank += 1
                continue
            where = f"{where_prefix}:{lineno}"
            try:
                obj = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{where}: malformed JSON ({exc.msg})") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"{where}: JSON line is not an object")
            if obj.get("type") != "Point" or obj.get("metric") != METRIC_NAME:
                other += 1
                continue
            acc.add_ms(_as_value(obj, where))
    return acc.result(lines=lines, blank_lines=blank, other_lines=other)


def write_hdr(hist: HdrHistogram, path: Path) -> Path:
    """Write a histogram in the standard compressed HDR V2 encoding."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(hist.encode())
    return path


def read_hdr(path: Path) -> HdrHistogram:
    """Read a histogram written by ``write_hdr`` (or any compressed HDR V2 blob)."""
    return HdrHistogram.decode(path.read_bytes())
