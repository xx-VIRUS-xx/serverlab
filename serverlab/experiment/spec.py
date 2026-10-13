"""Experiment spec models (Week 1 subset). Owned by the lead."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from serverlab.load.base import LoadSpec


class TargetSpec(BaseModel):
    image: str = Field(description="must be pinned by digest: name@sha256:...")
    env: dict[str, str] = Field(default_factory=dict)
    port: int = 8000
    health_path: str = "/healthz"


class CpuSpec(BaseModel):
    sut: list[int]
    load: list[int]


class RunSpec(BaseModel):
    name: str
    seed: int
    target: TargetSpec
    load: LoadSpec
    cpus: CpuSpec
    warmup_s: int = 5
    cooldown_s: int = 5
    keep_raw: bool = False


def load_spec(path: Path) -> RunSpec:
    return RunSpec.model_validate(yaml.safe_load(path.read_text()))
