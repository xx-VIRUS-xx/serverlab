# Predictions (committed BEFORE the experiments they predict)

## Week 3 tool validation (`serverlab validate`)

Committed 2026-10-14, before `validate` has been run.

Synthetic target capacity C = CONCURRENCY_LIMIT / (SERVICE_TIME_MS / 1000):

| Target | CONCURRENCY_LIMIT | SERVICE_TIME_MS | Predicted capacity C (req/s) |
|---|---|---|---|
| s50  | 5  | 100 | 50  |
| s100 | 10 | 100 | 100 |
| s200 | 20 | 100 | 200 |

Pass rules (thresholds are not retuned after seeing data):
1. Knee point estimate within 10% of C for all three targets.
2. C lies inside the reported bracket for at least two of three targets; the count is reported either way.
3. CI coverage of the 95% percentile-bootstrap interval within [0.90, 0.99] for n = 10 and n = 20 (normal and lognormal populations, 2000 simulated experiments each, seeded). n = 5 is reported, not pass/fail.

Known limitation stated in advance: percentile bootstrap is expected to undercover at n = 5.

## Week 4: connection pool experiment (Experiment 1)

Committed 2026-10-15, BEFORE the calibration sweep and before any pool-size sweep has been run on the real stack (the plumbing check `experiments/week4_smoke.yaml` was run once for wiring only).

Setup: FastAPI, 1 uvicorn worker, async psycopg pool (min = max = pool size), each request holds one connection for `pg_sleep(0.050)`. Postgres `max_connections=500`. Dev Mac, `not-cross-machine-comparable`; results are same-host relative findings only.

Prediction, Little's law: pool capacity `C_pool(n) = n / 0.050 s`:

| Pool size n | 5 | 10 | 20 | 40 | 80 |
|---|---|---|---|---|---|
| C_pool (req/s) | 100 | 200 | 400 | 800 | 1600 |

Predicted knee = `min(C_pool(n), A)` where `A` is the API's own ceiling on this host, measured by the calibration sweep (pool 400, never pool-limited; median of 3 interleaved-free repeats). `A` and its bracket will be committed to this file BEFORE the main experiment is run.

Pass rule (not retuned after seeing data): for each pool size the measured knee estimate is within 15% of the predicted knee. For each size the report states whether the pool or the API limited it, using the app pool metrics (`pool_available == 0`, `requests_waiting > 0`) and `pg_stat_activity` (backends in `PgSleep` equal to the pool size) at the knee.

Known expectation stated in advance, from Week 3: the knee estimate sits ~1-2% below the true capacity because the step at an offered rate equal to capacity already queues.
