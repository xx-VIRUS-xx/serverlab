# Phase plan: local UI + SDK + publish

Status: **Approved 2026-10-15: public repository, MIT license, other defaults as written. Built and merged (U-1, U-2); README written; publishing waits for the Week 4 main run and the email confirmation.**
Decision D-4 (owner): a simple interface that runs on the user's own machine, then publish the project. This overrides the V1 non-goal "no web dashboard" (plan.md s2); everything else in plan.md stands. Weeks 5-8 (concurrency model, Redis, bottleneck analyzer + HTML report, LLM serving) are **not built** and the README will list them as roadmap, not as features.

## 1. What the user gets

**SDK** (`serverlab.sdk`), a thin typed facade over what the CLI already does, no new logic:
```python
from serverlab.sdk import Lab
lab = Lab(results_root="results", experiments_dir="experiments")
spec  = lab.load_spec("experiments/week2_demo.yaml")      # or a dict
lab.plan(spec)                                            # seeded schedule, no side effects
outcome = lab.run(spec)                                   # same lifecycle as `serverlab run`
sweep   = lab.sweep(spec)                                 # same as `serverlab sweep`
lab.results()                                             # list of past runs (read-only)
lab.result("week2_demo", "20261014T144256Z")              # parsed summary
lab.validate(offline=True)                                # tool checks (stats), no Docker
lab.analyze_pool(results_dir, calibration_dir)            # graded pool verdicts
```
Every call returns the existing pydantic models; nothing is reimplemented.

**Local web UI** (`serverlab ui`, opens http://127.0.0.1:8765), one page, works offline (no CDN, no external fonts), light/dark:
1. **Experiments:** lists `experiments/*.yaml` with name, conditions, whether it is a sweep. Buttons: *Show schedule* (dry run), *Run*, *Sweep*, *Validate (offline)*.
2. **Live job panel:** log tail while a job runs, elapsed time, Cancel. One job at a time (parallel jobs would corrupt each other's measurements; the UI refuses a second one).
3. **Results:** list of past results; opening one shows environment (with the **warnings** banner: swap, battery, other containers, not-comparable tag), the stored schedule, a runs table (valid/invalid with the reasons, p50/p99, dropped, SUT CPU) and, for sweeps, an SVG curve per condition (achieved vs offered, P99 vs offered, knee bracket marked, saturated/invalid-other steps distinguished) plus the metrics collected (pool wait, wait events) when present.
4. **Predictions:** shows `docs/predictions.md` read-only, so findings are always next to what was predicted before running.
No editing of results; no accounts; no network calls.

## 2. Security (it can start Docker jobs, so this matters)
- Binds **127.0.0.1 only**; refuses requests whose `Host` is not `127.0.0.1:<port>`/`localhost:<port>` (DNS-rebinding guard).
- A random per-start **token**, embedded in the served page and required as a header on every `POST`, so a web page on another origin cannot start jobs.
- Jobs can only run **spec files inside `experiments/`** (validated by the pydantic spec model first) or a YAML pasted into the UI that passes the same validation; results are read from inside `results/` only (path traversal rejected).
- Jobs run as subprocesses of the existing CLI, so there is exactly one code path for measurements.

## 3. Work split
| Task | Owner | Deliverable |
|---|---|---|
| U-1 | Claude | `serverlab/sdk.py`, `serverlab/ui/server.py` (JSON API per section 4), `serverlab ui` command, tests (FastAPI TestClient on real result directories, security tests) |
| U-2 | Haiku worker | `serverlab/ui/static/index.html`: the single-file UI against the JSON API contract (inline CSS/JS/SVG, no dependencies) |
| U-3 | Claude | review U-2 by running it against real results and checking screenshots (headless Chrome), fix issues |
| U-4 | Claude | README (install, quickstart, UI, SDK, what is measured and what is not, honest status/roadmap), packaging (`ui` extra), clean-checkout test |
| U-5 | Claude | publish (needs your answers, section 6) |

## 4. JSON API contract (what U-2 builds against)
`GET /api/specs`, `GET /api/results`, `GET /api/results/{experiment}/{timestamp}`, `GET /api/predictions`, `POST /api/jobs` `{spec, action: schedule|run|sweep|validate-offline}`, `GET /api/jobs/current`, `POST /api/jobs/cancel`. Shapes are fixed in `docs/ui-api.md` before U-2 starts.

## 5. Acceptance
1. `uv run serverlab ui` serves the page; headless-Chrome screenshots of the real Week 2, Week 3 and Week 4 results render (pasted).
2. From the UI: dry-run schedule works; a real short run (`week1_smoke`) starts, streams its log, finishes, and appears under Results.
3. Security tests pass: wrong `Host`, missing/incorrect token, path traversal, a second concurrent job, a spec outside `experiments/`.
4. Fresh clone + `uv sync` + `uv run serverlab ui` works; `pytest`, `ruff`, `mypy` clean.
5. Nothing in the UI states a finding that is not in a results file or `docs/predictions.md`.

## 6. Publishing: I need your answers before U-5
`gh` is logged in as `xx-VIRUS-xx`; there is no git remote yet. Pushing is outward-facing, so I will not do it until you confirm:
1. **Repository name and visibility.** Decided: `serverlab`, **public**.
2. **License.** Default: MIT (only matters if it is public).
3. **Author email.** Every commit currently carries `<author email>`. For a public repo that address becomes visible. Keep it, or switch to your GitHub noreply address? (Changing it rewrites history, which is fine before the first push.)
4. **What to push.** Default: `main` only; not the 14 local worker branches or worktrees. I will remove the now-unused tool-specific files (`.agent/`, `.github/copilot-instructions.md`) and keep `collab/` as a record of how it was built.
5. Week 4: the main pool experiment is still running in the background and will be committed with its results when it finishes. Results directories stay out of git (`results/` is ignored); I will commit a short summary of Week 4 findings and the evidence tables in `docs/phases/week4.md`.

## 7. Open questions (defaults apply if you say "go")
1. Port 8765, local only: OK?
2. Single-file UI with no external assets: OK?
3. Answers to section 6, or "use the defaults".

## 8. Evidence

- `uv run pytest -q tests/ui`: 12 passed (host guard, token on POST, path traversal, spec confinement, one job at a time, pasted-YAML validation, a real CLI job, results from earlier versions, SDK).
- Real data: all 14 result folders in `results/` load through `GET /api/results/{experiment}/{timestamp}` (a worker found 11 returning HTTP 500 on the first version; fixed and covered by a regression test).
- Headless-Chrome screenshots (dark theme, 1280 px) of the Results list, a sweep detail with charts, and a run detail with invalid reasons were inspected by the lead; the worker also checked light, dark and phone widths and the `#predictions` and `#results/<experiment>/<timestamp>` routes.
- Not exercised against a live server: Run/Sweep confirmation, Cancel, Open-result button, inline 400/409 messages (no real jobs may run during an experiment).
