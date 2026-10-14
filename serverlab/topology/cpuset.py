"""Disjoint CPU set assignment for SUT and load generator (plan.md s3.3). Pure, no Docker."""

from collections.abc import Iterable

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

    @model_validator(mode="after")
    def _check_disjoint_in_range(self) -> "CpuAssignment":
        if self.total <= 0:
            raise ValueError(f"total_cpus must be positive, got {self.total}")
        if not self.sut:
            raise ValueError("sut cpuset must not be empty")
        if not self.load:
            raise ValueError("load cpuset must not be empty")
        for label, cpus in (("sut", self.sut), ("load", self.load)):
            if len(set(cpus)) != len(cpus):
                raise ValueError(f"{label} cpuset contains duplicate cpu ids: {list(cpus)}")
            bad = [c for c in cpus if c < 0 or c >= self.total]
            if bad:
                raise ValueError(
                    f"{label} cpu ids {bad} out of range for host with {self.total} cpus "
                    f"(valid: 0..{self.total - 1})"
                )
        overlap = sorted(set(self.sut) & set(self.load))
        if overlap:
            raise ValueError(f"sut and load cpusets overlap on cpu ids {overlap}")
        return self

    @property
    def unassigned(self) -> tuple[int, ...]:
        used = set(self.sut) | set(self.load)
        return tuple(c for c in range(self.total) if c not in used)

    def sut_cpuset(self) -> str:
        return _format_cpuset(self.sut)

    def load_cpuset(self) -> str:
        return _format_cpuset(self.load)


def _format_cpuset(cpus: Iterable[int]) -> str:
    return ",".join(str(c) for c in sorted(cpus))


def assign_cpusets(total_cpus: int, sut_cpus: list[int], load_cpus: list[int]) -> CpuAssignment:
    """Validate explicit SUT and load cpu id lists against a host with `total_cpus` cpus.

    Raises ValueError (pydantic ValidationError, a ValueError subclass) on overlap,
    out-of-range or duplicate ids, empty sets, or oversubscription.
    """
    return CpuAssignment(total=total_cpus, sut=tuple(sut_cpus), load=tuple(load_cpus))
