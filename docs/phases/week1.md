# Week 1 phase plan: skeleton + one end-to-end run

Status: **DRAFT, awaiting human approval. No implementation code exists.**
Source: `plan.md` s6 Week 1. Locked decisions s3.1-3.4 apply.

## 1. Scope

Deliver one command, `serverlab run <spec>`, for a single condition that:
1. captures the environment,
2. brings up a compose topology with disjoint CPU sets for SUT and k6,
3. runs one fixed-rate open-loop k6 run,
4. writes `environment.json` and `raw/` (summary JSON, HDR histogram, `dropped_iterations`, validity verdict),
5. tears down.

Out of scope this week: multi-condition expansion, randomized schedule, metrics collector, stats, knee finding (Weeks 2-3). Anything else goes to `docs/backlog.md`.

## 2. Interfaces (Claude owns, created in L-2)

```python
# serverlab/load/base.py
class LoadSpec(BaseModel):      # rate, duration_s, pre_allocated_vus, max_vus, url, method, headers, body
class RunResult(BaseModel):     # planned, completed, dropped, peak_vus, latency_ms{p50,p90,p99,max},
                                # error_rate, valid: bool, invalid_reasons: list[str],
                                # summary_path, hdr_path
class LoadAdapter(Protocol):
    def run(self, spec: LoadSpec, out_dir: Path, run_id: str) -> RunResult: ...
```

```python
# serverlab/experiment/spec.py  (Week 1 subset)
class RunSpec(BaseModel):       # name, seed, target{image, env}, load: LoadSpec, cpus{sut, load}, warmup_s, cooldown_s
```

Result paths follow `plan.md` s5 exactly. Week 1 writes one run: `raw/run_<cond>_1.json` and `.hdr`.

## 3. Work split

| Task | Owner | Deliverable |
|---|---|---|
| L-2 | Claude | pyproject (uv, ruff, mypy, pytest), package skeleton, interfaces above |
| L-3 | Claude | runner lifecycle + `serverlab run` |
| C-1 | Copilot | `env/detect.py` |
| C-2 | Copilot | `topology/` cpuset + compose rendering |
| C-3 | Copilot | `targets/synthetic/` |
| A-1 | Antigravity | `load/k6.py` |
| A-2 | Antigravity | `load/hdr.py` |
| A-3 | Antigravity | invalid-run integration test |

Order: L-2 first (unblocks all) -> C-1/C-2/C-3/A-1 in parallel -> A-2, A-3 -> L-3 -> L-4 review/merge.

## 4. Key technical choices

- **Validity (locked s3.2):** invalid if `dropped_iterations > 0.5%` of planned or peak VUs == `maxVUs`. Reasons are specific strings and surface in `raw/*.json`.
- **HDR:** k6 has no native HDR output. Build the histogram from per-request k6 output with `hdrhistogram`, fixed range 1 us to 60 s, 3 significant digits, standard compressed encoding for later merge.
- **Per-request output size:** at high rates this file is large; parse streaming, keep the raw file only if the spec asks (default: delete after HDR written). Open question Q3.
- **Determinism:** the seed fixes the request schedule inputs (key distribution, body choice). Constant-arrival-rate timing is k6's; we record that in metadata rather than claim bit-exact timing.
- **cpuset:** `cpuset` in compose on Linux. On macOS Docker Desktop it is applied inside the VM, so isolation is not trustworthy: recorded as `cpu_pinning_reliable: false`, report tag `not-cross-machine-comparable` (`agent-prompt` rule 8).

## 5. Tests

- Unit: cpuset assignment (overlap/oversubscription), compose render (digest pinning enforced), k6 script snapshot, summary parser on 3 real-output fixtures, HDR round trip, env detect (mocked Linux/macOS).
- Integration: synthetic target + k6 end to end (A-3 plus a valid control run); skipped loudly when k6/Docker are absent.

## 6. Acceptance checks (from plan.md, made executable)

1. `serverlab run experiments/week1_smoke.yaml` exits 0 and produces `environment.json`, `raw/run_*.json` with latency percentiles and `dropped_iterations`, and `raw/run_*.hdr`.
2. Same command with `max_vus` deliberately starved: exit code non-zero for validity, run marked `invalid` with reasons naming dropped iterations/maxVUs.
3. `ruff check`, `mypy`, `pytest` all clean.
4. Real command output pasted into the "Evidence" section below after implementation.

## 7. Risks

| Risk | Handling |
|---|---|
| k6 not installed / Docker not running on the dev Mac | Q1; integration tests skip loudly, never pass silently |
| macOS cannot isolate CPUs | Recorded, tagged, not worked around |
| Per-request output volume | Streaming parse, optional retention (Q3) |
| Three agents touching shared interfaces | Interfaces frozen in L-2; changes only via Claude |

## 8. Open questions (need your answers)

1. **Tooling on this Mac:** are `k6`, `docker`/Docker Desktop and `uv` installed? If not, may I install them, or do you prefer dev-only mocks until the Linux host exists?
2. **Canonical Linux host:** is there a VM yet (provider, core count)? Week 1 runs here are dev-only and tagged accordingly; the CPU split defaults I plan: SUT 4 CPUs, k6 2 CPUs unless you say otherwise.
3. **Raw per-request k6 output:** delete after HDR is built (saves disk), or keep compressed alongside each run?
4. **Git:** `ServerLabs` is not a git repo. The approval gate relies on branches. OK to `git init`, make an initial commit with the current files, and gitignore `results/`?
5. **HDR range/precision:** 1 us to 60 s with 3 significant digits acceptable?
6. **Agents:** confirm Antigravity and Copilot can each be pointed at this repo directory and are allowed to run shell commands (tests, k6, docker). If either can only edit files, I will shift execution-heavy tasks to myself.

## 8a. Answers (decided by lead, delegated by owner)

1. Tooling: installed `k6` 2.3.0 and `uv` 0.13.0 via brew; Docker present (VM: 4 CPUs, ~8 GB).
2. No Linux host yet. Week 1 is dev-only, tagged `not-cross-machine-comparable`. Dev split inside the 4-CPU VM: SUT cpus 0-1, k6 cpu 2, cpu 3 spare. Canonical host sizing decided before Week 4.
3. Raw per-request k6 output: delete after the HDR is built; `keep_raw: true` in the spec retains it gzipped.
4. Git initialised, `main` protected by a local hook (lead only), `results/` ignored. Commits carry no co-author or tool-attribution trailers; a `commit-msg` hook rejects them. Applies to all three agents.
5. HDR: 1 us to 60 s, 3 significant digits.
6. Agent shell access cannot be verified from here. Assumed yes; if an agent reports otherwise it posts a `blocker` and I reassign.

## 9. Deviations

(none yet)

## 10. Evidence

(filled after implementation)
