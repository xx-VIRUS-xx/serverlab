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

### Week 4 addendum: calibration run 1 was inconclusive (committed before any pool-size sweep)

Calibration run 1 (`results/week4_calibration/20261015T020420Z`, pool 400, 3 interleaved sweeps) gave knee estimates 737, 281 and 473 req/s (median 473, spread 2.6x). It is reported in full and is **not** used as `A`. Why it is inconclusive:
- Latency onset moved between sweeps while the API was not saturated: at 975 req/s the API container used about 45-60% of its CPU with no requests waiting for the pool, and the same offered rate was `ok` in one sweep and `saturated` in another. The API's real CPU ceiling only appears at 1700+ req/s (container CPU about 100%).
- Host evidence at that time: 8 GiB physical RAM, Docker VM allocated 7.7 GiB, swap 16.6 GB used of 17.4 GB, 92 MB unused, `kernel_task` at 47% CPU (`top`, `memory_pressure`, `vm.swapusage`). A host that is swapping adds random stalls, which the p99-based knee criterion reads as the knee.
- Pool 400 also made Postgres use 709 MiB (vs 92 MiB at pool 10).

Procedure change, declared now and before any pool-size data exists:
1. The owner reduces memory pressure (quits heavy applications, lowers Docker Desktop memory to about 3 GiB). Environment capture now records host RAM, free memory %, swap in use and emits warnings; any run with warnings is labelled in its output.
2. The calibration pool is 160 instead of 400 (`C_pool` = 3200 req/s, still above the top of the sweep range), to cut Postgres memory.
3. The calibration is repeated exactly once, same procedure (3 sweeps, `A` = median estimate). No further calibration reruns to improve `A`.
4. If the three estimates of the repeat differ by more than 1.5x (max/min), `A` is reported as the interval [min, max], and for pool sizes where `C_pool` exceeds `A_lo` the prediction becomes the interval [min(C_pool, A_lo), min(C_pool, A_hi)]; the pass rule is then "measured knee within 15% of any value in that interval". Predictions for pool sizes whose `C_pool` is below `A_lo` are unchanged.

### Week 4 amendment 2: resource-based estimators (committed before the calibration rerun and before any pool-size data)

Reason: calibration run 1 showed the p99-onset knee is unreliable on this host (estimates 281-737 req/s for one system; the API container was at ~50% CPU where the knee was "found"), because swap stalls inflate p99 at random steps. Two estimators that average over thousands of requests and do not depend on p99 are declared as primary. The p99 knee is still computed and reported as secondary, with its noise.

**Estimator 1, API ceiling `A` (utilization law).** From valid steps with offered rate <= 975 req/s of the calibration sweeps, fit `SUT_CPU_us_per_s = F + d * req_per_s` (least squares, per-step values from the SUT cgroup `usage_usec` and the k6 wall time as the window). `A = (1e6 - F) / d` for one CPU. The interval is the 95% percentile bootstrap over steps (10 000 resamples, seeded). *Exploratory check on the run-1 data only (not used as `A`):* `F` = 20.6% of a CPU, `d` = 504 us/request, `A` = 1577 req/s, interval [1413, 1741] (27 steps, R^2 0.87).

**Estimator 2, pool capacity `C_meas(n)` (Little's law at saturation).** From the app metrics of each pool-size sweep, take every 1-second interval with `pool_available == 0`, `requests_waiting > 0` and no counter reset; `C_meas` for that sweep is the median of `delta_ok / interval_s_actual` over those intervals. One value per sweep repetition; per pool size report the three values, their median and range. Prediction `C_pool(n) = n / 0.050` unchanged.

**Amended pass rule (replaces the "measured knee within 15%" rule; everything else in this section stands):**
1. Pool-limited sizes (predicted `C_pool(n) < A_lo`, the lower interval end): pass if the median `C_meas(n)` is within 15% of `C_pool(n)`.
2. Sizes with `C_pool(n)` above `A_hi`: predicted API-limited. Pass if the pool never saturates in the sweeps' valid steps (pool_available > 0) and the SUT container CPU reaches >= 90% of its CPU where latency inflates. No numeric knee match is claimed.
3. Sizes whose `C_pool(n)` lies inside [A_lo, A_hi] are reported as "ambiguous by construction" and are not graded.
4. The secondary p99 knee per sweep is reported next to each, with its spread; it is not graded.

The interval-prediction procedure from the first addendum (rerun calibration once, pool 160, same procedure) still applies; `A` is taken from the rerun, not run 1. Both are reported.
