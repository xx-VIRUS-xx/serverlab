"""Experiment spec models (Week 1 subset). Owned by the lead."""

from pathlib import Path

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from serverlab.load.base import LoadSpec


class ServiceSpec(BaseModel):
    """An extra container on the SUT side (e.g. Postgres). Image must be pinned; CPU set comes
    from `cpus.services[name]`. The service name is its DNS name on the compose network."""

    name: str = Field(pattern=r"^[a-z][a-z0-9-]*$")
    image: str = Field(description="pinned image: name@sha256:... or local id sha256:...")
    env: dict[str, str] = Field(default_factory=dict)
    command: list[str] | None = None
    tmpfs: list[str] = Field(default_factory=list, description="in-memory mounts, clean per run")
    healthcheck: list[str] | None = Field(
        default=None, description="compose healthcheck `test`, e.g. ['CMD-SHELL', 'pg_isready']"
    )
    container_port: int | None = None
    publish: int | None = Field(
        default=None, description="host port (127.0.0.1) mapped to container_port, for collectors"
    )

    @model_validator(mode="after")
    def _publish_needs_port(self) -> "ServiceSpec":
        if self.publish is not None and self.container_port is None:
            raise ValueError(f"service {self.name}: publish requires container_port")
        return self


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
    publish: int | None = Field(
        default=None, description="host port (127.0.0.1) mapped to `port`, for app-metrics scraping"
    )
    services: list[ServiceSpec] = Field(default_factory=list)

    @field_validator("services")
    @classmethod
    def _unique_service_names(cls, v: list[ServiceSpec]) -> list[ServiceSpec]:
        names = [s.name for s in v]
        reserved = {"sut", "k6"} & set(names)
        if reserved or len(set(names)) != len(names):
            raise ValueError(f"service names must be unique and not sut/k6: {names}")
        return v


class CpuSpec(BaseModel):
    sut: list[int]
    load: list[int]
    services: dict[str, list[int]] = Field(default_factory=dict)


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


class SweepConfig(BaseModel):
    """Rate sweep per condition: coarse log-spaced stage, then bracketed bisection."""

    rate_lo: int = Field(ge=1)
    rate_hi: int = Field(ge=2)
    step_duration_s: int = Field(default=10, ge=3)
    step_warmup_s: int = Field(default=2, ge=0)
    coarse_points: int = Field(default=8, ge=3)
    max_bisect: int = Field(default=6, ge=0)
    width_target_pct: float = Field(default=5.0, gt=0)

    @model_validator(mode="after")
    def _ordered(self) -> "SweepConfig":
        if self.rate_hi <= self.rate_lo:
            raise ValueError(f"rate_hi ({self.rate_hi}) must exceed rate_lo ({self.rate_lo})")
        return self


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
    sweep: SweepConfig | None = None
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

    @model_validator(mode="after")
    def _service_cpusets(self) -> "ExperimentSpec":
        names = {s.name for s in self.target.services}
        if names != set(self.cpus.services):
            raise ValueError(
                f"cpus.services {sorted(self.cpus.services)} must match target.services "
                f"{sorted(names)}"
            )
        return self

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
