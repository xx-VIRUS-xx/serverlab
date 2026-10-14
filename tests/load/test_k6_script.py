"""Tests for k6 script generation (open-loop constant-arrival-rate)."""

import shutil
import subprocess
from pathlib import Path

from serverlab.load.base import LoadAdapter, LoadSpec
from serverlab.load.k6 import K6Adapter, find_k6_binary, generate_k6_script

SNAPSHOT_GET = """import http from 'k6/http';

export const options = {
  summaryTrendStats: ['med', 'p(90)', 'p(99)', 'max'],
  scenarios: {
    open_loop: {
      executor: 'constant-arrival-rate',
      rate: 10,
      timeUnit: '1s',
      duration: '5s',
      preAllocatedVUs: 2,
      maxVUs: 8,
    },
  },
};

const url = "http://sut:8000/work";
const method = "GET";
const params = {
  headers: {"Accept": "application/json", "User-Agent": "serverlab"},
};
const body = null;

export default function () {
  http.request(method, url, body, params);
}
"""


def test_generated_script_matches_snapshot() -> None:
    spec = LoadSpec(
        rate=10,
        duration_s=5,
        pre_allocated_vus=2,
        max_vus=8,
        url="http://sut:8000/work",
        method="GET",
        headers={"Accept": "application/json", "User-Agent": "serverlab"},
        body=None,
    )
    script = generate_k6_script(spec)
    assert script == SNAPSHOT_GET


def test_script_generation_is_deterministic() -> None:
    # Key order in headers must not affect generated script
    spec1 = LoadSpec(
        rate=50,
        duration_s=10,
        pre_allocated_vus=5,
        max_vus=20,
        url="http://sut:8000/api",
        method="POST",
        headers={"Z-Header": "z", "A-Header": "a", "M-Header": "m"},
        body='{"payload": 42}',
    )
    spec2 = LoadSpec(
        rate=50,
        duration_s=10,
        pre_allocated_vus=5,
        max_vus=20,
        url="http://sut:8000/api",
        method="POST",
        headers={"A-Header": "a", "M-Header": "m", "Z-Header": "z"},
        body='{"payload": 42}',
    )
    script1 = generate_k6_script(spec1)
    script2 = generate_k6_script(spec2)
    assert script1 == script2


def test_script_contains_open_loop_executor_and_params() -> None:
    spec = LoadSpec(
        rate=100,
        duration_s=30,
        pre_allocated_vus=10,
        max_vus=50,
        url="http://sut:8000/items",
        method="PUT",
        headers={"Content-Type": "application/json"},
        body='{"key": "val"}',
    )
    script = generate_k6_script(spec)
    assert "executor: 'constant-arrival-rate'" in script
    assert "rate: 100" in script
    assert "timeUnit: '1s'" in script
    assert "duration: '30s'" in script
    assert "preAllocatedVUs: 10" in script
    assert "maxVUs: 50" in script
    assert 'const method = "PUT";' in script
    assert 'const url = "http://sut:8000/items";' in script


def test_adapter_satisfies_protocol() -> None:
    adapter: LoadAdapter = K6Adapter()
    assert callable(adapter.run)


def test_k6_inspect_validates_generated_script(tmp_path: Path) -> None:
    k6_bin = find_k6_binary()
    if not shutil.which(k6_bin):
        return

    spec = LoadSpec(
        rate=5,
        duration_s=2,
        pre_allocated_vus=1,
        max_vus=5,
        url="http://127.0.0.1:8000/healthz",
        method="GET",
    )
    script_path = tmp_path / "test.js"
    script_path.write_text(generate_k6_script(spec), encoding="utf-8")

    proc = subprocess.run([k6_bin, "inspect", str(script_path)], capture_output=True, text=True)
    assert proc.returncode == 0, f"k6 inspect failed: {proc.stderr}"
