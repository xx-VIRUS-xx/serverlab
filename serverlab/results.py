"""Read-only access to results directories (plan.md s5). Shared by the SDK and the web UI.

Nothing here writes. Every path is resolved inside the results root; names that are not plain
file names are rejected, so a caller cannot read outside it.
"""

import json
import re
from pathlib import Path
from typing import Any

import yaml

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_METRIC_LINES = 2000
_SKIP_SUFFIXES = (".k6summary.json", ".warmup.json", ".error.json", ".requests.json")
METRIC_SUFFIX = {"docker": "", "app": ".app", "pg": ".pg"}


class ResultsError(ValueError):
    """Bad name or path (HTTP 400/404 in the UI)."""


def check_name(name: str) -> str:
    if not NAME_RE.match(name) or name in {".", ".."}:
        raise ResultsError(f"invalid name {name!r}")
    return name


def result_dir(root: Path, experiment: str, timestamp: str) -> Path:
    path = (root / check_name(experiment) / check_name(timestamp)).resolve()
    if root.resolve() not in path.parents or not path.is_dir():
        raise ResultsError(f"no such result {experiment}/{timestamp}")
    return path


def _json(path: Path) -> Any:
    return json.loads(path.read_text())


def _run_files(d: Path) -> list[Path]:
    raw = d / "raw"
    if not raw.is_dir():
        return []
    return sorted(
        p for p in raw.glob("run_*.json") if not p.name.endswith(_SKIP_SUFFIXES)
    )


