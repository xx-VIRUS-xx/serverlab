# Week 2 phase plan: experiment framework

Status: **Approved 2026-10-14 with the defaults in section 3 and 8. In progress.**
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
(none yet)

## 10. Evidence
(filled after implementation)
