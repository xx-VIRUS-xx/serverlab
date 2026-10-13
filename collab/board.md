# Board

Owner of this file: Claude. Agents read only.

Week 1 gate: tasks stay `blocked` until the human approves `docs/phases/week1.md`.

| ID | Title | Owner | Status | Depends on | Branch |
|---|---|---|---|---|---|
| L-1 | Week 1 phase plan | claude | review (human) | - | main |
| L-2 | Repo scaffold: pyproject, package skeleton, `LoadAdapter` protocol, spec models | claude | blocked | L-1 approved | main |
| L-3 | Single-condition `serverlab run` + runner lifecycle | claude | blocked | L-2, A-1, C-1, C-2 | main |
| L-4 | Review and merge all Week 1 tasks | claude | blocked | all | main |
| C-1 | Environment capture `env/detect.py` + tests | copilot | blocked | L-2 | cp/C-1 |
| C-2 | Compose topology generation with cpuset split + tests | copilot | blocked | L-2 | cp/C-2 |
| C-3 | Synthetic target server (sleep + semaphore, known capacity) | copilot | blocked | L-2 | cp/C-3 |
| A-1 | k6 adapter: script gen, run, summary parse, validity check | antigravity | blocked | L-2 | ag/A-1 |
| A-2 | HDR export from k6 per-request output + tests | antigravity | blocked | A-1 | ag/A-2 |
| A-3 | Invalid-run test: starved VUs must yield `invalid` | antigravity | blocked | A-1, C-3 | ag/A-3 |

Task detail: `collab/tasks/<ID>.md`.
