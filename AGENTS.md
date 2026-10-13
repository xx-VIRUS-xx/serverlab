# AGENTS.md: rules for every agent on ServerLab

Three agents work here. **Claude is the lead and sole decision-maker.** Antigravity and Copilot are implementers.

Read in this order before doing anything: `plan.md` -> `agent-prompt.md` -> `COLLAB.md` -> `collab/board.md` -> your inbox `collab/inbox/<your-name>.md`.

## Non-negotiable

1. **Your changes do not take effect until Claude approves them.** Work only on your own branch (`ag/<task-id>` or `cp/<task-id>`), never on `main`. When done, set the task to `review` and stop. Do not merge, do not self-approve, do not edit another agent's task file status.
2. **Only work on tasks assigned to you on `collab/board.md` with status `ready`.** No task = no code. Ideas go to `docs/backlog.md` or `collab/channel.md`.
3. **Stay in the task's `files:` list.** Touching anything else requires a `change-request` message to Claude and a reply of `APPROVED`.
4. **Locked decisions (`plan.md` s3) are not yours to change.** Disagree with evidence in `collab/channel.md`, then wait.
5. **No fabricated numbers**, no expected results in code or docs. No custom load generator. Open-loop k6 only.
6. **Do not guess.** If the spec is ambiguous, post a `question` in the channel and work on something else. Claude answers.
7. **No attribution in commits.** No `Co-Authored-By`, "Generated with" or tool/agent names as contributors. Run `git config core.hooksPath .githooks` once per clone/worktree (the hook rejects such messages). Commits on `main` are blocked for everyone but the lead.
8. Python 3.12, uv, typer, pydantic v2, pytest, type hints, ruff + mypy clean. Run tests before marking `review`.

## Where to read and write coordination files

Worktrees have their own copies of `collab/`, which go stale. The single live copy is in the main checkout:

`<repo>/collab/`

- **Append to** `<repo>/collab/channel.md` (the only file you write there). Never use the worktree copy.
- **Read** the board and your inbox from that same directory.
- Set your task status (`in-progress`, `review`) in your own worktree's `collab/tasks/<ID>.md` and commit it on your branch. The lead's watcher sees it.

## Definition of "ready for review"

- Acceptance checks in the task file all ticked, with real command output pasted under `## Evidence`.
- `ruff`, `mypy`, `pytest` pass.
- Only files in `files:` changed.
- Task `status: review`, and a one-line `review-request` appended to `collab/channel.md`.

Full protocol: `COLLAB.md`.
