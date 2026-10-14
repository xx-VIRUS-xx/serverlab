# Week 3 phase plan: statistics, knee finder, tool validation

Status: **Approved 2026-10-14 with the defaults in section 9. Implemented and merged; results in section 11.**
Source: `plan.md` s6 Week 3; locked decisions s3.6 (HDR merge, never average P99), s3.7 (bootstrap CIs), s3.8 (sweep strategy), s3.9 (knee = curve analysis, USL descriptive only), s3.12 (validate the tool before any experiment).

## 1. Scope

1. `analysis/stats.py`: merge HDR histograms, seeded bootstrap CIs (10k resamples), effect size with CI, the "no significant difference" rule.
2. `analysis/knee.py`: knee finder over (offered rate, achieved throughput, P99) points.
3. `analysis/sweep.py`: coarse log-spaced sweep, then bracketed search around the knee, then full-length repeated runs at a few points near it.
4. `serverlab sweep <spec>` and `serverlab validate`.
5. Validation: the knee finder must recover the known capacity of the synthetic target, and the CI machinery must be coverage-checked by simulation.

Out of scope (-> `docs/backlog.md`): USL fitting (descriptive only, needs >= 6 concurrency points; no experiment needs it yet), reports/HTML (Week 7), real Postgres/Redis targets (Week 4+).

## 2. The hard problem: saturation vs invalid runs

Past the knee an open-loop run needs ever more VUs, so it eventually hits `maxVUs` or drops iterations, which our rules mark **invalid** (s3.2). That is the evidence of saturation, but it is also what a starved load generator looks like. Design:

- Sweep steps are classified `ok`, `saturated`, or `invalid-other`. A step is `saturated` only if it is invalid for dropped iterations / maxVUs **and** the k6 container CPU stayed below 80% of its cpuset (so k6 was not the limiter) **and** SUT latency inflated. Anything else invalid is `invalid-other` and is excluded and reported.
- Invalid runs never enter statistics (s3.2). The knee finder uses them only as an upper bracket ("capacity is below this rate"), and every one is listed in the output with its reason.
- The runner therefore records the k6 container's cgroup CPU per run, as it already does for the SUT.
- The full-length repeated runs near the knee (stage 3) must be **valid** to be used for CIs; if they are not, that is reported, not worked around.

## 3. Definitions (fixed before any data, per the no-fabrication rule)

- **Knee** (curve analysis, s3.9): the highest tested offered rate at which both hold: achieved/offered >= 0.95, and P99 <= 2.0 x the P99 of the lowest sweep step. The finder returns the bracket `[last_ok, first_bad]` and a point estimate at the bracket's geometric midpoint after bisection, with the bracket width reported.
- **Bisection:** on the log scale until bracket width <= 5% of the lower bound, max 6 steps.
- **Coarse sweep:** 8 log-spaced rates between `rate_lo` and `rate_hi`, 10 s steps, randomized order (seeded), 1 run each.
- **Bootstrap:** percentile bootstrap, 10 000 resamples, `numpy.random.default_rng(seed)`, 95% interval. BCa is cross-checked against `scipy.stats.bootstrap` in tests only (scipy is a dev dependency).
- **Effect size:** ratio of medians (B/A) with a CI from independently resampling each group, plus the difference of medians. If the ratio CI contains 1 (difference CI contains 0) the output says "no significant difference detected". No normal-theory intervals.
- **Pooled percentiles:** merge per-run HDR histograms (`HdrHistogram.add`) and read percentiles from the merged one; the per-run P99 distribution is reported alongside. P99s are never averaged.

## 4. Validation design (committed before it is run)

- **Predictions:** `docs/predictions.md` gets the known capacities `C = CONCURRENCY_LIMIT / service_time` for the three synthetic targets and the pass rule, committed **before** `validate` is executed.
  Targets: limit 5, 10, 20 at 100 ms => C = 50, 100, 200 req/s.
- **Knee check:** pass if the finder's point estimate is within 10% of C for all three, and C lies inside the reported bracket for at least two of three (a bracket that never contains C would be a bias, reported either way).
- **CI coverage check (no Docker, seeded):** 2000 simulated experiments each at n = 5, 10, 20 for a normal and a lognormal population; pass if empirical coverage of the 95% interval is within [0.90, 0.99] for n = 10 and 20. n = 5 is reported but not pass/fail: percentile bootstrap is known to undercover at very small n and that limitation goes into the methodology doc as measured, not tuned away.
- `serverlab validate` runs both, prints every number, exits non-zero on any failed check. `--offline` runs the coverage and pure-function checks only.

## 5. Work split

| Task | Owner | Deliverable | Depends on |
|---|---|---|---|
| L-8 | Claude | `analysis/stats.py` + tests + coverage simulation + `docs/predictions.md` | - |
| L-9 | Claude | `analysis/knee.py` (pure) + tests on synthetic curves | - |
| C-5 | Haiku worker | Synthetic target: HTTP keep-alive + a throughput check (>= 300 req/s with 1 CPU) | - |
| A-5 | Haiku worker | `analysis/sweep.py`: coarse + bracket search driver with an injected `measure(rate) -> StepResult`, unit-tested against a fake system of known capacity | L-9 interface |
| L-10 | Claude | k6 cgroup CPU in the runner, `serverlab sweep`, `serverlab validate` | L-8, L-9, C-5, A-5 |
| L-11 | Claude | Review, merge, real validation run, evidence | all |

