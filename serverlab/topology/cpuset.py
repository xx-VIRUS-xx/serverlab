"""Disjoint CPU set assignment for SUT, load generator and extra services (plan.md s3.3).

Pure, no Docker. `extra` holds named sets for additional SUT-side services such as a database.
"""

from collections.abc import Iterable, Mapping

from pydantic import BaseModel, model_validator


class CpuAssignment(BaseModel):
    """Explicit CPU ids for the SUT and the load generator on a host with `total` cpus.

    Every CPU on the host is either in `sut`, in `load`, or listed in `unassigned`.
    Nothing is reserved implicitly.
    """

    model_config = {"frozen": True}

    total: int
    sut: tuple[int, ...]
    load: tuple[int, ...]
    extra: dict[str, tuple[int, ...]] = {}

    @model_validator(mode="after")
    def _check_disjoint_in_range(self) -> "CpuAssignment":
        if self.total <= 0:
            raise ValueError(f"total_cpus must be positive, got {self.total}")
        if not self.sut:
            raise ValueError("sut cpuset must not be empty")
        if not self.load:
            raise ValueError("load cpuset must not be empty")
        named: list[tuple[str, tuple[int, ...]]] = [
            ("sut", self.sut), ("load", self.load), *self.extra.items()
        ]
        for label, cpus in named:
            if not cpus:
                raise ValueError(f"{label} cpuset must not be empty")
            if len(set(cpus)) != len(cpus):
                raise ValueError(f"{label} cpuset contains duplicate cpu ids: {list(cpus)}")
            bad = [c for c in cpus if c < 0 or c >= self.total]
            if bad:
                raise ValueError(
                    f"{label} cpu ids {bad} out of range for host with {self.total} cpus "
                    f"(valid: 0..{self.total - 1})"
                )
        for i, (a_name, a_cpus) in enumerate(named):
            for b_name, b_cpus in named[i + 1 :]:
                overlap = sorted(set(a_cpus) & set(b_cpus))
                if overlap:
                    raise ValueError(f"{a_name} and {b_name} cpusets overlap on cpu ids {overlap}")
        return self

    @property
    def unassigned(self) -> tuple[int, ...]:
        used = set(self.sut) | set(self.load) | {c for s in self.extra.values() for c in s}
        return tuple(c for c in range(self.total) if c not in used)

    def sut_cpuset(self) -> str:
        return _format_cpuset(self.sut)

    def load_cpuset(self) -> str:
        return _format_cpuset(self.load)

    def extra_cpuset(self, name: str) -> str:
        return _format_cpuset(self.extra[name])


def _format_cpuset(cpus: Iterable[int]) -> str:
    return ",".join(str(c) for c in sorted(cpus))


def assign_cpusets(
    total_cpus: int,
    sut_cpus: list[int],
    load_cpus: list[int],
    extra: Mapping[str, list[int]] | None = None,
) -> CpuAssignment:
    """Validate explicit SUT and load cpu id lists against a host with `total_cpus` cpus.

    Raises ValueError (pydantic ValidationError, a ValueError subclass) on overlap,
    out-of-range or duplicate ids, empty sets, or oversubscription.
    """
    return CpuAssignment(
        total=total_cpus, sut=tuple(sut_cpus), load=tuple(load_cpus),
        extra={k: tuple(v) for k, v in (extra or {}).items()},
    )
