"""Grade the pool experiment against docs/predictions.md (Week 4, amendments 1 and 2).

Rules as committed (not retuned): pool-limited sizes pass if the median saturated throughput
is within 15% of n / 0.050; sizes whose C_pool lies inside the API-ceiling interval are
"ambiguous by construction" and not graded; sizes above the interval are predicted API-limited
and pass if the pool never saturated and the SUT CPU reached >= 90% where latency inflated.
"""

import json
import statistics
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel

from serverlab.analysis.capacity import (
    CPU_US_PER_S_PER_CPU,
    UtilizationFit,
    fit_utilization_law,
    pool_capacity,
    read_jsonl,
    saturated_throughputs,
)

SERVICE_S = 0.050
TOLERANCE = 0.15
API_CPU_MIN_PCT = 90.0
LOW_CPU_PCT = 85.0

Regime = Literal["pool-limited", "ambiguous", "api-limited"]


class SweepCapacity(BaseModel):
    repetition: int
    c_meas: float | None
    saturated_intervals: int
    p99_knee_estimate: float | None  # secondary, ungraded
    # secondary, ungraded: plateau over steps where the SUT container CPU median was < 85%
    c_meas_low_cpu: float | None = None
    low_cpu_intervals: int = 0
    pool_ever_saturated: bool
    # median SUT CPU over the steps where latency inflated
    saturated_steps_sut_cpu_pct: float | None


class SizeVerdict(BaseModel):
    pool_size: int
    c_pool: float
    regime: Regime
    sweeps: list[SweepCapacity]
    c_meas_median: float | None
    c_meas_min: float | None
    c_meas_max: float | None
    error_pct: float | None
    passed: bool | None  # None = not graded
    note: str


def c_pool(pool_size: int, service_s: float = SERVICE_S) -> float:
    return pool_size / service_s


def regime_for(pool_size: int, a_lo: float, a_hi: float) -> Regime:
    cp = c_pool(pool_size)
    if cp < a_lo:
        return "pool-limited"
    if cp > a_hi:
        return "api-limited"
    return "ambiguous"


def grade_size(
    pool_size: int, sweeps: list[SweepCapacity], a_lo: float, a_hi: float
) -> SizeVerdict:
    cp = c_pool(pool_size)
    regime = regime_for(pool_size, a_lo, a_hi)
    vals = [s.c_meas for s in sweeps if s.c_meas is not None]
    med = statistics.median(vals) if vals else None
    err = None if med is None else 100.0 * (med - cp) / cp
    passed: bool | None
    if regime == "pool-limited":
        passed = med is not None and abs(med - cp) <= TOLERANCE * cp
        note = (
            "no saturated intervals observed: the pool never saturated in these sweeps"
            if med is None else f"median plateau {med:.0f} vs C_pool {cp:.0f}"
        )
    elif regime == "ambiguous":
        passed = None
        note = f"C_pool {cp:.0f} lies inside the API ceiling interval [{a_lo:.0f}, {a_hi:.0f}]"
    else:
        cpu = [
            s.saturated_steps_sut_cpu_pct for s in sweeps
            if s.saturated_steps_sut_cpu_pct is not None
        ]
        never = not any(s.pool_ever_saturated for s in sweeps)
        passed = never and bool(cpu) and statistics.median(cpu) >= API_CPU_MIN_PCT
        note = (
            f"pool saturated in a sweep: {not never}; median SUT CPU over saturated steps "
            f"{statistics.median(cpu):.0f}%" if cpu else "no SUT CPU data"
        )
    return SizeVerdict(
        pool_size=pool_size, c_pool=cp, regime=regime, sweeps=sweeps, c_meas_median=med,
        c_meas_min=min(vals) if vals else None, c_meas_max=max(vals) if vals else None,
        error_pct=err, passed=passed, note=note,
    )


def _spec(results_dir: Path) -> dict[str, Any]:
    return yaml.safe_load((results_dir / "spec.yaml").read_text())  # type: ignore[no-any-return]


def utilization_fit_for(results_dir: Path, max_rate: int = 975, seed: int = 1) -> UtilizationFit:
    """Fit over every valid step with offered rate <= max_rate in a sweep results directory."""
    spec = _spec(results_dir)
    points: list[tuple[float, float]] = []
    for path in sorted((results_dir / "raw").glob("run_*.json")):
        if path.name.endswith((".k6summary.json", ".warmup.json", ".error.json")):
            continue
        rec = json.loads(path.read_text())
        res = rec["result"]
        wall = res.get("k6_wall_s")
        if not res["valid"] or rec["offered_rate"] > max_rate or not wall:
            continue
        if rec.get("sut_cpu_usec") is None or res["completed"] == 0:
            continue
        points.append((res["completed"] / wall, rec["sut_cpu_usec"] / wall))
    capacity = len(spec["cpus"]["sut"]) * CPU_US_PER_S_PER_CPU
    return fit_utilization_law(points, capacity, seed=seed)


def _pool_size(spec: dict[str, Any], condition: str) -> int:
    cond = next(c for c in spec["conditions"] if c["name"] == condition)
    env = {**spec["target"]["env"], **cond.get("target_env", {})}
    return int(env["POOL_SIZE"])


def sweep_capacities(results_dir: Path) -> dict[int, list[SweepCapacity]]:
    """Per pool size, one SweepCapacity per sweep repetition."""
    spec = _spec(results_dir)
    out: dict[int, list[SweepCapacity]] = {}
    for path in sorted(results_dir.glob("sweep_*.json")):
        cs = json.loads(path.read_text())
        size = _pool_size(spec, cs["condition"])
        sat: list[float] = []
        low_cpu_lines: list[dict[str, Any]] = []
        ever = False
        sat_cpu: list[float] = []
        sat_rates = {
            s["measurement"]["rate"] for s in cs["sweep"]["steps"] if s["status"] == "saturated"
        }
        for rec in cs["records"]:
            run_id = rec["result"]["run_id"]
            app = read_jsonl(results_dir / "metrics" / f"{run_id}.app.jsonl")
            sat += saturated_throughputs(app)
            dk = [d["cpu_percent"] for d in read_jsonl(results_dir / "metrics" / f"{run_id}.jsonl")
                  if d["container"].endswith("-sut-1") and d.get("cpu_percent") is not None]
            if dk and statistics.median(dk) < LOW_CPU_PCT:
                low_cpu_lines += app
            if rec["result"]["valid"] and saturated_throughputs(app):
                ever = True
            if rec["offered_rate"] in sat_rates:
                docker = [d for d in read_jsonl(results_dir / "metrics" / f"{run_id}.jsonl")
                          if d["container"].endswith("-sut-1")]
                cpu = [d["cpu_percent"] for d in docker if d.get("cpu_percent") is not None]
                if cpu:
                    sat_cpu.append(statistics.median(cpu))
        all_app = [
            line
            for rec in cs["records"]
            for line in read_jsonl(
                results_dir / "metrics" / f"{rec['result']['run_id']}.app.jsonl"
            )
        ]
        c_meas, n_int = pool_capacity(all_app) if all_app else (None, 0)
        c_low, n_low = pool_capacity(low_cpu_lines) if low_cpu_lines else (None, 0)
        out.setdefault(size, []).append(SweepCapacity(
            repetition=cs["repetition"], c_meas=c_meas, saturated_intervals=n_int,
            c_meas_low_cpu=c_low, low_cpu_intervals=n_low,
            p99_knee_estimate=cs["sweep"]["knee"].get("estimate"),
            pool_ever_saturated=ever,
            saturated_steps_sut_cpu_pct=statistics.median(sat_cpu) if sat_cpu else None,
        ))
    return out
