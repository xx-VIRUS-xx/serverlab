"""UI server and SDK on hermetic result directories built with the test fakes (no Docker/k6)."""

import json
import time
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from serverlab.experiment.runner import run_experiment
from serverlab.experiment.spec import ExperimentSpec
from serverlab.experiment.sweeps import run_sweeps
from serverlab.sdk import Lab
from serverlab.ui.server import create_app
from tests.test_runner import FakeAdapter, FakeDocker, _spec
from tests.test_sweeps import Adapter as SweepAdapter
from tests.test_sweeps import Docker as SweepDocker
from tests.test_sweeps import _spec as sweep_spec

TOKEN = "t0ken"
HOST = {"host": "testserver"}


@pytest.fixture(scope="module")
def root(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp_path = tmp_path_factory.mktemp("lab")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "predictions.md").write_text("# Predictions\nC = n / 0.05\n")
    exp = tmp_path / "experiments"
    exp.mkdir()
    (exp / "demo.yaml").write_text(yaml.safe_dump(_spec(2).model_dump(mode="json")))
    (exp / "broken.yaml").write_text("name: x\n")
    res = tmp_path / "results"
    run_experiment(_spec(2), res, base_dir=tmp_path, cmd=FakeDocker(), sleep=lambda _s: None,
                   adapter_factory=lambda _c: FakeAdapter(invalid_runs={"run_A_1"}),
                   collectors_factory=None)
    time.sleep(1.1)  # distinct timestamp for the sweep result
    run_sweeps(sweep_spec(), res, base_dir=tmp_path, cmd=SweepDocker(), hdr_builder=None,
               sleep=lambda _s: None, adapter_factory=lambda _c: SweepAdapter())
    return tmp_path


def client(root: Path, **kw: Any) -> TestClient:
    app = create_app(root, token=TOKEN, allowed_hosts={"testserver"}, **kw)
    return TestClient(app, base_url="http://testserver")


def post(c: TestClient, path: str, body: dict[str, Any], token: str | None = TOKEN) -> Any:
    headers = {} if token is None else {"X-Serverlab-Token": token}
    return c.post(path, json=body, headers=headers)


def test_host_guard_rejects_other_hosts(root: Path) -> None:
    c = client(root)
    assert c.get("/api/specs").status_code == 200
    assert c.get("/api/specs", headers={"host": "evil.example"}).status_code == 403
    assert c.get("/api/specs", headers={"host": "127.0.0.1:1"}).status_code == 403


def test_page_embeds_the_token_and_posts_need_it(root: Path) -> None:
    c = client(root)
    assert f'content="{TOKEN}"' in c.get("/").text
    body = {"action": "schedule", "spec": "demo.yaml"}
    assert post(c, "/api/jobs", body, token=None).status_code == 403
    assert post(c, "/api/jobs", body, token="wrong").status_code == 403
    assert post(c, "/api/jobs/cancel", {}, token="wrong").status_code == 403


def test_specs_listing_flags_invalid_specs(root: Path) -> None:
    rows = {r["file"]: r for r in client(root).get("/api/specs").json()}
    assert rows["demo.yaml"]["conditions"] == ["A", "B"] and rows["demo.yaml"]["error"] is None
    assert rows["broken.yaml"]["error"] and rows["broken.yaml"]["name"] is None


def test_results_list_and_run_summary(root: Path) -> None:
    c = client(root)
    rows = c.get("/api/results").json()
    assert [r["kind"] for r in rows] == ["sweep", "run"]  # newest first
    run = next(r for r in rows if r["kind"] == "run")
    assert (run["runs"], run["invalid"]) == (4, 1)
    s = c.get(f"/api/results/{run['experiment']}/{run['timestamp']}").json()
    assert s["kind"] == "run" and len(s["runs"]) == 4
    assert s["schedule"]["method"] == "randomized-blocks"
    bad = next(r for r in s["runs"] if not r["valid"])
    assert bad["invalid_reasons"] == ["dropped"] and bad["p99_ms"] == 3
    assert "warnings" in s["environment"] and s["environment"]["comparability"]


def test_sweep_summary_has_steps_and_knee(root: Path) -> None:
    c = client(root)
    row = next(r for r in c.get("/api/results").json() if r["kind"] == "sweep")
    s = c.get(f"/api/results/{row['experiment']}/{row['timestamp']}").json()
    assert s["schedule"] is not None and len(s["sweeps"]) == 4
    sw = s["sweeps"][0]
    assert sw["knee"]["status"] == "found" and sw["knee"]["lo"] <= 100 <= sw["knee"]["hi"]
    assert {st["status"] for st in sw["steps"]} <= {"ok", "saturated", "invalid-other"}


def test_path_traversal_and_bad_names_rejected(root: Path) -> None:
    c = client(root)
    for path in ("/api/results/..%2f../x", "/api/results/%2e%2e/%2e%2e", "/api/results/a b/c"):
        assert c.get(path).status_code in (400, 404)
    assert c.get("/api/results/nope/20260101T000000Z").status_code == 400
    row = c.get("/api/results").json()[0]
    base = f"/api/results/{row['experiment']}/{row['timestamp']}/metrics"
    assert c.get(f"{base}/..%2f..%2fspec/docker").status_code in (400, 404)
    assert c.get(f"{base}/run_A_1/other").status_code == 400
    assert c.get(f"{base}/run_A_1/app").status_code == 404  # no such metrics file


def test_predictions_endpoint(root: Path) -> None:
    assert "C = n / 0.05" in client(root).get("/api/predictions").json()["markdown"]