## 6. Tests

- Unit: bootstrap seeded-determinism, CI on known samples, effect-size output text (including the null case), HDR merge vs concatenated samples within precision, knee finder on synthetic curves (clean knee, noisy, no knee in range, knee below range), sweep driver against a fake system (finds C within tolerance, honours max steps, randomizes order by seed).
- Integration (real Docker + k6): `serverlab validate` end to end.

## 7. Acceptance checks (executable)

1. `serverlab validate --offline` passes with real printed coverage numbers.
2. `serverlab validate` passes: knee within 10% of C for C = 50, 100, 200, with brackets and every excluded/invalid step listed.
3. Same seed twice => identical sweep order and identical CI endpoints.
4. ruff, mypy, pytest clean; real output pasted here.

If check 2 fails, the finding is reported as a tool defect with data. Thresholds are not retuned to pass.

## 8. Risks

| Risk | Handling |
|---|---|
| Synthetic target (single-threaded asyncio, `Connection: close`) cannot reach 200 req/s, which would fail validation for the wrong reason | C-5 adds keep-alive and a measured throughput ceiling; if the ceiling is below 1.5 x the largest C, the largest test C is lowered and the reason recorded |
| k6 on 1 CPU becomes the limiter near 200 req/s | k6 CPU recorded per step; `saturated` requires k6 < 80% CPU |
| Dev-Mac noise biases the knee | Validation is a tool check, not a performance claim; repeat on the Linux host before Week 4 |
| `validate` runtime (~10 min) | Acceptable; `--offline` is the quick check |

## 9. Open questions (defaults apply if you say "go")

1. Percentile bootstrap (BCa only cross-checked in tests)? Default yes.
2. USL fitting stays in the backlog this week? Default yes.
3. Synthetic keep-alive (backlog item) pulled into scope because validation depends on it? Default yes.
4. Knee thresholds as in section 3 (0.95 achieved/offered, 2x baseline P99)? Default yes.
5. Add numpy as a runtime dependency and scipy as dev-only? Default yes.

## 10. Deviations
- The sweep driver, knee finder, stats and `serverlab sweep`/`validate` are as planned. Added: per-run k6 container CPU and wall time (`RunResult.k6_cpu_usec`, `k6_wall_s`), recorded from the container's own cgroup via an `sh` wrapper around k6.
- **Measurement bug found by a plumbing run and fixed before validation:** k6 CPU % was first computed over the nominal step duration; past saturation k6 drains VUs for up to 30 s, giving impossible values (450% of one CPU) that wrongly excluded saturated steps. It now uses the k6 container's real wall time. No threshold or definition changed.
- One reduced-range `serverlab sweep` (`experiments/sweep_smoke.yaml`, one condition) was run twice for plumbing before the validation. It is not part of the validation evidence.
- Normal and lognormal coverage rows are the same experiment for a median CI (the interval depends only on ranks, which `exp` preserves). They are listed separately but are not independent evidence.

## 11. Evidence
Dev Mac, Docker Desktop VM (SUT cpus 0-1, k6 cpu 2), `not-cross-machine-comparable`. Thresholds from `docs/predictions.md`, committed before the run, unchanged.

`uv run serverlab validate` -> exit 0 (results: `results/validate_synthetic/20261014T165923Z`, 36 sweep steps, 0 excluded):

| Target | C (req/s) | Bracket | Point estimate | Error | Steps |
|---|---|---|---|---|---|
| s50  | 50  | [48, 50]   | 49.0  | -2.0% | 12 |
| s100 | 100 | [97, 100]  | 98.5  | -1.5% | 12 |
| s200 | 200 | [194, 201] | 197.5 | -1.3% | 12 |

- Rule 1 (within 10%): PASS for all three. Rule 2 (bracket contains C): 3 of 3.
- CI coverage (95% percentile bootstrap, 2000 trials x 10 000 resamples, MC s.e. ~0.005): n=10: 0.945, n=20: 0.937 (both PASS); n=5: 0.930 (reported only). Coverage is slightly below nominal, as expected for a percentile bootstrap at small n.

What this does and does not show:
- The estimates are all 1.3-2.0% **below** C, consistently. At an offered rate equal to C the queue already builds (P99 of 260-297 ms vs ~105 ms baseline), so the criterion marks that step bad. For s50 and s100 the bracket contains C only at its upper edge (hi == C). This is a small systematic bias of the definition, not noise.
- This validates the tool against a deterministic target on one dev host. A real system with jitter and a noisy knee is harder; nothing here is a performance claim.
- The baseline is the P99 of the single lowest-rate step; in s100 that step read 122.7 ms against ~105-112 ms elsewhere, which loosened its threshold to ~245 ms. It did not change any verdict here (rate 100 read 279.8 ms), but a noisy baseline is a real risk. Not changed after seeing the data (pre-registration); logged in `docs/backlog.md` for a decision with more data.
- Runtime: about 15 minutes for the full `validate`; `--offline` about 30 s.
- `ruff`, `mypy` clean; 192 tests pass.
