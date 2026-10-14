"""k6 load adapter implementation. Open-loop only (plan.md s3.1)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from serverlab.load.base import LatencyMs, LoadSpec, RunResult

__all__ = [
    "K6Adapter",
    "check_validity",
    "find_k6_binary",
    "generate_k6_script",
    "parse_k6_summary",
]


def find_k6_binary(configured_path: str = "k6") -> str:
    """Locate k6 binary, checking PATH and common macOS / Linux install locations."""
    found = shutil.which(configured_path)
    if found:
        return found
    for fallback in [
        Path("/opt/homebrew/bin/k6"),
        Path("/usr/local/bin/k6"),
        Path.home() / ".cargo/bin/k6",
    ]:
        if fallback.is_file() and os.access(fallback, os.X_OK):
            return str(fallback)
    return configured_path


def generate_k6_script(spec: LoadSpec) -> str:
    """Generate a deterministic k6 script for constant-arrival-rate executor.

    Sorting headers and JSON serialization keys ensures equal specs produce
    byte-identical script output.
    """
    headers_json = json.dumps(spec.headers, sort_keys=True)
    body_json = json.dumps(spec.body)
    url_json = json.dumps(spec.url)
    method_json = json.dumps(spec.method)

    return f"""import http from 'k6/http';

export const options = {{
  summaryTrendStats: ['med', 'p(90)', 'p(99)', 'max'],
  scenarios: {{
    open_loop: {{
      executor: 'constant-arrival-rate',
      rate: {spec.rate},
      timeUnit: '1s',
      duration: '{spec.duration_s}s',
      preAllocatedVUs: {spec.pre_allocated_vus},
      maxVUs: {spec.max_vus},
    }},
  }},
}};

const url = {url_json};
const method = {method_json};
const params = {{
  headers: {headers_json},
}};
const body = {body_json};

