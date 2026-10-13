# ServerLab: Plan

## 1. Thesis

ServerLab is a reproducible experiment framework for answering one question:

> Given the same workload and the same host, what changes when the architecture changes, and where does the bottleneck move?

It does NOT claim absolute capacity ("this machine does N RPS"). It claims relative, statistically defended findings ("B sustained 2.4x A on this host, 95% bootstrap CI [2.2, 2.6], n=5").

Success criterion for V1: three controlled experiments producing three non-obvious, reproducible findings, plus one LLM-serving experiment as the differentiator. Not "a platform".

## 2. Non-goals (V1)

No web dashboard, no control plane, no Postgres for results, no WebSocket, no cloud agent, no custom load generator, no Kafka/Celery, no failure injection. Revisit only after V1 findings exist.

## 3. Locked design decisions

1. **Open-loop load only.** Wrap k6 (`constant-arrival-rate` / `ramping-arrival-rate`). Adapter interface so wrk2/Vegeta can be added later. Never write a load generator.
2. **Detect coordinated omission.** Fail a run if k6 `dropped_iterations` > 0.5% of planned iterations or VUs hit `maxVUs`. Mark run `invalid`, exclude from stats, log reason.
3. **Host isolation.** Load generator and SUT get disjoint CPU sets (`cpuset` in compose). Metrics collection is lightweight and out-of-band. Canonical results come from a fixed Linux host (dedicated cloud VM); macOS is dev-only and reports are tagged `not-cross-machine-comparable`.
4. **Environment capture is mandatory metadata**: OS, kernel, CPU model, cores, governor, Docker version, Docker VM resources, image digests, runtime versions, CPU allocation, git SHA of the experiment.
5. **Run order is randomized and interleaved** across conditions (A B B A B A...). Seed and realized order stored in results.
6. **Latency stats**: capture per-run HDR histograms; merge histograms for pooled percentiles; for run-to-run variation report the distribution of per-run P99. Never average P99s.
7. **Inference**: bootstrap CIs (BCa or percentile, 10k resamples) because n is small. Report effect size and CI. If CI includes zero: "no significant difference detected". No normal-theory intervals at n<30.
8. **Sweep strategy**: coarse log-spaced arrival-rate sweep (short steps) -> bracketed search around the knee -> full-length repeated runs only at a few points near the knee. Never a dense linear sweep.
9. **Knee detection**: primary = curve analysis (offered vs achieved throughput divergence + P99 inflection). USL fit is descriptive only, requires >= 6 concurrency points, reported with fit residuals.
10. **Bottleneck analyzer = explicit rules over USE metrics** (utilization, saturation, errors per resource). Rules are readable code, each rule emits its evidence. No ML.
11. **No pre-written conclusions** in templates or docs. Findings are written after data exists.
12. **Tool validation precedes experiments.** A synthetic target with known capacity must be recovered by the knee finder and CIs.

## 4. Repo layout

```
serverlab/
  pyproject.toml
  serverlab/
    cli.py                  # typer: run, analyze, report, validate
    env/detect.py           # host + docker + vm capture
    topology/               # compose generation, cpuset assignment
    load/base.py            # LoadAdapter protocol
    load/k6.py              # k6 script gen, run, summary parse, HDR export
    metrics/collector.py    # docker stats / cAdvisor, pg_stat_*, redis INFO
    experiment/spec.py      # pydantic models for YAML
    experiment/runner.py    # lifecycle: setup, warmup, run, cooldown, teardown
    experiment/schedule.py  # randomized interleaving, seeded
    analysis/stats.py       # bootstrap, HDR merge, effect size
    analysis/knee.py        # knee finder, optional USL
    analysis/bottleneck.py  # rule engine
    report/html.py          # static report (jinja2 + inline plotly/svg)
  targets/
    synthetic/              # known-capacity server (sleep + semaphore)
    api/                    # FastAPI SUT with configurable pool/workers/cache
  experiments/
    pool.yaml
    workers.yaml
    redis_hit_ratio.yaml
    llm_serving.yaml
  tests/
  docs/methodology.md
  results/                  # gitignored; sample committed separately
```

## 5. Result layout

```
results/<experiment>/<timestamp>/
  spec.yaml  environment.json  schedule.json
  raw/run_<cond>_<n>.json  raw/run_<cond>_<n>.hdr
  metrics/<run>.jsonl
  analysis.json  report.html
```