class FakeRunner:
    def __init__(self) -> None:
        self.started: list[tuple[str, Path | None]] = []
        self.busy = False

    def start(self, action: str, spec: Path | None) -> dict[str, Any]:
        from serverlab.ui.jobs import JobBusyError

        if self.busy:
            raise JobBusyError("a job is already running")
        self.busy = True
        self.started.append((action, spec))
        return {"id": "j1", "state": "running"}

    def current(self) -> dict[str, Any]:
        return {"state": "running" if self.busy else "idle"}

    def cancel(self) -> bool:
        was, self.busy = self.busy, False
        return was


def test_job_rules_busy_cancel_and_spec_confinement(root: Path) -> None:
    fake = FakeRunner()
    c = client(root, runner=fake)
    ok = post(c, "/api/jobs", {"action": "run", "spec": "demo.yaml"})
    assert ok.status_code == 202 and fake.started[0][0] == "run"
    assert post(c, "/api/jobs", {"action": "run", "spec": "demo.yaml"}).status_code == 409
    assert post(c, "/api/jobs/cancel", {}).json() == {"cancelled": True}
    assert post(c, "/api/jobs/cancel", {}).json() == {"cancelled": False}
    for spec in ("../etc/passwd.yaml", "/etc/hosts", "nope.yaml", "demo.txt", "broken.yaml", ""):
        r = post(c, "/api/jobs", {"action": "run", "spec": spec})
        assert r.status_code == 400, spec
    assert post(c, "/api/jobs", {"action": "rm -rf", "spec": "demo.yaml"}).status_code == 400
    assert post(c, "/api/jobs", {"action": "run"}).status_code == 400
    assert len(fake.started) == 1  # nothing else reached the runner


def test_pasted_yaml_is_validated_then_saved_inside_experiments(root: Path) -> None:
    fake = FakeRunner()
    c = client(root, runner=fake)
    assert post(c, "/api/jobs", {"action": "schedule", "yaml": "name: x"}).status_code == 400
    assert post(c, "/api/jobs", {"action": "schedule", "yaml": "x" * 70_000}).status_code == 400
    good = yaml.safe_dump(_spec(1).model_dump(mode="json"))
    assert post(c, "/api/jobs", {"action": "schedule", "yaml": good}).status_code == 202
    saved = fake.started[0][1]
    assert saved is not None and saved.parent == root / "experiments"
    assert saved.name.startswith("ui-")
    ExperimentSpec.model_validate(yaml.safe_load(saved.read_text()))


def test_real_job_runs_the_cli_and_reports_result(root: Path) -> None:
    c = client(root)
    assert c.get("/api/jobs/current").json()["state"] == "idle"
    assert post(c, "/api/jobs", {"action": "schedule", "spec": "demo.yaml"}).status_code == 202
    for _ in range(100):
        cur = c.get("/api/jobs/current").json()
        if cur["state"] != "running":
            break
        time.sleep(0.2)
    assert cur["state"] == "finished" and cur["exit_code"] == 0
    assert any(line.startswith("order:") for line in cur["log_tail"]), cur["log_tail"]
    assert cur["action"] == "schedule" and cur["spec"] == "demo.yaml"


def test_sdk_reads_the_same_results(root: Path) -> None:
    lab = Lab(root / "results", root / "experiments")
    spec = lab.load_spec("demo.yaml")
    assert lab.plan(spec).order() == lab.plan(spec).order()
    assert len(lab.results()) == 2
    row = lab.results()[1]
    assert len(lab.result(row["experiment"], row["timestamp"])["runs"]) == 4
    assert yaml.safe_load(Lab.dump_spec(spec))["name"] == "unit"


def test_results_from_earlier_versions_load_with_missing_fields_as_none(tmp_path: Path) -> None:
    """Weeks 1-3 wrote records without offered_rate/repetition/index and specs without
    conditions. They must load, with unknown fields as None (never invented)."""
    d = tmp_path / "results" / "old" / "20260101T000000Z"
    (d / "raw").mkdir(parents=True)
    (d / "environment.json").write_text(
        json.dumps({"comparability": "not-cross-machine-comparable"})
    )
    (d / "spec.yaml").write_text(yaml.safe_dump({
        "name": "old", "seed": 1, "load": {"rate": 50, "duration_s": 20}, "cpus": {"sut": [0]},
    }))
    rec = {"experiment": "old", "condition": "main", "result": {
        "run_id": "run_main_1", "planned": 1000, "completed": 1000, "dropped": 0, "peak_vus": 5,
        "latency_ms": {"p50": 1.0, "p90": 2.0, "p99": 3.0, "max": 4.0}, "error_rate": 0.0,
        "valid": True, "invalid_reasons": [],
    }}
    (d / "raw" / "run_main_1.json").write_text(json.dumps(rec))
    sweep = {"condition": "main", "sweep": {"steps": [], "coarse_order": [], "bisect_rates": [],
             "knee": {"status": "above_range"}}, "records": []}
    (d / "sweep_main.json").write_text(json.dumps(sweep))
    c = client(tmp_path)
    assert c.get("/api/results").json()[0]["runs"] == 1
    s = c.get("/api/results/old/20260101T000000Z").json()
    run = s["runs"][0]
    assert run["offered_rate"] == 50  # single-rate spec: the spec's rate is the run's rate
    assert run["repetition"] is None and run["index"] is None and run["sut_cpu_usec"] is None
    assert s["spec"]["conditions"] == [] and s["sweeps"][0]["repetition"] is None
