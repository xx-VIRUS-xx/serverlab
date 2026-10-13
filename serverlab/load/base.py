"""Load-generation interface. Open-loop only (plan.md s3.1). Owned by the lead."""

from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, Field


class LoadSpec(BaseModel):
    """One constant-arrival-rate step."""

    rate: int = Field(gt=0, description="arrival rate, requests/s")
    duration_s: int = Field(gt=0)
    pre_allocated_vus: int = Field(gt=0)
    max_vus: int = Field(gt=0)
    url: str
    method: Literal["GET", "POST", "PUT", "DELETE"] = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: str | None = None


class LatencyMs(BaseModel):
    p50: float
    p90: float
    p99: float
    max: float


class RunResult(BaseModel):
    run_id: str
    planned: int
    completed: int
    dropped: int
    peak_vus: int
    latency_ms: LatencyMs
    error_rate: float
    valid: bool
    invalid_reasons: list[str] = Field(default_factory=list)
    summary_path: Path
    hdr_path: Path | None = None


class LoadAdapter(Protocol):
    """Adapter contract. `run` writes raw artefacts under `out_dir` and never raises
    for an invalid run: it returns valid=False with specific reasons."""

    def run(self, spec: LoadSpec, out_dir: Path, run_id: str) -> RunResult: ...
