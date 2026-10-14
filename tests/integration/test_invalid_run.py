"""Week 1 acceptance: starved VUs make a k6 run invalid; ample VUs stay valid.

Runs real k6 (open-loop constant-arrival-rate) against the synthetic target,
started as a subprocess for the module. Capacity is
CONCURRENCY_LIMIT / (SERVICE_TIME_MS / 1000) = 10 / 0.1 = 100 req/s.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

from serverlab.load.base import LoadSpec, RunResult
from serverlab.load.k6 import K6Adapter, find_k6_binary

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNTHETIC_APP = REPO_ROOT / "targets" / "synthetic" / "app.py"

CONCURRENCY_LIMIT = 10
SERVICE_TIME_MS = 100
STARTUP_TIMEOUT_S = 15.0


def _k6_available() -> bool:
    return shutil.which("k6") is not None and shutil.which(find_k6_binary()) is not None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not _k6_available(),
        reason="k6 is not installed (not on PATH or in common install locations); "
        "install k6 to run the invalid-run integration tests",
    ),
]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def synthetic_base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Start the synthetic target on a free localhost port; stop it at teardown."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = tmp_path_factory.mktemp("synthetic") / "target.log"

    env = dict(os.environ)
    env.update(
        {
            "CONCURRENCY_LIMIT": str(CONCURRENCY_LIMIT),
            "SERVICE_TIME_MS": str(SERVICE_TIME_MS),
            "HOST": "127.0.0.1",
            "PORT": str(port),
        }
    )

    with log_path.open("w", encoding="utf-8") as log:
        proc = subprocess.Popen(
            [sys.executable, str(SYNTHETIC_APP)],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + STARTUP_TIMEOUT_S
            while True:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"synthetic target exited early with code {proc.returncode}: "
                        f"{log_path.read_text(encoding='utf-8')[-500:]}"
                    )
                try:
                    with urllib.request.urlopen(f"{base_url}/healthz", timeout=1) as resp:
                        if resp.status == 200:
                            break
                except (urllib.error.URLError, OSError):
                    pass
                if time.monotonic() > deadline:
                    raise RuntimeError(
                        f"synthetic target at {base_url} did not become healthy "
                        f"within {STARTUP_TIMEOUT_S}s"
                    )
                time.sleep(0.1)
            yield base_url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _run_k6(spec: LoadSpec, out_dir: Path, run_id: str) -> RunResult:
    adapter = K6Adapter()
    return adapter.run(spec, out_dir, run_id)


@pytest.mark.integration
def test_starved_vus_mark_run_invalid(
    synthetic_base_url: str,
    tmp_path: Path,
) -> None:
    """rate=150 against ~100 req/s capacity with max_vus=5 must be flagged invalid."""
    spec = LoadSpec(
        rate=150,
        duration_s=8,
        pre_allocated_vus=2,
        max_vus=5,
        url=f"{synthetic_base_url}/work",
    )
    result = _run_k6(spec, tmp_path, "starved")

    assert result.valid is False, f"starved run unexpectedly valid: {result}"
    assert result.invalid_reasons, "starved run has no invalid_reasons"
    assert any("dropped iterations" in reason for reason in result.invalid_reasons), (
        f"no reason mentions dropped iterations: {result.invalid_reasons}"
    )
    assert any("max_vus" in reason for reason in result.invalid_reasons), (
        f"no reason mentions max_vus: {result.invalid_reasons}"
    )


@pytest.mark.integration
def test_ample_vus_control_run_is_valid(
    synthetic_base_url: str,
    tmp_path: Path,
) -> None:
    """rate=50 (half of capacity) with ample VUs must be valid with no reasons."""
    spec = LoadSpec(
        rate=50,
        duration_s=8,
        pre_allocated_vus=10,
        max_vus=100,
        url=f"{synthetic_base_url}/work",
    )
    result = _run_k6(spec, tmp_path, "control")

    assert result.valid is True, f"control run invalid: {result.invalid_reasons}"
    assert result.invalid_reasons == []
