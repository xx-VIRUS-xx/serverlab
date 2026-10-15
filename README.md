# ServerLab

A reproducible experiment framework that asks one question:

> Given the same workload and the same host, **what changes when the architecture changes, and where does the bottleneck move?**

It does not claim absolute capacity ("this machine does N req/s"). It makes **relative, statistically defended** claims about one host ("pool 40 reached 8x pool 5, 95% bootstrap CI [..], n=3") and refuses to report a number it cannot defend: runs that fail validity checks are kept, flagged and excluded, never hidden.

## Status (read this first)

| Area | State |
|---|---|
| Open-loop load via k6 (container, CPU-pinned), validity checks (dropped iterations, VU ceiling) | built, tested |
| Environment capture (host, Docker VM, memory/swap, power, other containers) with warnings | built, tested |
| Seeded, randomized-block experiment schedule; clean stack per run; collectors (container stats, app pool metrics, `pg_stat_activity`) | built, tested |
| Statistics (HDR merge, seeded bootstrap CIs, effect size), knee finder, coarse-then-bracket sweep, `serverlab validate` | built, validated on a synthetic target of known capacity |
| Experiment 1: DB connection pool (FastAPI + Postgres) | built and run once on one host; see *Results* |
| Local web UI and Python SDK | built |
| Experiment 2 (sync/async/multi-process workers), Experiment 3 (Redis hit ratio), bottleneck rule engine + HTML report, LLM-serving experiment | **not built** (roadmap in `plan.md`, weeks 5-8) |

All numbers in this repository were produced on one macOS laptop with Docker Desktop. That host **cannot pin CPUs reliably** and its results are tagged `not-cross-machine-comparable` by the tool itself. Treat them as same-host relative findings. A native Linux host is the intended canonical environment.

## Quickstart

Requirements: Python 3.12, [uv](https://docs.astral.sh/uv/), Docker with Compose v2. k6 is pulled as a pinned container image; you do not need it installed (only a few integration tests use a local `k6`).

```bash
uv sync --extra ui
uv run serverlab validate --offline        # statistics checks, ~30 s, no Docker
uv run serverlab ui                        # local web UI at http://127.0.0.1:8765
uv run serverlab run experiments/week1_smoke.yaml        # one short real run (needs Docker)
uv run serverlab run experiments/week2_demo.yaml --dry-run   # print the seeded schedule
```

Before a real run: plug the laptop in, turn Low Power Mode off, and close heavy applications. The tool records the host state and prints environment warnings (swap in use, battery, other containers); a thrashing host produces noise that looks like a knee.

## Using it

**Web UI** (`serverlab ui`): lists `experiments/*.yaml`, starts one job at a time with a live log and Cancel, and browses `results/` (environment warnings, schedule, per-run validity, sweep curves with the knee bracket, collector time series). It binds to `127.0.0.1` only, checks the `Host` header, requires a per-session token on every action, and can only run spec files inside `experiments/`.

**SDK:**
```python
from serverlab.sdk import Lab

lab = Lab(results_root="results", experiments_dir="experiments")
spec = lab.load_spec("week2_demo.yaml")
print(lab.plan(spec).order())          # seeded schedule, nothing runs
outcome = lab.run(spec)                # same lifecycle as `serverlab run` (needs Docker)
for row in lab.results(): print(row["experiment"], row["timestamp"], row["invalid"])
```

**CLI:** `run`, `sweep`, `validate [--offline]`, `overhead <results>`, `analyze-pool <results> --calibration <results>`, `ui`.

An experiment is a YAML file: a target image (pinned by digest), optional extra services such as Postgres, named conditions that override environment variables or load, a seed, repetitions and CPU sets. See `experiments/`.

## Method (the rules the tool enforces)

- **Open-loop load only** (k6 `constant-arrival-rate`): a closed loop would hide queueing (coordinated omission). A run with more than 0.5% dropped iterations or that hits `maxVUs` is `invalid` and excluded, with the reason recorded.
- **Randomized, interleaved order** (seeded randomized blocks); a fresh container stack per run; the realized order is stored before anything runs.
- **Latency statistics:** per-run HDR histograms are merged for pooled percentiles; P99s are never averaged; confidence intervals are seeded percentile bootstrap (10 000 resamples); "no significant difference detected" when the interval contains the null.
- **Predictions before experiments:** `docs/predictions.md` is committed before the runs it predicts, with pass rules that are not retuned afterwards. Amendments are written there before new data, with the reason.
- **Honest limits recorded in the data:** CPU-pinning reliability, comparability tag, memory/swap, power state, other containers.

Full design and the locked decisions: [`plan.md`](plan.md). Per-phase plans, deviations and evidence: [`docs/phases/`](docs/phases/).

## Results

Tool validation (synthetic target with known capacity, thresholds committed first): the knee finder recovered capacities of 50, 100 and 200 req/s to within -2.0%, -1.5% and -1.3% (all three brackets contained the true value); bootstrap CI coverage was 0.945 (n=10) and 0.937 (n=20) against a nominal 0.95. Details: `docs/phases/week3.md`.

Collector overhead (dev host): -0.11 percentage points of SUT CPU, indistinguishable from run-to-run noise. Details: `docs/phases/week2.md`.

Experiment 1, DB connection pool (FastAPI + async pool + Postgres, 50 ms `pg_sleep`; predictions committed before the runs, 3 interleaved sweeps per pool size, one Mac under memory pressure):

| Pool size | Predicted capacity `n / 0.05 s` | Measured plateau | Error | Hold time `n / plateau` |
|---|---|---|---|---|
| 5  | 100 req/s  | 95   | -5.4%  | 52.8 ms |
| 10 | 200 req/s  | 189  | -5.5%  | 52.9 ms |
| 20 | 400 req/s  | 378  | -5.5%  | 52.9 ms |
| 40 | 800 req/s  | 718  | -10.2% | 56.5 ms |
| 80 | 1600 req/s | 1121 | -30.0% (not graded) | 69.7 ms |

Capacity scales linearly with the pool up to about 20 connections, with a constant ~52.9 ms hold time (50 ms service plus ~2.9 ms overhead on this stack). Beyond that the API's own event loop becomes the limiter: the hold time rises, and the share of checked-out connections actually inside the database drops from ~90% to 43%. The four graded sizes passed the pre-committed 15% rule; pool 80 was ungraded by those rules and is not a confirmed result. Read the caveats (one host, n=3, the unreliable calibration of the API ceiling, `pg_sleep` is not a real query) in [`docs/phases/week4.md`](docs/phases/week4.md) before quoting any of this. The raw evidence summaries are in [`docs/results/`](docs/results/).

## Repository layout

```
serverlab/          package: cli, sdk, experiment/ (spec, schedule, runner, sweeps), load/ (k6, HDR),
                    topology/ (compose + cpusets), metrics/ (collectors), analysis/ (stats, knee, sweep,
                    capacity, validate), env/ (host capture), ui/ (server + single-file page)
targets/            systems under test: synthetic/ (known capacity), api/ (FastAPI + Postgres pool)
experiments/        declarative YAML specs
docs/               phase plans, predictions, UI API contract
collab/             record of how this was built (task board, reviews, decisions)
tests/              unit + integration tests (integration tests need Docker and k6)
```

## How this was built

The project was developed by a lead engineer agent and implementation workers under a review gate: workers worked on branches, and nothing merged without a recorded review (`collab/reviews/`). The task board and decisions are kept in `collab/` as a record, not as something you need to use.

## License

MIT, see [LICENSE](LICENSE).
