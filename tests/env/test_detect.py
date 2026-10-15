import platform
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import ValidationError

from serverlab.env.detect import (
    CommandResult,
    CommandRunner,
    EnvironmentInfo,
    ReadText,
    capture_environment,
)

SHA = "a" * 40
GOVERNOR_PATH = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"


def ok(stdout: str) -> CommandResult:
    return CommandResult(ok=True, stdout=stdout)


def make_runner(answers: dict[tuple[str, ...], CommandResult]) -> CommandRunner:
    """Unmapped commands behave like a missing binary."""

    def run(argv: Sequence[str]) -> CommandResult:
        key = tuple(argv)
        if key in answers:
            return answers[key]
        return CommandResult(ok=False, error=f"command not found: {argv[0]}")

    return run


def make_reader(files: dict[str, str]) -> ReadText:
    def read(path: str) -> str | None:
        return files.get(path)

    return read


LINUX_ANSWERS: dict[tuple[str, ...], CommandResult] = {
    ("uname", "-r"): ok("5.15.0-100-generic"),
    ("nproc",): ok("16"),
    ("docker", "version", "--format", "{{.Server.Version}}"): ok("27.1.1"),
    ("docker", "info", "--format", "{{.OperatingSystem}}"): ok("Ubuntu 22.04.4 LTS"),
    ("docker", "info", "--format", "{{.NCPU}}"): ok("8"),
    ("docker", "info", "--format", "{{.MemTotal}}"): ok("8589934592"),
    ("k6", "version"): ok("k6 v0.52.0 (go1.22.2, linux/amd64)"),
    ("git", "-C", "/exp", "rev-parse", "HEAD"): ok(SHA),
    ("docker", "ps", "--format", "{{.Names}}"): ok(""),
}

LINUX_FILES: dict[str, str] = {
    "/etc/os-release": 'NAME="Ubuntu"\nPRETTY_NAME="Ubuntu 22.04.4 LTS"\n',
    "/proc/cpuinfo": "processor\t: 0\nmodel name\t: Test CPU 9000\n",
    GOVERNOR_PATH: "performance\n",
}

MAC_ANSWERS: dict[tuple[str, ...], CommandResult] = {
    ("uname", "-r"): ok("25.6.0"),
    ("sw_vers", "-productVersion"): ok("26.6.2"),
    ("sysctl", "-n", "machdep.cpu.brand_string"): ok("Test Mac CPU"),
    ("sysctl", "-n", "hw.logicalcpu"): ok("10"),
    ("docker", "version", "--format", "{{.Server.Version}}"): ok("27.1.1"),
    ("docker", "info", "--format", "{{.OperatingSystem}}"): ok("Docker Desktop"),
    ("docker", "info", "--format", "{{.NCPU}}"): ok("4"),
    ("docker", "info", "--format", "{{.MemTotal}}"): ok("4294967296"),
    ("k6", "version"): ok("k6 v0.52.0 (go1.22.2, darwin/arm64)"),
    ("git", "-C", "/exp", "rev-parse", "HEAD"): ok(SHA),
}


def test_linux_native_docker_is_canonical() -> None:
    info = capture_environment(
        image_digests={"sut": "example/sut@sha256:abc"},
        cpu_allocation={"sut": [0, 1], "load": [2, 3]},
        experiment_dir=Path("/exp"),
        host_system="Linux",
        runner=make_runner(LINUX_ANSWERS),
        read_text=make_reader(LINUX_FILES),
    )
    assert info.os_name == "Linux"
    assert info.os_version == "Ubuntu 22.04.4 LTS"
    assert info.kernel == "5.15.0-100-generic"
    assert info.cpu_model == "Test CPU 9000"
    assert info.cpu_cores == 16
    assert info.cpu_governor == "performance"
    assert info.docker_version == "27.1.1"
    assert info.docker_operating_system == "Ubuntu 22.04.4 LTS"
    assert info.docker_vm_cpus == 8
    assert info.docker_vm_memory_bytes == 8589934592
    assert info.image_digests == {"sut": "example/sut@sha256:abc"}
    assert info.python_version == platform.python_version()
    assert info.k6_version == "0.52.0"
    assert info.cpu_allocation == {"sut": [0, 1], "load": [2, 3]}
    assert info.git_sha == SHA
    assert info.cpu_pinning_reliable is True
    assert info.comparability == "canonical"
    # only the macOS-only probes are null on a canonical Linux host, each with its reason
    assert set(info.reasons) == {"power_source", "low_power_mode", "thermal_cpu_speed_limit_pct"}
    assert info.other_running_containers == []


def test_macos_forces_unreliable_pinning_and_not_comparable() -> None:
    info = capture_environment(
        experiment_dir=Path("/exp"),
        host_system="Darwin",
        runner=make_runner(MAC_ANSWERS),
        read_text=make_reader({}),
    )
    assert info.os_name == "macOS"
    assert info.os_version == "26.6.2"
    assert info.cpu_model == "Test Mac CPU"
    assert info.cpu_cores == 10
    assert info.cpu_governor is None
    assert "Linux cpufreq" in info.reasons["cpu_governor"]
    assert info.docker_operating_system == "Docker Desktop"
    assert info.cpu_pinning_reliable is False
    assert info.comparability == "not-cross-machine-comparable"
    assert "cpu_pinning_reliable" in info.reasons
    assert "comparability" in info.reasons


def test_linux_docker_desktop_forces_unreliable_pinning() -> None:
    answers = dict(LINUX_ANSWERS)
    answers[("docker", "info", "--format", "{{.OperatingSystem}}")] = ok("Docker Desktop")
    info = capture_environment(
        experiment_dir=Path("/exp"),
        host_system="Linux",
        runner=make_runner(answers),
        read_text=make_reader(LINUX_FILES),
    )
    assert info.cpu_pinning_reliable is False
    assert info.comparability == "not-cross-machine-comparable"


