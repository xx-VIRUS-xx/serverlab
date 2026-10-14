# Board

Owner of this file: Claude. Agents read only.

Week 1 plan approved 2026-10-14. L-2 merged. Worktrees: ../ServerLabs-wt/<task-id> on branch ag/<id> or cp/<id>; run `uv sync` and `git config core.hooksPath .githooks` in yours.

| ID | Title | Owner | Status | Depends on | Branch |
|---|---|---|---|---|---|
| L-1 | Week 1 phase plan | claude | merged | - | main |
| L-2 | Repo scaffold: pyproject, package skeleton, `LoadAdapter` protocol, spec models | claude | merged | - | main |
| L-3 | Single-condition `serverlab run` + runner lifecycle | claude | merged | L-2, A-1, C-1, C-2 | main |
| L-4 | Review and merge all Week 1 tasks | claude | done | all | main |
| C-1 | Environment capture `env/detect.py` + tests | copilot | merged | L-2 | cp/C-1 |
| C-2 | Compose topology generation with cpuset split + tests | copilot | merged | L-2 | cp/C-2 |
| C-3 | Synthetic target server (sleep + semaphore, known capacity) | copilot | merged | L-2 | cp/C-3 |
| A-1 | k6 adapter: script gen, run, summary parse, validity check | antigravity | merged | L-2 | ag/A-1 |
| A-2 | HDR export from k6 per-request output + tests | antigravity | merged | A-1 | ag/A-2 |
| A-3 | Invalid-run test: starved VUs must yield `invalid` | antigravity | merged | A-1 | ag/A-3 |

Task detail: `collab/tasks/<ID>.md`.
| L-5 | Week 2: ExperimentSpec, seeded block schedule, dry-run | claude | merged | week2 plan approved | main |
| L-6 | Week 2: multi-run runner loop, clean state per run | claude | merged | L-5, C-4 | main |
| C-4 | Week 2: metrics collector | haiku-worker | merged | week2 plan approved | cp/C-4 |
| A-4 | Week 2: collector overhead analysis command | haiku-worker | merged | C-4 | ag/A-4 |
| L-8 | Week 3: stats (bootstrap, effect size, HDR merge) + coverage sim + predictions.md | claude | merged | week3 plan approved | main |
| L-9 | Week 3: knee finder (pure) | claude | merged | week3 plan approved | main |
| C-5 | Week 3: synthetic keep-alive + measured throughput ceiling | haiku-worker | ready | week3 plan approved | cp/C-5 |
| A-5 | Week 3: coarse + bracket sweep driver | haiku-worker | merged | L-9 | ag/A-5 |
| L-10 | Week 3: sweep/validate CLI, k6 cgroup CPU | claude | blocked | L-8, L-9, C-5, A-5 | main |
