"""Environment capture: host, Docker and runtime metadata for an experiment run.

Every probe is either a command run through an injectable ``CommandRunner`` or a
file read through an injectable ``ReadText``. A probe that cannot produce a value
yields ``None`` together with a reason string; nothing is guessed.
"""

import platform
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

COMMAND_TIMEOUT_S = 10.0
GOVERNOR_PATH = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"


@dataclass(frozen=True)
class CommandResult:
    ok: bool
    stdout: str = ""
    error: str = ""


CommandRunner = Callable[[Sequence[str]], CommandResult]
ReadText = Callable[[str], str | None]


def run_command(argv: Sequence[str]) -> CommandResult:
    """Run a command without a shell. Never raises; failures are returned as results."""
    try:
        proc = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_S,
            check=False,
        )
    except FileNotFoundError:
        return CommandResult(ok=False, error=f"command not found: {argv[0]}")
    except subprocess.TimeoutExpired:
        return CommandResult(ok=False, error=f"timed out after {COMMAND_TIMEOUT_S}s: {argv[0]}")
    except OSError as exc:
        return CommandResult(ok=False, error=f"could not run {argv[0]}: {exc}")
    if proc.returncode != 0:
        detail = proc.stderr.strip()[:200]
        return CommandResult(ok=False, error=f"exit {proc.returncode}: {detail}".rstrip(": "))
    return CommandResult(ok=True, stdout=proc.stdout.strip())


