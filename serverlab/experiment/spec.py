"""Experiment spec models (Week 1 subset). Owned by the lead."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator

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


class MetricsSpec(BaseModel):
    enabled: bool = True
    interval_s: float = Field(default=1.0, ge=1.0, description="sampling interval, >= 1 s")


class ConditionSpec(BaseModel):
    """One arm of the experiment. Only the listed overrides differ between conditions."""

    name: str = Field(pattern=r"^[A-Za-z0-9-]+$", description="no underscores: used in run ids")
    target_env: dict[str, str] = Field(default_factory=dict)
    load_rate: int | None = Field(default=None, gt=0)
    metrics_enabled: bool | None = Field(
        default=None, description="override metrics.enabled for this condition only"
    )


class ExperimentSpec(BaseModel):
    name: str
    seed: int
    repetitions: int = Field(default=5, ge=1)
    target: TargetSpec
    k6_image: str
    load: LoadSpec
    cpus: CpuSpec
    warmup_s: int = 5
    cooldown_s: int = 5
    keep_raw: bool = False
    metrics: MetricsSpec = Field(default_factory=MetricsSpec)
    conditions: list[ConditionSpec] = Field(min_length=1)

    @field_validator("conditions")
    @classmethod
    def _unique_names(cls, v: list[ConditionSpec]) -> list[ConditionSpec]:
        names = [c.name for c in v]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"duplicate condition names: {dupes}")
        return v

    def collector_enabled(self, condition: str) -> bool:
        cond = next(c for c in self.conditions if c.name == condition)
        return self.metrics.enabled if cond.metrics_enabled is None else cond.metrics_enabled

    @property
    def condition_names(self) -> list[str]:
        return [c.name for c in self.conditions]

    def resolve(self, condition: str) -> RunSpec:
        """The single-run spec for one condition (overrides applied over the base)."""
        cond = next((c for c in self.conditions if c.name == condition), None)
        if cond is None:
            raise KeyError(f"unknown condition {condition!r}")
        load = self.load if cond.load_rate is None else self.load.model_copy(
            update={"rate": cond.load_rate}
        )
        target = self.target.model_copy(update={"env": {**self.target.env, **cond.target_env}})
        return RunSpec(
            name=self.name, seed=self.seed, target=target, k6_image=self.k6_image, load=load,
            cpus=self.cpus, warmup_s=self.warmup_s, cooldown_s=self.cooldown_s,
            keep_raw=self.keep_raw,
        )


def load_experiment(path: Path) -> ExperimentSpec:
    return ExperimentSpec.model_validate(yaml.safe_load(path.read_text()))


def load_spec(path: Path) -> RunSpec:
    return RunSpec.model_validate(yaml.safe_load(path.read_text()))
