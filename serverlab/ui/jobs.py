"""One job at a time: the CLI as a subprocess, its output captured, cancellable.

Only the existing CLI runs measurements, so the UI has no second code path. One job at a time
because two concurrent experiments would perturb each other's measurements.
"""

import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

ACTIONS = ("schedule", "run", "sweep", "validate-offline")
LOG_TAIL = 200
CANCEL_GRACE_S = 60.0
_RESULT_LINE = re.compile(r"^results:\s+(?:.*/)?([A-Za-z0-9_.-]+)/([0-9]{8}T[0-9]{6}Z)\b")


class JobBusyError(RuntimeError):
    pass


def command_for(action: str, spec: Path | None, results_root: Path) -> list[str]:
    base = [sys.executable, "-m", "serverlab"]
    if action == "validate-offline":
        return [*base, "validate", "--offline"]
    if spec is None:
        raise ValueError(f"action {action!r} needs a spec")
    if action == "schedule":
        return [*base, "run", str(spec), "--dry-run"]
    if action == "run":
        return [*base, "run", str(spec), "--results", str(results_root)]
    if action == "sweep":
        return [*base, "sweep", str(spec), "--results", str(results_root)]
    raise ValueError(f"unknown action {action!r}")


class JobRunner:
    def __init__(self, cwd: Path, results_root: Path) -> None:
        self._cwd = cwd
        self._results_root = results_root
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[str] | None = None
        self._job: dict[str, Any] | None = None
        self._log: deque[str] = deque(maxlen=2000)
        self._cancel_requested = False

    def start(self, action: str, spec: Path | None) -> dict[str, Any]:
        cmd = command_for(action, spec, self._results_root)
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                raise JobBusyError("a job is already running")
            self._log.clear()
            self._cancel_requested = False
            proc = subprocess.Popen(
                cmd, cwd=self._cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            self._proc = proc
            self._job = {
                "id": uuid4().hex[:12], "action": action,
                "spec": None if spec is None else spec.name,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "started_mono": time.monotonic(), "result_dir": None,
            }
            threading.Thread(target=self._pump, args=(proc,), daemon=True).start()
            return {"id": self._job["id"], "state": "running"}

    def _pump(self, proc: subprocess.Popen[str]) -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            text = line.rstrip("\n")
            with self._lock:
                self._log.append(text)
                match = _RESULT_LINE.match(text)
                if match and self._job is not None:
                    self._job["result_dir"] = {"experiment": match[1], "timestamp": match[2]}
        proc.wait()

    def cancel(self) -> bool:
        with self._lock:
            proc = self._proc
            if proc is None or proc.poll() is not None:
                return False
            self._cancel_requested = True
        proc.send_signal(signal.SIGTERM)  # the CLI turns this into KeyboardInterrupt -> teardown
        threading.Thread(target=self._kill_later, args=(proc,), daemon=True).start()
        return True

    def _kill_later(self, proc: subprocess.Popen[str]) -> None:
        try:
            proc.wait(timeout=CANCEL_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()

    def current(self) -> dict[str, Any]:
        with self._lock:
            if self._job is None or self._proc is None:
                return {"id": None, "action": None, "spec": None, "state": "idle",
                        "exit_code": None, "started_at": None, "elapsed_s": None,
                        "log_tail": [], "result_dir": None}
            code = self._proc.poll()
            if code is None:
                state = "running"
            elif self._cancel_requested:
                state = "cancelled"
            else:
                state = "finished" if code in (0, 3) else "failed"
            elapsed = time.monotonic() - self._job["started_mono"]
            return {
                "id": self._job["id"], "action": self._job["action"], "spec": self._job["spec"],
                "state": state, "exit_code": code, "started_at": self._job["started_at"],
                "elapsed_s": round(elapsed, 1), "log_tail": list(self._log)[-LOG_TAIL:],
                "result_dir": self._job["result_dir"],
            }
