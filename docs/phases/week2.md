# Week 2 phase plan: experiment framework

Status: **Approved 2026-10-14 with the defaults in section 3 and 8. Implemented and merged; results in section 10.**
Source: `plan.md` s6 Week 2; locked decisions s3.5 (randomized interleaving), s3.3 (out-of-band metrics). Backlog items stay out of scope unless listed in section 8.

## 1. Scope

`serverlab run <spec>` takes a multi-condition spec and:
1. expands conditions x repetitions into a **seeded, randomized, interleaved schedule** (stored before anything runs),
2. executes each scheduled run from a clean state (fresh compose up/down, warmup, run, cooldown),
3. samples SUT metrics **out-of-band** at >= 1 s into `metrics/<run>.jsonl`,
4. keeps every run's result, valid or invalid, and never retries or hides invalid ones.

Out of scope: statistics, knee finding, sweeps (Week 3); bottleneck rules (Week 7); Postgres/Redis targets (Week 4+).

## 2. Interfaces (lead owns)

```python
# experiment/spec.py (replaces RunSpec; single-condition specs are the 1-condition case)
class ConditionSpec(BaseModel):   # name, target_env overrides, optional load overrides (rate)
class ExperimentSpec(BaseModel):  # name, seed, repetitions, target, k6_image, load, cpus,
                                  # warmup_s, cooldown_s, keep_raw, metrics{interval_s>=1}, conditions[]
# experiment/schedule.py
class ScheduledRun(BaseModel):    # index, condition, repetition
def build_schedule(conditions: list[str], repetitions: int, seed: int) -> list[ScheduledRun]
```

```python
# metrics/collector.py (worker task C-4)
class MetricsCollector(Protocol):
    def start(self, run_id: str, out_path: Path) -> None: ...   # begins sampling, returns immediately
    def stop(self) -> CollectorStats: ...                         # flushes; stats = samples, missed, interval
```

## 3. Key decisions (defaults; object if you disagree)

- **Interleaving = randomized blocks.** Each block holds every condition once, shuffled by `random.Random(seed)`. Keeps conditions balanced over time (guards against drift) while still randomizing order, e.g. `A B | B A | A B`. Pure random shuffling of the whole list can put 5 A's before any B.
- **Clean state per run:** `compose down -v` and a fresh `up --wait` for every scheduled run, not just between conditions. Image is built once per experiment; the ID is recorded once.
- **Invalid runs:** recorded, flagged, kept in `schedule.json` realized order; no automatic retry. A report later shows the count per condition.
- **Collector source:** `docker stats` streaming (one process, 1 s). Fields: cpu%, memory, net, block IO, pids per container, host-side timestamps. Cgroup-level detail (ctx switches) arrives in Week 5 when needed.
- **Overhead measurement:** SUT CPU consumed (cgroup `cpu.stat usage_usec`, read via `docker exec` before/after, independent of the collector) with collector off vs on at the same fixed rate, n >= 5 each, interleaved. Pass if the median difference is < 2% of the SUT cpuset's capacity; report the number either way, with the spread.
- **Result layout** follows `plan.md` s5: `results/<exp>/<ts>/{spec.yaml, environment.json, schedule.json, raw/run_<cond>_<n>.{json,hdr}, metrics/run_<cond>_<n>.jsonl}`.
- **Spec migration:** `week1_smoke.yaml` and `week1_starved.yaml` become 1-condition specs; Week 1 tests updated, behaviour unchanged.

## 4. Work split

| Task | Owner | Deliverable | Depends on |
|---|---|---|---|
| L-5 | Claude | ExperimentSpec, `schedule.py`, schedule tests (same seed => same schedule, balance per block, different seed differs), `--dry-run` | - |
| L-6 | Claude | Multi-run runner loop, per-run clean state, `schedule.json`, collector hook, CLI | L-5, C-4 |
| C-4 | Haiku worker | `metrics/collector.py` (docker stats streaming -> jsonl, >= 1 s, never raises into the run, reports missed samples) + mocked tests | - |
| A-4 | Haiku worker | `serverlab overhead` harness script + test: off/on interleaved, cgroup cpu.stat deltas, prints the numbers | C-4, L-6 |
| L-7 | Claude | Review, merge, evidence in this doc | all |

## 5. Tests