def read_text_file(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


NULLABLE_FIELDS: tuple[str, ...] = (
    "os_name",
    "os_version",
    "kernel",
    "cpu_model",
    "cpu_cores",
    "cpu_governor",
    "docker_version",
    "docker_operating_system",
    "docker_vm_cpus",
    "docker_vm_memory_bytes",
    "image_digests",
    "python_version",
    "k6_version",
    "cpu_allocation",
    "git_sha",
)


class EnvironmentInfo(BaseModel):
    os_name: str | None = None
    os_version: str | None = None
    kernel: str | None = None
    cpu_model: str | None = None
    cpu_cores: int | None = Field(default=None, description="logical CPUs visible to the host")
    cpu_governor: str | None = Field(default=None, description="Linux cpufreq governor only")
    docker_version: str | None = None
    docker_operating_system: str | None = Field(
        default=None, description="docker info OperatingSystem, e.g. 'Docker Desktop'"
    )
    docker_vm_cpus: int | None = None
    docker_vm_memory_bytes: int | None = None
    image_digests: dict[str, str] | None = None
    python_version: str | None = None
    k6_version: str | None = None
    cpu_allocation: dict[str, list[int]] | None = None
    git_sha: str | None = None
    cpu_pinning_reliable: bool
    comparability: Literal["canonical", "not-cross-machine-comparable"]
    reasons: dict[str, str] = Field(
        default_factory=dict,
        description="field name -> why the value is null (or why it is not canonical)",
    )

    @model_validator(mode="after")
    def _every_null_has_reason(self) -> "EnvironmentInfo":
        missing = [
            name
            for name in NULLABLE_FIELDS
            if getattr(self, name) is None and name not in self.reasons
        ]
        if missing:
            raise ValueError(f"null values without a reason: {missing}")
        return self


def _run_probe(runner: CommandRunner, argv: Sequence[str]) -> tuple[str | None, str | None]:
    result = runner(argv)
    if not result.ok:
        return None, result.error or f"{argv[0]} failed"
    if not result.stdout:
        return None, f"{' '.join(argv)} printed nothing"
    return result.stdout, None


def _parse_int(text: str, what: str) -> tuple[int | None, str | None]:
    try:
        return int(text.strip()), None
    except ValueError:
        return None, f"could not parse {what} from {text.strip()[:80]!r}"


def _int_probe(
    runner: CommandRunner, argv: Sequence[str], what: str
) -> tuple[int | None, str | None]:
    text, reason = _run_probe(runner, argv)
    if text is None:
        return None, reason
    return _parse_int(text, what)


def _take[T](reasons: dict[str, str], name: str, pair: tuple[T | None, str | None]) -> T | None:
    value, reason = pair
    if value is None:
        reasons[name] = reason or f"{name} unavailable"
    return value


def _os_version(
    host_system: str, runner: CommandRunner, read_text: ReadText
) -> tuple[str | None, str | None]:
    if host_system == "Darwin":
        return _run_probe(runner, ["sw_vers", "-productVersion"])
    if host_system == "Linux":
        text = read_text("/etc/os-release")
        if text is None:
            return None, "/etc/os-release not readable"
        match = re.search(r'^PRETTY_NAME="?([^"\n]+)"?$', text, re.MULTILINE)
        if match is None:
            return None, "no PRETTY_NAME in /etc/os-release"
        return match.group(1).strip(), None
    return None, f"OS version probe not implemented for {host_system!r}"


def _cpu_model(
    host_system: str, runner: CommandRunner, read_text: ReadText
) -> tuple[str | None, str | None]:
    if host_system == "Darwin":
        return _run_probe(runner, ["sysctl", "-n", "machdep.cpu.brand_string"])
    if host_system == "Linux":
        text = read_text("/proc/cpuinfo")
        if text is None:
            return None, "/proc/cpuinfo not readable"
        match = re.search(r"^model name\s*:\s*(.+)$", text, re.MULTILINE)
        if match is None:
            return None, "no 'model name' line in /proc/cpuinfo"
        return match.group(1).strip(), None
    return None, f"CPU model probe not implemented for {host_system!r}"


def _cpu_cores(host_system: str, runner: CommandRunner) -> tuple[int | None, str | None]:
    if host_system == "Darwin":
        return _int_probe(runner, ["sysctl", "-n", "hw.logicalcpu"], "hw.logicalcpu")
    if host_system == "Linux":
        return _int_probe(runner, ["nproc"], "nproc")
    return None, f"CPU core probe not implemented for {host_system!r}"


def _governor(host_system: str, read_text: ReadText) -> tuple[str | None, str | None]:
    if host_system != "Linux":
        return None, f"cpu governor is Linux cpufreq only; not applicable on {host_system!r}"
    text = read_text(GOVERNOR_PATH)
    if text is None:
        return None, f"{GOVERNOR_PATH} not readable (no cpufreq driver or no permission)"
    return text.strip(), None


def _git_sha(runner: CommandRunner, experiment_dir: Path) -> tuple[str | None, str | None]:
    sha, reason = _run_probe(runner, ["git", "-C", str(experiment_dir), "rev-parse", "HEAD"])
    if sha is None:
        return None, f"not a git repository or git unavailable: {reason}"
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        return None, f"unexpected git output: {sha[:80]!r}"
    return sha, None


def _k6_version(runner: CommandRunner) -> tuple[str | None, str | None]:
    text, reason = _run_probe(runner, ["k6", "version"])
    if text is None:
        return None, reason
    match = re.search(r"k6 v(\S+)", text)
    if match is None:
        return None, f"unrecognised k6 version output: {text[:80]!r}"
    return match.group(1), None


def _pinning(
    host_system: str, docker_os: str | None, docker_os_reason: str | None
) -> tuple[bool, str | None]:
    if host_system == "Darwin":
        return False, "macOS host: Docker runs in a Linux VM, so cpuset pinning is not reliable"
    if host_system != "Linux":
        return False, f"unsupported host OS for CPU pinning: {host_system!r}"
    if docker_os is None:
        return False, f"Docker engine not confirmed as native Linux ({docker_os_reason})"
    if "docker desktop" in docker_os.lower():
        return False, "Docker Desktop runs containers in a VM, so cpuset pinning is not reliable"
    return True, None


def capture_environment(
    *,
    image_digests: Mapping[str, str] | None = None,
    cpu_allocation: Mapping[str, Sequence[int]] | None = None,
    experiment_dir: Path | None = None,
    host_system: str | None = None,
    runner: CommandRunner = run_command,
    read_text: ReadText = read_text_file,
) -> EnvironmentInfo:
    """Capture host, Docker, and runtime metadata. Missing tools yield nulls with reasons."""
    system = platform.system() if host_system is None else host_system
    reasons: dict[str, str] = {}

    os_name = "macOS" if system == "Darwin" else system or None
    if os_name is None:
        reasons["os_name"] = "platform.system() returned an empty name"
    os_version = _take(reasons, "os_version", _os_version(system, runner, read_text))
    kernel = _take(reasons, "kernel", _run_probe(runner, ["uname", "-r"]))
    cpu_model = _take(reasons, "cpu_model", _cpu_model(system, runner, read_text))
    cpu_cores = _take(reasons, "cpu_cores", _cpu_cores(system, runner))
    cpu_governor = _take(reasons, "cpu_governor", _governor(system, read_text))

    docker_version = _take(
        reasons,
        "docker_version",
        _run_probe(runner, ["docker", "version", "--format", "{{.Server.Version}}"]),
    )
    docker_os_pair = _run_probe(runner, ["docker", "info", "--format", "{{.OperatingSystem}}"])
    docker_os = _take(reasons, "docker_operating_system", docker_os_pair)
    docker_vm_cpus = _take(
        reasons,
        "docker_vm_cpus",
        _int_probe(runner, ["docker", "info", "--format", "{{.NCPU}}"], "NCPU"),
    )
    docker_vm_memory = _take(
        reasons,
        "docker_vm_memory_bytes",
        _int_probe(runner, ["docker", "info", "--format", "{{.MemTotal}}"], "MemTotal"),
    )

    if image_digests is None:
        reasons["image_digests"] = "no image digests supplied to capture_environment"
    digests = None if image_digests is None else dict(image_digests)

    python_version = platform.python_version() or None
    if python_version is None:
        reasons["python_version"] = "platform.python_version() returned an empty string"
    k6_version = _take(reasons, "k6_version", _k6_version(runner))

    if cpu_allocation is None:
        reasons["cpu_allocation"] = "no CPU allocation supplied to capture_environment"
    allocation = None if cpu_allocation is None else {k: list(v) for k, v in cpu_allocation.items()}

    experiment = Path.cwd() if experiment_dir is None else experiment_dir
    git_sha = _take(reasons, "git_sha", _git_sha(runner, experiment))

    reliable, pinning_reason = _pinning(system, docker_os, docker_os_pair[1])
    if pinning_reason is not None:
        reasons["cpu_pinning_reliable"] = pinning_reason
    comparability: Literal["canonical", "not-cross-machine-comparable"] = (
        "canonical" if reliable else "not-cross-machine-comparable"
    )
    if not reliable:
        reasons["comparability"] = "cpu_pinning_reliable is false: " + (pinning_reason or "")

    return EnvironmentInfo(
        os_name=os_name,
        os_version=os_version,
        kernel=kernel,
        cpu_model=cpu_model,
        cpu_cores=cpu_cores,
        cpu_governor=cpu_governor,
        docker_version=docker_version,
        docker_operating_system=docker_os,
        docker_vm_cpus=docker_vm_cpus,
        docker_vm_memory_bytes=docker_vm_memory,
        image_digests=digests,
        python_version=python_version,
        k6_version=k6_version,
        cpu_allocation=allocation,
        git_sha=git_sha,
        cpu_pinning_reliable=reliable,
        comparability=comparability,
        reasons=reasons,
    )
