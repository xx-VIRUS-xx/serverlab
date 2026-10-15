"""Local web UI server (docs/ui-api.md). Binds to 127.0.0.1; see docs/phases/ui.md s2.

Threat model: a page on another origin must not be able to start Docker jobs or read results.
Hence: loopback bind, Host header allow-list (DNS rebinding), a random token that only the
served page knows and every POST must send, spec files only from `experiments/`, and result
names that cannot escape the results directory.
"""

import hashlib
import secrets
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import ValidationError

from serverlab.experiment.spec import ExperimentSpec
from serverlab.results import (
    NAME_RE,
    ResultsError,
    list_results,
    read_metrics,
    result_summary,
)
from serverlab.ui.jobs import ACTIONS, JobBusyError, JobRunner

STATIC = Path(__file__).parent / "static" / "index.html"
TOKEN_HEADER = "x-serverlab-token"
TOKEN_PLACEHOLDER = "__SERVERLAB_TOKEN__"
MAX_YAML_BYTES = 64_000


def _spec_summary(path: Path) -> dict[str, Any]:
    base: dict[str, Any] = {"file": path.name, "name": None, "repetitions": None,
                            "conditions": None, "kind": "experiment", "sweep": None, "error": None}
    try:
        spec = ExperimentSpec.model_validate(yaml.safe_load(path.read_text()))
    except (ValidationError, ValueError, yaml.YAMLError, OSError) as exc:
        base["error"] = str(exc).splitlines()[0][:300]
        return base
    base.update(name=spec.name, repetitions=spec.repetitions, conditions=spec.condition_names)
    if spec.sweep is not None:
        base["kind"] = "sweep"
        base["sweep"] = {
            "rate_lo": spec.sweep.rate_lo, "rate_hi": spec.sweep.rate_hi,
            "coarse_points": spec.sweep.coarse_points, "max_bisect": spec.sweep.max_bisect,
            "step_duration_s": spec.sweep.step_duration_s,
        }
    return base


def create_app(
    root: Path, port: int = 8765, token: str | None = None,
    allowed_hosts: set[str] | None = None, runner: JobRunner | None = None,
) -> FastAPI:
    root = root.resolve()
    results_root = root / "results"
    experiments = root / "experiments"
    tok = token or secrets.token_urlsafe(24)
    hosts = allowed_hosts or {f"127.0.0.1:{port}", f"localhost:{port}"}
    jobs = runner or JobRunner(root, results_root)
    app = FastAPI(title="serverlab", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token = tok

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Any:
        if request.headers.get("host", "") not in hosts:
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        if request.method == "POST" and not secrets.compare_digest(
            request.headers.get(TOKEN_HEADER, ""), tok
        ):
            return JSONResponse({"error": "missing or invalid token"}, status_code=403)
        return await call_next(request)

    @app.exception_handler(ResultsError)
    async def _bad_name(_r: Request, exc: ResultsError) -> JSONResponse:
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(FileNotFoundError)
    async def _missing(_r: Request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse({"error": f"not found: {exc}"}, status_code=404)

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        page = STATIC.read_text() if STATIC.is_file() else (
            "<!doctype html><title>serverlab</title>"
            '<meta name="serverlab-token" content="__SERVERLAB_TOKEN__">'
            "<p>UI page not installed.</p>"
        )
        return HTMLResponse(page.replace(TOKEN_PLACEHOLDER, tok))

    @app.get("/api/specs")
    def specs() -> list[dict[str, Any]]:
        if not experiments.is_dir():
            return []
        return [_spec_summary(p) for p in sorted(experiments.glob("*.yaml"))]

    @app.get("/api/results")
    def results() -> list[dict[str, Any]]:
        return list_results(results_root)

    @app.get("/api/results/{experiment}/{timestamp}")
    def result(experiment: str, timestamp: str) -> dict[str, Any]:
        return result_summary(results_root, experiment, timestamp)

    @app.get("/api/results/{experiment}/{timestamp}/metrics/{run_id}/{kind}")
    def metrics(experiment: str, timestamp: str, run_id: str, kind: str) -> dict[str, Any]:
        return read_metrics(results_root, experiment, timestamp, run_id, kind)

    @app.get("/api/predictions")
    def predictions() -> dict[str, str]:
        path = root / "docs" / "predictions.md"
        return {"markdown": path.read_text() if path.is_file() else ""}

    def _resolve_spec(body: dict[str, Any]) -> Path | None:
        action = body.get("action")
        if action not in ACTIONS:
            raise HTTPException(400, detail=f"action must be one of {list(ACTIONS)}")
        if action == "validate-offline":
            return None
        name, text = body.get("spec"), body.get("yaml")
        if text is not None:
            if len(text.encode()) > MAX_YAML_BYTES:
                raise HTTPException(400, detail="pasted spec too large")
            try:
                ExperimentSpec.model_validate(yaml.safe_load(text))
            except (ValidationError, ValueError, yaml.YAMLError) as exc:
                raise HTTPException(400, detail=f"invalid spec: {str(exc)[:300]}") from exc
            experiments.mkdir(exist_ok=True)
            path = experiments / f"ui-{hashlib.sha256(text.encode()).hexdigest()[:10]}.yaml"
            path.write_text(text)
            return path
        if not isinstance(name, str) or not NAME_RE.match(name) or not name.endswith(".yaml"):
            raise HTTPException(400, detail="spec must be a file name like 'my.yaml'")
        path = (experiments / name).resolve()
        if path.parent != experiments.resolve() or not path.is_file():
            raise HTTPException(400, detail=f"no such spec in experiments/: {name}")
        try:
            ExperimentSpec.model_validate(yaml.safe_load(path.read_text()))
        except (ValidationError, ValueError, yaml.YAMLError) as exc:
            raise HTTPException(400, detail=f"invalid spec: {str(exc)[:300]}") from exc
        return path

    @app.post("/api/jobs", status_code=202)
    async def start_job(request: Request) -> Any:
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(400, detail="body must be a JSON object")
        spec_path = _resolve_spec(body)
        try:
            return jobs.start(body["action"], spec_path)
        except JobBusyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=409)

    @app.get("/api/jobs/current")
    def current() -> dict[str, Any]:
        return jobs.current()

    @app.post("/api/jobs/cancel")
    def cancel() -> dict[str, bool]:
        return {"cancelled": jobs.cancel()}

    @app.exception_handler(HTTPException)
    async def _http(_r: Request, exc: HTTPException) -> JSONResponse:
        return JSONResponse({"error": exc.detail}, status_code=exc.status_code)

    return app
