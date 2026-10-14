"""Experiment spec models (Week 1 subset). Owned by the lead."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from serverlab.load.base import LoadSpec


class TargetSpec(BaseModel):
    image: str = Field(
        description="pinned image: name@sha256:... or local id sha256:...; "
        "when build_context is set it is only a label and the built image id is used"
    )
    build_context: str | None = Field(
        default=None, description="docker build context (relative to the spec file's cwd)"
    )
    dockerfile: str | None = None
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
    k6_image: str = Field(description="pinned k6 image: name@sha256:...")
    load: LoadSpec
    cpus: CpuSpec
    warmup_s: int = 5
    cooldown_s: int = 5
    keep_raw: bool = False


def load_spec(path: Path) -> RunSpec:
    return RunSpec.model_validate(yaml.safe_load(path.read_text()))
