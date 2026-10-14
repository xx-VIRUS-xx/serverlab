"""k6 container execution: command shape and cpu.stat parsing (no Docker needed)."""

import subprocess
from pathlib import Path
from typing import Any

import pytest

from serverlab.load.base import LoadSpec
from serverlab.load.k6 import ContainerExec, K6Adapter, parse_cpu_stat_usec


def test_parse_cpu_stat_usec() -> None:
    assert parse_cpu_stat_usec("usage_usec 5962965\nuser_usec 1\n") == 5962965
    assert parse_cpu_stat_usec("user_usec 1\n") is None
    assert parse_cpu_stat_usec("usage_usec abc\n") is None


def test_container_command_uses_sh_wrapper_and_translates_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> "subprocess.CompletedProcess[str]":
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 1, "", "no summary")

    monkeypatch.setattr(subprocess, "run", fake_run)
    ce = ContainerExec(tmp_path / "c.yaml", "proj", "k6", tmp_path)
    spec = LoadSpec(rate=5, duration_s=3, pre_allocated_vus=1, max_vus=2, url="http://sut/x")
    K6Adapter(container=ce).run(spec, tmp_path, "r1")
    cmd = seen[0]
    assert cmd[:4] == ["docker", "compose", "-p", "proj"]
    assert cmd[cmd.index("--entrypoint") + 1] == "sh" and "k6" in cmd
    script = cmd[cmd.index("-c") + 1]
    assert "CPU_STAT_PATH=/work/r1.k6cpu.stat" in script
    assert 'k6 "$@"' in script and "exit $rc" in script
    assert "--summary-export=/work/r1.k6summary.json" in cmd and cmd[-1] == "/work/r1.js"