- Unit: schedule determinism and balance, spec validation (duplicate condition names, repetitions >= 1, interval >= 1), collector parsing with recorded `docker stats` lines (fixtures captured from the real tool), runner loop with fakes (teardown on failure, invalid runs kept).
- Integration (real k6 + Docker): 2 conditions x 2 repetitions on the synthetic target producing the full layout.

## 6. Acceptance checks (executable)

1. Same spec and seed twice => byte-identical `schedule.json` ordering; different seed => different order (shown with real output).
2. `serverlab run experiments/week2_demo.yaml` (2 conditions x 3 reps of the synthetic target with different `CONCURRENCY_LIMIT`) produces 6 run records, 6 metrics files, a realized-order `schedule.json`, and `docker ps` shows nothing left over.
3. Overhead harness prints the collector's SUT CPU cost with the spread; pass only if < 2%, otherwise report the number and propose a lighter sampler (no tuning of the threshold).
4. ruff, mypy, pytest clean; real output pasted here.

## 7. Risks

| Risk | Handling |
|---|---|
| `docker stats` on Docker Desktop samples the VM, not host pinning | Overhead measured on this host and tagged non-comparable; re-measure on the Linux host before Week 4 |
| Overhead below noise on a laptop | Report spread, n, and the raw numbers; no pass/fail claim if the spread exceeds the effect |
| Per-run compose restart is slow | Accepted; clean state outranks speed |

## 8. Open questions (defaults apply if you just say "go")

1. Randomized blocks as above, rather than one flat shuffle?
2. No automatic retry of invalid runs?
3. Collector via `docker stats` for now, deeper cgroup metrics in Week 5?
4. Default `repetitions: 5` in the demo spec?
5. Should I fold two backlog items into Week 2 (they're small): the Docker-Desktop-on-Linux detection and the git dirty flag in `env/detect.py`? Default: no, stay in scope.

## 9. Deviations
- ExperimentSpec expands to the Week 1 `RunSpec` per condition (`resolve()`), so the runner/compose layers are unchanged.
- Added per-condition `metrics_enabled` override and runner-recorded `sut_cpu_usec` (cgroup `cpu.stat`, read via `docker exec`, independent of the collector) so the overhead check is an ordinary two-condition experiment plus `serverlab overhead`.
- `docker stats` is served by the Docker daemon, so the collector's cost may land on the daemon rather than in the SUT cgroup. The check measures the effect on the SUT (CPU and latency); daemon-side cost is NOT measured.
- Collector frames arrive ~2/s, so written gaps are 1.0-1.5 s, not exactly 1 s.
- Demo spec uses 3 repetitions (acceptance); the ExperimentSpec default stays 5.

## 10. Evidence
Dev Mac, Docker Desktop VM (4 CPUs; SUT cpus 0-1, k6 cpu 2), `not-cross-machine-comparable`.

1. **Schedule determinism.** `serverlab run experiments/week2_demo.yaml --dry-run` twice, same seed: identical `order: limit10 limit5 limit5 limit10 limit10 limit5`. Seeds 1-6 gave 5 distinct orders (seed 99 happened to equal the demo seed; 8 possible orders for 2 conditions x 3 blocks). Unit tests also assert byte-identical `schedule.json`.
2. **Demo run.** `serverlab run experiments/week2_demo.yaml` -> exit 0, 6/6 valid in the stored order, 6 run records, 6 HDR files, 6 metrics jsonl (12-15 samples each, 0 parse errors), compose projects torn down (0 `serverlab` containers, 0 `docker stats` processes left).
3. **Collector overhead.** `serverlab run experiments/week2_overhead.yaml` (80 req/s, capacity 100, 6 reps per condition, interleaved) then `serverlab overhead`:
   - collector-off: SUT utilization median 5.280% (min 4.389, max 6.164), median p99 109.13 ms
   - collector-on: median 5.168% (min 4.409, max 6.944), median p99 109.84 ms
   - difference -0.112 percentage points of SUT cpuset capacity -> **BELOW 2% threshold; on/off ranges overlap, so the difference is indistinguishable from run-to-run noise.**
   Limits: the cpu window includes ~1-2 s of k6 container start-up while the SUT idles (denominator is the nominal 20 s), identical for both conditions; daemon-side cost unmeasured; single host, n=6. Re-measure on the Linux host before Week 4.
4. ruff, mypy clean; 140 tests pass.
