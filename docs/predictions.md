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

### Week 4 amendment 3: calibration rerun outcome and main-run design (committed before any pool-size data)

Calibration rerun (`results/week4_calibration/20261015T041012Z`, pool 160, 3 interleaved sweeps, same procedure as declared). Host still thrashing: swap 16986 MiB in use (the run's `environment.json` carries the warning).
- p99-onset knees (secondary, ungraded): 217, 67.5 and "above range" (>975). They confirm the p99 knee is unusable on this host.
- Utilization-law fit over 16 valid steps: `A` = 1843 req/s, 95% interval **[896, 3510]**, R^2 0.47 (fixed 28.1% CPU, marginal 390 us/request). The estimate is weak and its interval is wide; it is used as declared, not improved.
- Per the declared rule 4 (estimates differ by more than 1.5x) `A` is the interval [896, 3510]. With the amended pass rule the regimes are:
  - pool 5, 10, 20, 40 (`C_pool` = 100, 200, 400, 800, all below `A_lo` = 896): **pool-limited, graded**: median saturated plateau within 15% of `C_pool`.
  - pool 80 (`C_pool` = 1600, inside [896, 3510]): **ambiguous by construction, not graded**.

Main-run design, fixed now:
- Each pool size gets its own sweep range, `[C_pool/4, 3 x C_pool]` clipped to `[20, 3200]` req/s: pool 5: 25-300, pool 10: 50-600, pool 20: 100-1200, pool 40: 200-2400, pool 80: 400-3200. Reason: in the calibration rerun the pool "saturated" at 320-592 req/s at pool 160 (`C_pool` 3200) because the API itself was overloaded at the top steps (event-loop delay inflates how long a connection is held). The plateau has to be measured just above `C_pool`, not far beyond it.
- 3 sweeps per pool size, interleaved in the seeded randomized-block order; collectors on (container stats, app metrics, `pg_stat_activity`).
- Secondary diagnostic, not graded: the plateau restricted to steps whose SUT container CPU median was below 85% (API not overloaded).
- Stated in advance: if event-loop stalls on this host make the connection hold time longer than 50 ms, `C_meas` falls below `C_pool` and the pass rule can fail. That outcome would be reported as a miss, with the hold-time evidence, not adjusted.

## Week 4 outcome (appended after the main run; everything above was committed before it)

Run: `results/week4_pool/20261015T190907Z` (summaries in `docs/results/week4_pool/`), 15 interleaved sweeps (3 per pool size), command `serverlab sweep experiments/week4_pool.yaml`, graded with `serverlab analyze-pool ... --calibration results/week4_calibration/20261015T041012Z`. Environment warning on the run: host swap 13 002 MiB in use. No sweep step was classified `invalid-other`.

| Pool size n | Predicted `C_pool` = n / 0.050 s | Measured plateau (median of 3 sweep medians) | Error | Regime / grade |
|---|---|---|---|---|
| 5  | 100  | 95   | -5.4%  | pool-limited: **PASS** |
| 10 | 200  | 189  | -5.5%  | pool-limited: **PASS** |
| 20 | 400  | 378  | -5.5%  | pool-limited: **PASS** |
| 40 | 800  | 718  | -10.2% | pool-limited: **PASS** (within 15%; see caveat) |
| 80 | 1600 | 1121 | -30.0% | ambiguous by construction: not graded |

All four graded sizes passed the pre-committed 15% rule. Not graded and not claimed: pool 80.
