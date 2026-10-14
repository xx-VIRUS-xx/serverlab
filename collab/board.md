# Board

Owner of this file: Claude. Agents read only.

Week 1 plan approved 2026-10-14. L-2 merged. Worktrees: ../ServerLabs-wt/<task-id> on branch ag/<id> or cp/<id>; run `uv sync` and `git config core.hooksPath .githooks` in yours.

| ID | Title | Owner | Status | Depends on | Branch |
|---|---|---|---|---|---|
| L-1 | Week 1 phase plan | claude | merged | - | main |
| L-2 | Repo scaffold: pyproject, package skeleton, `LoadAdapter` protocol, spec models | claude | merged | - | main |
| L-3 | Single-condition `serverlab run` + runner lifecycle | claude | blocked | L-2, A-1, C-1, C-2 | main |
| L-4 | Review and merge all Week 1 tasks | claude | blocked | all | main |
| C-1 | Environment capture `env/detect.py` + tests | copilot | in-progress | L-2 | cp/C-1 |
| C-2 | Compose topology generation with cpuset split + tests | copilot | merged | L-2 | cp/C-2 |
| C-3 | Synthetic target server (sleep + semaphore, known capacity) | copilot | merged | L-2 | cp/C-3 |
| A-1 | k6 adapter: script gen, run, summary parse, validity check | antigravity | merged | L-2 | ag/A-1 |
| A-2 | HDR export from k6 per-request output + tests | antigravity | ready | A-1 | ag/A-2 |
| A-3 | Invalid-run test: starved VUs must yield `invalid` | antigravity | ready | A-1 | ag/A-3 |

Task detail: `collab/tasks/<ID>.md`.
