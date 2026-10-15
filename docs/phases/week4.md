# Week 4 phase plan: Experiment 1, DB connection pool

Status: **Approved 2026-10-15 with the defaults in section 9; other Docker containers stopped by the owner's instruction. In progress.**
Source: `plan.md` s6 Week 4. Locked decisions s3.1-3.12 apply. The thesis this experiment tests: *with a fixed API, the knee of a DB-bound endpoint is set by pool_size / DB service time (Little's law), until another resource saturates first.*

## 1. Scope

1. A FastAPI target (`targets/api/`) with a configurable DB pool whose endpoint holds one connection for a controlled DB service time.
2. A topology with three disjoint CPU sets: API, Postgres, k6.
3. Environment capture gains power source, Low Power Mode and the list of other running containers.
4. Out-of-band collection of (a) app pool metrics and (b) `pg_stat_activity` wait events.
5. Predictions committed first, then the experiment: knee per pool size in {5, 10, 20, 40, 80}, interleaved and repeated.
6. Report: measured vs predicted knee per pool size with error, pool-wait and DB wait events at the knee.

Out of scope: process-count variation (Week 5), Redis (Week 6), HTML report (Week 7).

## 2. Design

**Endpoint.** `GET /work` acquires a connection from an async pool (`psycopg_pool.AsyncConnectionPool`), runs `SELECT pg_sleep(<service_s>)`, releases it. Default service time 50 ms. Why `pg_sleep`: the connection is held with no DB CPU, which isolates the pool as the only limiter and makes Little's law the exact prediction. The cost: it is not a realistic query. A tuned real query (plan.md s6 allows ~5-10 ms) adds DB CPU and a second possible bottleneck, so it is a follow-up, not the showcase.
**Workers fixed at 1** (one uvicorn process). The total connection count is then exactly `pool_size`, and Postgres `max_connections` is set explicitly to 300 so it is never the limiter.
**Pool timeout 30 s, no `max_waiting` cap**, so excess requests queue instead of failing fast (a fast 5xx would look like "completed" throughput). Errors are still counted and gate the knee (section 4).

**Topology (dev Mac: 4 CPUs in the Docker VM):** API cpu 0, Postgres cpu 1-2, k6 cpu 3. On the canonical Linux host: sets sized there. `CpuAssignment` generalizes from {sut, load} to named sets.

**Metrics contract (`GET /metrics`, JSON, cumulative counters from `psycopg_pool.get_stats()` plus app counters):**
`pool_min, pool_max, pool_size, pool_available, requests_waiting, requests_num, requests_queued, requests_wait_ms, requests_wait_max` and `in_flight, ok, errors`. The collector diffs counters per interval; pool-wait is derived (`requests_wait_ms` delta / `requests_queued` delta).
**Postgres sampler:** one persistent host-side connection, >= 1 s, counting `wait_event_type, wait_event` of active backends and `state` counts from `pg_stat_activity`.

## 3. Predictions, committed in two stages BEFORE the main runs

1. **Calibration (committed first).** One sweep with a pool never limiting (pool 400, `pg_sleep` 50 ms) finds the API's own ceiling `A` (req/s) on this host. `A` goes into `docs/predictions.md` with its bracket before anything else runs.
2. **Prediction table:** `C_pool(n) = n / 0.050` = 100, 200, 400, 800, 1600 req/s for n = 5, 10, 20, 40, 80, and the predicted knee is `min(C_pool(n), A)`. Pool sizes where `C_pool > A` are predicted to knee at `A` with the bottleneck shifting to API CPU, not the pool. Pass rule: measured knee within 15% of the prediction for each pool size (wider than Week 3: a real stack is noisier than the synthetic one), and for each size the report states whether the pool or the API limited it, with evidence.
Nothing is retuned after seeing data. A miss is reported as a miss with the evidence for why.

## 4. Changes to shared definitions (made now, before any Week 4 data)

- **Errors gate the knee.** A step is `good` only if the error rate (non-2xx, from k6 `http_req_failed`) is <= 1%. Week 3 validation had 0 errors, so its result is unaffected (re-run `validate` to confirm).
- **Interleaving across sweeps.** `serverlab sweep --repetitions N` runs (condition, repetition) sweeps in a seeded randomized-block order (reusing `build_schedule`), so conditions are interleaved at sweep level, not run back-to-back.
- **Spec:** `target.services` (extra containers such as Postgres with image digest, env, cpuset, healthcheck); conditions can override `target_env` for the API (pool size, service time).

## 5. Work split

| Task | Owner | Deliverable | Depends on |
|---|---|---|---|
| C-6 | Haiku worker | `targets/api/` FastAPI app + Dockerfile (pinned) + `/work`, `/healthz`, `/metrics` per contract; tests incl. a real-Postgres integration test | - |
| A-6 | Haiku worker | `PgActivityCollector` (host psycopg, jsonl, >= 1 s, never raises) + tests against a real Postgres container | - |
| C-7 | Haiku worker | `AppMetricsCollector` (polls `/metrics`, writes derived per-interval jsonl) + tests | C-6 contract |
| L-12 | Claude | spec/compose/cpuset generalization, `services`, error gate in knee, `sweep --repetitions`, collectors wired in runner | - |
| L-13 | Claude | calibration -> predictions -> main experiment -> report, evidence | all |
| L-14 | Claude | review and merge | all |

## 6. Tests

Unit: topology with 3 sets (overlap/oversubscription rejected), compose with a DB service and healthcheck, error-gated knee, sweep-level interleaving determinism, collectors with fixtures captured from real tools. Integration: API + Postgres + k6 end to end at a known pool size (1 sweep point) producing metrics files.

## 7. Acceptance checks (executable)

1. `docker compose config` valid for the 3-service topology; digests pinned for API, Postgres and k6.
2. Calibration sweep run and its `A` committed to `docs/predictions.md` before the main run (git history shows the order).
3. Main experiment (5 pool sizes x N repetitions, interleaved) produces per-size knee estimates, brackets, and pool-wait + `pg_stat_activity` evidence at each knee.
4. Report table: predicted vs measured knee, error, limiting resource. Invalid and excluded steps listed with reasons.
5. `serverlab validate --offline` and the Week 3 sweep validation still pass after the shared-definition changes.
6. ruff, mypy, pytest clean; real output pasted here.

## 8. Risks and honest limits

| Risk | Handling |
|---|---|
| Docker Desktop VM cannot pin CPUs reliably and has 4 CPUs | **Decision: the experiment runs on this Mac.** Findings are valid only as relative results on this host and session, tagged `not-cross-machine-comparable`; no absolute capacity claims. The calibration step measures the API ceiling under the same conditions, so predictions account for it. Controls: other containers on the Docker VM stopped (or recorded), laptop plugged in, Low Power Mode off, power/thermal state recorded in `environment.json`, interleaved repetitions. A Linux host can repeat it later. |
| API ceiling `A` is low and masks the larger pools | Expected and planned for: it is the "bottleneck moves" result, predicted and tested, not a failure |
| `pg_sleep` is unrealistic | Stated limitation; real-query variant is backlog |
| Collectors perturb the system | Re-run the Week 2 overhead check with both collectors on; report the number |
| n per size is small (3) | Report estimates and brackets individually; CI only from the n repetitions with the Week 3 bootstrap, flagged as wide |
| Sweep time: 5 sizes x 3 reps x ~12 steps x ~25 s ~ 75 min | Acceptable; run unattended |

## 9. Open questions (defaults apply if you say "go")

1. **Linux host:** none available, so the experiment runs on this Mac (decided). Results are labelled as same-host relative findings. Please stop the other Docker containers during runs.
2. DB service time 50 ms via `pg_sleep`, API workers = 1, `max_connections` = 300?
3. Repetitions per pool size: 3?
4. Pass tolerance 15% around `min(C_pool, A)`?
5. The error-rate gate (<= 1%) added to the knee definition now?

## 9a. Amendment after calibration run 1 (see docs/predictions.md)
Primary estimators changed from the p99-onset knee to (1) the utilization-law API ceiling and (2) the saturated-interval pool throughput plateau; the p99 knee is kept as a secondary, reported, ungraded number. Declared before the calibration rerun and before any pool-size data.

## 10. Deviations
- `max_connections` is 500, not 300: the calibration pool is 400 and must never be refused by Postgres.
- `/healthz` runs one `SELECT 1` at startup and never borrows a pooled connection afterwards (the compose healthcheck would otherwise steal pool capacity every second); `/metrics` zero-fills psycopg_pool counters that were never touched so the collector contract is complete at low load. Both were gaps in my spec, found by checking the worker's real `/metrics` output.
- Calibration uses 3 repetitions of one condition; `A` is the median estimate.
- **Calibration run 1 was inconclusive** (737/281/473 req/s) because the host was swapping heavily (8 GiB Mac, 7.7 GiB Docker VM, swap 16.6/17.4 GB). Reported in full; handling procedure committed to `docs/predictions.md` before any pool-size data. Added host memory/swap capture and environment warnings to `environment.json`.
- One plumbing sweep (`experiments/week4_smoke.yaml`, pool 10, 4 coarse steps) was run to verify collectors and topology before the calibration. It is not part of the evidence.

## 11. Evidence
(filled after implementation)