export default function () {{
  http.request(method, url, body, params);
}}
"""


def check_validity(
    dropped: int,
    planned: int,
    peak_vus: int,
    max_vus: int | None,
) -> tuple[bool, list[str]]:
    """Check validity according to plan.md s3.2 and task A-1.

    Valid if dropped_iterations / planned <= 0.005 (0.5%) and peak VUs < max_vus.
    Returns (valid, invalid_reasons).
    """
    invalid_reasons: list[str] = []
    if planned > 0:
        dropped_ratio = dropped / planned
        if dropped_ratio > 0.005:
            invalid_reasons.append(
                f"dropped iterations ratio {dropped_ratio:.4f} ({dropped}/{planned}) "
                f"exceeded 0.005 (0.5%) threshold"
            )
    elif dropped > 0:
        invalid_reasons.append(
            f"dropped iterations count {dropped} with 0 planned iterations"
        )

    if max_vus is not None and peak_vus >= max_vus:
        invalid_reasons.append(
            f"peak VUs ({peak_vus}) reached or exceeded max_vus ({max_vus})"
        )

    return len(invalid_reasons) == 0, invalid_reasons


def parse_k6_summary(
    summary_data: dict[str, Any] | str | Path,
    *,
    spec: LoadSpec | None = None,
    planned: int | None = None,
    max_vus: int | None = None,
    run_id: str = "run",
    summary_path: Path | None = None,
) -> RunResult:
    """Parse k6 summary-export JSON into RunResult and evaluate run validity."""
    if isinstance(summary_data, Path):
        summary_path = summary_path or summary_data
        with open(summary_data, encoding="utf-8") as f:
            raw: dict[str, Any] = json.load(f)
    elif isinstance(summary_data, str):
        raw = json.loads(summary_data)
    else:
        raw = summary_data

    metrics: dict[str, Any] = raw.get("metrics", {})

    # Iterations
    iterations_metric = metrics.get("iterations", {})
    completed = int(iterations_metric.get("count", 0))

    dropped_metric = metrics.get("dropped_iterations", {})
    dropped = int(dropped_metric.get("count", 0)) if dropped_metric else 0

    effective_planned: int
    effective_max_vus: int | None
    if spec is not None:
        effective_planned = int(spec.rate * spec.duration_s)
        effective_max_vus = spec.max_vus
    else:
        effective_planned = int(planned) if planned is not None else (completed + dropped)
        effective_max_vus = max_vus

    # Peak VUs: active vs allocated
    vus_metric = metrics.get("vus", {})
    vus_max_metric = metrics.get("vus_max", {})
    peak_active = int(vus_metric.get("max", vus_metric.get("value", 0)))
    peak_allocated = int(vus_max_metric.get("max", vus_max_metric.get("value", 0)))
    if peak_active == 0 and completed > 0:
        peak_active = 1

    if spec is not None and spec.pre_allocated_vus >= spec.max_vus:
        peak_vus = peak_active
    else:
        peak_vus = max(peak_active, peak_allocated)

    # Latency percentiles in milliseconds
    dur = metrics.get("http_req_duration", {})
    p50 = float(dur.get("med", dur.get("p(50)", dur.get("p(50.0)", dur.get("avg", 0.0)))))
    p90 = float(dur.get("p(90)", dur.get("p(90.0)", 0.0)))
    p99 = float(dur.get("p(99)", dur.get("p(99.0)", dur.get("p(95)", dur.get("max", 0.0)))))
    max_val = float(dur.get("max", 0.0))
    latency_ms = LatencyMs(p50=p50, p90=p90, p99=p99, max=max_val)

    # Error rate
    failed_metric = metrics.get("http_req_failed", {})
    if "value" in failed_metric:
        error_rate = float(failed_metric["value"])
    else:
        passes = float(failed_metric.get("passes", 0.0))
        fails = float(failed_metric.get("fails", 0.0))
        error_rate = (passes / (passes + fails)) if (passes + fails) > 0 else 0.0

    valid, invalid_reasons = check_validity(
        dropped=dropped,
        planned=effective_planned,
        peak_vus=peak_vus,
        max_vus=effective_max_vus,
    )

    return RunResult(
        run_id=run_id,
        planned=effective_planned,
        completed=completed,
        dropped=dropped,
        peak_vus=peak_vus,
        latency_ms=latency_ms,
        error_rate=error_rate,
        valid=valid,
        invalid_reasons=invalid_reasons,
        summary_path=summary_path or Path(f"{run_id}.json"),
        hdr_path=None,
    )


class K6Adapter:
    """k6 adapter implementing the LoadAdapter protocol."""

    def __init__(self, k6_bin: str = "k6") -> None:
        self.k6_bin = find_k6_binary(k6_bin)

    def run(
        self,
        spec: LoadSpec,
        out_dir: Path,
        run_id: str,
        per_request_path: Path | None = None,
        summary_path: Path | None = None,
    ) -> RunResult:
        out_dir.mkdir(parents=True, exist_ok=True)
        script_path = out_dir / f"{run_id}.js"
        script_code = generate_k6_script(spec)
        script_path.write_text(script_code, encoding="utf-8")

        target_summary_path = summary_path or (out_dir / f"{run_id}.json")

        cmd = [
            self.k6_bin,
            "run",
            f"--summary-export={target_summary_path}",
            "--summary-trend-stats=med,p(90),p(99),max",
        ]

        if per_request_path is not None:
            per_request_path.parent.mkdir(parents=True, exist_ok=True)
            if str(per_request_path).endswith(".csv"):
                cmd.extend(["--out", f"csv={per_request_path}"])
            else:
                cmd.extend(["--out", f"json={per_request_path}"])

        cmd.append(str(script_path))

        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
        except Exception as exc:
            return RunResult(
                run_id=run_id,
                planned=int(spec.rate * spec.duration_s),
                completed=0,
                dropped=0,
                peak_vus=0,
                latency_ms=LatencyMs(p50=0.0, p90=0.0, p99=0.0, max=0.0),
                error_rate=1.0,
                valid=False,
                invalid_reasons=[f"k6 execution failed to start: {exc}"],
                summary_path=target_summary_path,
                hdr_path=None,
            )

        if not target_summary_path.is_file():
            stderr_preview = proc.stderr.strip()[:300]
            err_msg = (
                f"k6 exited with code {proc.returncode} but produced no summary: {stderr_preview}"
            )
            return RunResult(
                run_id=run_id,
                planned=int(spec.rate * spec.duration_s),
                completed=0,
                dropped=0,
                peak_vus=0,
                latency_ms=LatencyMs(p50=0.0, p90=0.0, p99=0.0, max=0.0),
                error_rate=1.0,
                valid=False,
                invalid_reasons=[err_msg],
                summary_path=target_summary_path,
                hdr_path=None,
            )

        result = parse_k6_summary(
            target_summary_path,
            spec=spec,
            run_id=run_id,
            summary_path=target_summary_path,
        )

        if proc.returncode != 0:
            result.valid = False
            result.invalid_reasons.append(
                f"k6 process exited with non-zero return code {proc.returncode}"
            )

        return result
