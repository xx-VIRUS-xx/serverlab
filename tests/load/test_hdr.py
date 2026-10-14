"""Tests for HDR histogram construction from k6 NDJSON output (no k6 or server needed)."""

import json
import math
from pathlib import Path
from typing import Any

import pytest

from serverlab.load.hdr import (
    HIGHEST_US,
    LOWEST_US,
    build_hdr,
    build_hdr_from_json,
    read_hdr,
    write_hdr,
)


def _point(value_ms: float, metric: str = "http_req_duration") -> str:
    return json.dumps(
        {
            "type": "Point",
            "metric": metric,
            "data": {"time": "2026-10-14T00:00:00Z", "value": value_ms, "tags": {}},
        }
    )


def _write_ndjson(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _summary(hist: Any) -> tuple[int, list[tuple[int, int]], list[int]]:
    recorded = [
        (int(item.value_iterated_to), int(item.count_at_value_iterated_to))
        for item in hist.get_recorded_iterator()
    ]
    pcts = [hist.get_value_at_percentile(p) for p in (50.0, 90.0, 99.0, 99.9)]
    return hist.get_total_count(), recorded, pcts


def test_round_trip_identical_counts_and_percentiles(tmp_path: Path) -> None:
    values = [0.5 + (i * 37 % 997) / 10 for i in range(5000)]
    original = build_hdr(values).histogram
    path = write_hdr(original, tmp_path / "raw" / "run_test_1.hdr")
    restored = read_hdr(path)
    assert _summary(restored) == _summary(original)
    assert restored.get_total_count() == len(values)


def test_values_are_microseconds_and_precision_is_three_digits() -> None:
    exact_ms = [i / 7 + 0.123 for i in range(1, 2001)]
    hist = build_hdr(exact_ms).histogram
    ordered = sorted(round(v * 1000) for v in exact_ms)
    for pct in (50.0, 90.0, 99.0):
        rank = max(1, math.ceil(pct / 100 * len(ordered)))
        exact_us = ordered[rank - 1]
        got_us = hist.get_value_at_percentile(pct)
        # 3 significant digits: relative error bounded by 1/1000 (plus rank slack).
        assert abs(got_us - exact_us) <= max(1, exact_us // 1000) * 2


def test_build_from_json_only_http_req_duration_points(tmp_path: Path) -> None:
    path = _write_ndjson(
        tmp_path / "in.json",
        [
            json.dumps({"type": "Metric", "metric": "http_req_duration", "data": {}}),
            _point(1.5),
            _point(2.25),
            _point(99.0, metric="http_req_waiting"),
            "",
            _point(10.0),
        ],
    )
    result = build_hdr_from_json(path)
    assert result.recorded == 3
    assert result.below_range == 0 and result.above_range == 0
    assert result.lines == 6
    assert result.blank_lines == 1
    assert result.other_lines == 2
    assert result.histogram.get_total_count() == 3
    assert result.histogram.get_min_value() == 1500
    assert result.histogram.values_are_equivalent(result.histogram.get_max_value(), 10000)


@pytest.mark.parametrize(
    "bad_line",
    [
        "{not json",
        "[1, 2, 3]",
        json.dumps({"type": "Point", "metric": "http_req_duration"}),
        json.dumps({"type": "Point", "metric": "http_req_duration", "data": {"value": "1.0"}}),
        json.dumps({"type": "Point", "metric": "http_req_duration", "data": {"value": True}}),
        '{"type": "Point", "metric": "http_req_duration", "data": {"value": NaN}}',
    ],
)
def test_malformed_lines_raise_with_line_number(tmp_path: Path, bad_line: str) -> None:
    path = _write_ndjson(tmp_path / "bad.json", [_point(1.0), bad_line, _point(2.0)])
    with pytest.raises(ValueError, match=r"bad\.json:2"):
        build_hdr_from_json(path)


def test_out_of_range_values_are_reported_not_clamped(tmp_path: Path) -> None:
    path = _write_ndjson(
        tmp_path / "range.json",
        [
            _point(0.0004),  # 0.4 us -> rounds to 0 us, below 1 us
            _point(-3.0),  # negative duration, below range
            _point(1.0),
            _point(60_000.0),  # exactly 60 s, in range
            _point(60_000.001),  # 60 s + 1 us, above range
            _point(120_000.0),  # 120 s, above range
        ],
    )
    result = build_hdr_from_json(path)
    assert result.below_range == 2
    assert result.above_range == 2
    assert result.recorded == 2
    assert result.histogram.get_total_count() == 2
    assert result.histogram.values_are_equivalent(result.histogram.get_max_value(), HIGHEST_US)
    assert LOWEST_US == 1


def test_in_memory_out_of_range_reported() -> None:
    result = build_hdr([0.0, 5.0, 60_001.0])
    assert result.recorded == 1
    assert result.below_range == 1
    assert result.above_range == 1


def test_read_hdr_round_trip_after_json_build(tmp_path: Path) -> None:
    path = _write_ndjson(tmp_path / "rt.json", [_point(float(i)) for i in range(1, 501)])
    built = build_hdr_from_json(path).histogram
    restored = read_hdr(write_hdr(built, tmp_path / "out" / "run_rt_1.hdr"))
    assert _summary(restored) == _summary(built)