def list_results(root: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not root.is_dir():
        return out
    for exp in sorted(p for p in root.iterdir() if p.is_dir() and NAME_RE.match(p.name)):
        for d in sorted(p for p in exp.iterdir() if p.is_dir() and NAME_RE.match(p.name)):
            if not (d / "environment.json").is_file():
                continue
            env = _json(d / "environment.json")
            runs = [_json(f) for f in _run_files(d)]
            out.append({
                "experiment": exp.name, "timestamp": d.name,
                "kind": "sweep" if any(d.glob("sweep_*.json")) else "run",
                "comparability": env.get("comparability"),
                "warnings": env.get("warnings", []),
                "runs": len(runs),
                "invalid": sum(not r["result"]["valid"] for r in runs),
                "errors": len(list((d / "raw").glob("*.error.json")))
                if (d / "raw").is_dir() else 0,
                "sweeps": len(list(d.glob("sweep_*.json"))),
            })
    out.sort(key=lambda r: r["timestamp"], reverse=True)
    return out


_ENV_KEYS = (
    "os_name", "os_version", "cpu_model", "cpu_cores", "docker_vm_cpus", "host_memory_gib",
    "memory_free_pct", "swap_used_mib", "power_source", "low_power_mode",
    "other_running_containers", "comparability", "cpu_pinning_reliable", "warnings", "reasons",
)


def _run_row(
    rec: dict[str, Any], metrics_dir: Path, default_rate: int | None = None
) -> dict[str, Any]:
    """One run. Results written by earlier versions lack some fields; those become None
    (shown as n/a), never a made-up value. `offered_rate` falls back to the spec's load rate
    only for single-rate experiments, where it is the same for every run."""
    res = rec["result"]
    rid = res["run_id"]
    lat = res.get("latency_ms") or {}
    return {
        "run_id": rid, "condition": rec.get("condition"), "repetition": rec.get("repetition"),
        "index": rec.get("index"),
        "offered_rate": rec["offered_rate"] if rec.get("offered_rate") is not None
        else default_rate,
        "valid": res["valid"],
        "invalid_reasons": res.get("invalid_reasons", []), "planned": res.get("planned"),
        "completed": res.get("completed"), "dropped": res.get("dropped"),
        "peak_vus": res.get("peak_vus"),
        "p50_ms": lat.get("p50"), "p90_ms": lat.get("p90"),
        "p99_ms": lat.get("p99"), "error_rate": res.get("error_rate"),
        "sut_cpu_usec": rec.get("sut_cpu_usec"), "k6_cpu_usec": res.get("k6_cpu_usec"),
        "metrics": [k for k, suf in METRIC_SUFFIX.items()
                    if (metrics_dir / f"{rid}{suf}.jsonl").is_file()],
    }


def _sweep_row(data: dict[str, Any]) -> dict[str, Any]:
    sweep = data["sweep"]
    knee = sweep["knee"]
    steps = []
    for s in sweep["steps"]:
        m = s["measurement"]
        steps.append({
            "rate": m["rate"], "status": s["status"], "note": s["note"],
            "achieved": m["achieved"], "p99_ms": m["p99_ms"], "k6_cpu_pct": m["k6_cpu_pct"],
            "error_rate": m.get("error_rate"), "valid": m["valid"],
            "invalid_reasons": m["invalid_reasons"],
        })
    return {
        "condition": data["condition"], "repetition": data.get("repetition"),
        "seed": data.get("seed"),
        "coarse_order": sweep["coarse_order"], "bisect_rates": sweep["bisect_rates"],
        "steps": steps,
        "knee": {k: knee.get(k) for k in
                 ("status", "lo", "hi", "estimate", "width_pct", "baseline_p99_ms", "warnings")},
    }


def result_summary(root: Path, experiment: str, timestamp: str) -> dict[str, Any]:
    d = result_dir(root, experiment, timestamp)
    spec = yaml.safe_load((d / "spec.yaml").read_text())
    env = _json(d / "environment.json")
    sched = _json(d / "schedule.json") if (d / "schedule.json").is_file() else None
    sweeps = sorted(d.glob("sweep_*.json"))
    errors = []
    if (d / "raw").is_dir():
        for p in sorted((d / "raw").glob("*.error.json")):
            e = _json(p)
            errors.append({"run_id": e["run_id"], "message": e["message"]})
    return {
        "experiment": experiment, "timestamp": timestamp,
        "kind": "sweep" if sweeps else "run",
        "spec": {
            "name": spec.get("name"), "seed": spec.get("seed"),
            "repetitions": spec.get("repetitions"),
            "conditions": [
                {"name": c["name"], "target_env": c.get("target_env", {}),
                 "load_rate": c.get("load_rate")} for c in spec.get("conditions", [])
            ],
            "load": {"rate": spec.get("load", {}).get("rate"),
                     "duration_s": spec.get("load", {}).get("duration_s")},
            "cpus": spec.get("cpus"),
        },
        "environment": {k: env.get(k) for k in _ENV_KEYS},
        "schedule": None if sched is None else {
            "method": sched.get("method"), "seed": sched.get("seed"),
            "order": [{"index": r.get("index"), "condition": r.get("condition"),
                       "repetition": r.get("repetition")} for r in sched.get("runs", [])],
        },
        "runs": [
            _run_row(_json(f), d / "metrics", spec.get("load", {}).get("rate"))
            for f in _run_files(d)
        ],
        "errors": errors,
        "sweeps": [_sweep_row(_json(p)) for p in sweeps],
    }


def read_metrics(
    root: Path, experiment: str, timestamp: str, run_id: str, kind: str
) -> dict[str, Any]:
    d = result_dir(root, experiment, timestamp)
    check_name(run_id)
    if kind not in METRIC_SUFFIX:
        raise ResultsError(f"unknown metrics kind {kind!r}")
    path = d / "metrics" / f"{run_id}{METRIC_SUFFIX[kind]}.jsonl"
    if not path.is_file():
        raise FileNotFoundError(str(path.name))
    lines = [json.loads(x) for x in path.read_text().splitlines() if x.strip()]
    return {
        "kind": kind, "lines": lines[:MAX_METRIC_LINES], "truncated": len(lines) > MAX_METRIC_LINES
    }