Results are immutable once written; re-analysis writes a new `analysis.json` version.

## 6. Week-by-week

### Week 1: Skeleton + one end-to-end run
- Deliver: env capture, compose topology with cpuset split, k6 adapter, one fixed-rate run to JSON, `serverlab run` for a single condition.
- Accept: one command produces `environment.json` + `raw/` with latency histogram and `dropped_iterations`; invalid-run detection works when VUs are deliberately starved.

### Week 2: Experiment framework
- Deliver: YAML spec (pydantic), condition x repetition expansion, seeded randomized interleaving, warmup/cooldown, out-of-band metrics collection, teardown to clean state between runs.
- Accept: re-running with the same seed reproduces the same schedule; metrics sampling overhead measured and < 2% CPU of SUT set.

### Week 3: Statistics + knee finder + validation
- Deliver: HDR merge, bootstrap CIs, effect size, coarse-then-bracket sweep, knee finder, synthetic known-capacity target.
- Accept: on the synthetic target (capacity C = concurrency_limit / service_time) knee estimate lands within 10% of C across 3 different C values; CI coverage sanity-checked by simulation. `serverlab validate` runs this and fails loudly otherwise.

### Week 4: Experiment 1, DB connection pool (methodology showcase)
- Setup: FastAPI + Postgres, query with controlled service time (e.g. `pg_sleep` or a real query tuned to ~5-10 ms), pool sizes 5/10/20/40/80, API workers fixed.
- Before running: write the Little's-law prediction (knee ~= pool_size / DB_service_time) into `docs/predictions.md` and commit it.
- Accept: measured knee vs predicted knee per pool size reported with error; pool-wait metric from app instrumentation and `pg_stat_activity` wait events shown at the knee.

### Week 5: Experiment 2, concurrency model (non-obvious version)
- Setup: mixed workload (I/O wait + CPU burst) comparing sync threads vs async vs multi-process workers on the pinned core set, worker counts 1/2/4/8.
- Accept: report identifies where each model's knee is and why (GIL, event-loop blocking, context switching) with supporting CPU/ctx-switch metrics. Do not treat "throughput flattens at N cores" as a finding.

### Week 6: Experiment 3, Redis hit ratio
- Setup: DB query expensive enough to be the bottleneck (verify DB saturates at 0% hit first, otherwise tune), hit ratio controlled by key distribution + pre-warming, hit ratio 0/25/50/75/90/99.
- Accept: achieved hit ratio verified from Redis `keyspace_hits/misses` and used (not the nominal value) on the x-axis; crossover point where bottleneck shifts from DB to API CPU identified or "no crossover" reported honestly.

### Week 7: Bottleneck analyzer + report
- Deliver: rule engine (CPU saturated, pool wait dominant, DB wait events, event-loop lag, queue depth if present), each rule emits evidence; static `report.html` with methodology, environment, results with CIs, bottleneck evidence, validity warnings.
- Accept: analyzer correctly labels the synthetic target scenarios and the three experiments; report renders offline with no network calls.

### Week 8: LLM-serving experiment + write-up
- Setup: vLLM or SGLang on available hardware, arrival-rate sweep vs TTFT P50/P99 and tokens/s, vary max batch / max-num-seqs or KV-cache fraction; fixed prompt/output length distribution.
- Accept: knee and failure mode (queueing vs KV-cache eviction/preemption) identified with server-side metrics.
- Deliver `docs/methodology.md`, README with exact reproduction steps, one published write-up per finding.

## 7. Risks

| Risk | Mitigation |
|---|---|
| Noisy laptop results | Canonical runs on fixed Linux host; record governor; interleave runs; report CV |
| k6 becomes the bottleneck | Dedicated CPU set; check `dropped_iterations`; validate on synthetic target |
| Experiments show nothing | Report null results with CIs; that is still a finding |
| Scope creep | Anything not in sections 3-6 goes to `docs/backlog.md`, not code |
| Metrics collector perturbs SUT | Measure overhead in Week 2; sample at >= 1 s |

## 8. Definition of done (V1)

- `serverlab validate` passes on a clean checkout.
- Three experiments + LLM experiment reproducible from YAML with one command each.
- Each has a report with CIs, environment, validity checks, and a bottleneck explanation backed by metrics.
- `docs/methodology.md` states every threshold and statistical choice.