def test_missing_docker_and_k6_do_not_crash() -> None:
    info = capture_environment(
        experiment_dir=Path("/exp"),
        host_system="Linux",
        runner=make_runner({("uname", "-r"): ok("5.15.0")}),
        read_text=make_reader(LINUX_FILES),
    )
    assert info.docker_version is None
    assert "command not found: docker" in info.reasons["docker_version"]
    assert info.docker_operating_system is None
    assert info.docker_vm_cpus is None
    assert info.docker_vm_memory_bytes is None
    assert info.k6_version is None
    assert "command not found: k6" in info.reasons["k6_version"]
    assert info.cpu_pinning_reliable is False
    assert info.comparability == "not-cross-machine-comparable"
    assert "not confirmed as native Linux" in info.reasons["cpu_pinning_reliable"]


def test_not_a_git_repo_gives_null_sha_with_reason() -> None:
    answers = dict(LINUX_ANSWERS)
    answers[("git", "-C", "/exp", "rev-parse", "HEAD")] = CommandResult(
        ok=False, error="exit 128: fatal: not a git repository"
    )
    info = capture_environment(
        experiment_dir=Path("/exp"),
        host_system="Linux",
        runner=make_runner(answers),
        read_text=make_reader(LINUX_FILES),
    )
    assert info.git_sha is None
    assert "not a git repository" in info.reasons["git_sha"]


def test_missing_inputs_are_null_with_reason() -> None:
    info = capture_environment(
        experiment_dir=Path("/exp"),
        host_system="Linux",
        runner=make_runner(LINUX_ANSWERS),
        read_text=make_reader(LINUX_FILES),
    )
    assert info.image_digests is None
    assert "no image digests" in info.reasons["image_digests"]
    assert info.cpu_allocation is None
    assert "no CPU allocation" in info.reasons["cpu_allocation"]


def test_unparseable_output_is_null_not_guessed() -> None:
    answers = dict(LINUX_ANSWERS)
    answers[("docker", "info", "--format", "{{.NCPU}}")] = ok("not-a-number")
    answers[("k6", "version")] = ok("something else entirely")
    info = capture_environment(
        experiment_dir=Path("/exp"),
        host_system="Linux",
        runner=make_runner(answers),
        read_text=make_reader(LINUX_FILES),
    )
    assert info.docker_vm_cpus is None
    assert "could not parse" in info.reasons["docker_vm_cpus"]
    assert info.k6_version is None
    assert "unrecognised k6 version output" in info.reasons["k6_version"]


def test_null_without_reason_is_rejected() -> None:
    with pytest.raises(ValidationError, match="null values without a reason"):
        EnvironmentInfo(
            cpu_pinning_reliable=False,
            comparability="not-cross-machine-comparable",
            reasons={"comparability": "x"},
        )


def test_json_round_trip() -> None:
    info = capture_environment(
        experiment_dir=Path("/exp"),
        host_system="Darwin",
        runner=make_runner(MAC_ANSWERS),
        read_text=make_reader({}),
    )
    restored = EnvironmentInfo.model_validate_json(info.model_dump_json())
    assert restored == info


def _mac_env(extra: dict[tuple[str, ...], CommandResult]) -> EnvironmentInfo:
    return capture_environment(
        host_system="Darwin", experiment_dir=Path("/exp"),
        runner=make_runner({**MAC_ANSWERS, **extra}), read_text=make_reader({}),
    )


def test_mac_power_thermal_and_other_containers_captured() -> None:
    env = _mac_env({
        ("pmset", "-g", "batt"): ok("Now drawing from 'AC Power'\n -InternalBattery-0 100%"),
        ("pmset", "-g"): ok("System-wide power settings:\n lowpowermode         0\n sleep 1"),
        ("pmset", "-g", "therm"): ok("Note: No thermal warning level has been recorded"),
        ("docker", "ps", "--format", "{{.Names}}"): ok("phielia-pg\nserverlab-x-sut-1\nredis"),
    })
    assert env.power_source == "AC Power" and env.low_power_mode is False
    assert env.thermal_cpu_speed_limit_pct is None
    assert "No thermal warning" in env.reasons["thermal_cpu_speed_limit_pct"]
    assert env.other_running_containers == ["phielia-pg", "redis"]  # our own stack excluded


def test_mac_low_power_battery_and_speed_limit_parsed() -> None:
    env = _mac_env({
        ("pmset", "-g", "batt"): ok("Now drawing from 'Battery Power'"),
        ("pmset", "-g"): ok(" lowpowermode         1"),
        ("pmset", "-g", "therm"): ok("CPU_Scheduler_Limit = 100\nCPU_Speed_Limit     = 75"),
        ("docker", "ps", "--format", "{{.Names}}"): ok(""),
    })
    assert env.power_source == "Battery Power" and env.low_power_mode is True
    assert env.thermal_cpu_speed_limit_pct == 75 and env.other_running_containers == []


def test_linux_has_null_mac_probes_with_reasons_and_failed_docker_is_null() -> None:
    env = capture_environment(
        host_system="Linux", experiment_dir=Path("/exp"),
        runner=make_runner(LINUX_ANSWERS), read_text=make_reader(LINUX_FILES),
    )
    for name in ("power_source", "low_power_mode", "thermal_cpu_speed_limit_pct"):
        assert getattr(env, name) is None and "macOS only" in env.reasons[name]
    assert env.other_running_containers is None and env.reasons["other_running_containers"]
