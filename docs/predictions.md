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
