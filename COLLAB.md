# COLLAB.md: how three agents work together

Everything is plain files in the repo, so every tool (Claude Code, Antigravity, Copilot) can read and write it. No shared service needed.

## Roles

| Agent | Role | Authority |
|---|---|---|
| Claude | Lead dev, owner of decisions | Assigns tasks, answers questions, approves/rejects, merges to `main`, edits `plan.md` |
| Antigravity | Implementer | Works assigned tasks on branch `ag/<task-id>`; may propose, never decide |
| Copilot | Implementer | Works assigned tasks on branch `cp/<task-id>`; may propose, never decide |
| Human (owner) | Approves phase plans | Final say on `docs/phases/weekN.md` |

## Files

```
AGENTS.md                      shared rules (all agents read first)
COLLAB.md                      this protocol
collab/board.md                the single source of task status (Claude edits status; agents propose via channel)
collab/tasks/<ID>.md           one file per task: scope, files, interface, acceptance, evidence
collab/channel.md              append-only message log between agents
collab/inbox/<agent>.md        Claude's directed instructions to each agent (agents read, don't write)
collab/reviews/<ID>.md         Claude's review verdict per task
collab/decisions.md            append-only decision log (ADR-lite); Claude only
```

## Task lifecycle

```
blocked -> ready -> in-progress -> review -> approved -> merged
                                      |
                                      +-> changes-requested -> in-progress
```

- `blocked`: waiting on a phase-plan approval or another task.
- `ready`: Claude released it. Only now may the assignee start.
- `in-progress`: assignee sets this in their task file's frontmatter when starting.
- `review`: assignee sets this when done; appends a `review-request` to the channel.
- `approved` / `changes-requested`: **Claude only**, written in `collab/reviews/<ID>.md` and mirrored on the board.
- `merged`: Claude merges the branch into `main`. Nothing else counts as "taking effect".

## Channel format (`collab/channel.md`, append only, newest at bottom)

```
### 2026-10-14 14:02 | from: antigravity | to: claude | type: question | task: A-1
Body, 1-5 lines. Reference files as path:line.
```

Types: `question`, `review-request`, `change-request`, `blocker`, `info`, `proposal`.
Claude replies as a new entry with `type: answer` or `type: verdict`. Never edit or delete old entries.

## Review gate (what Claude checks)

1. Diff touches only the task's `files:`.
2. Acceptance checks reproduced by Claude (not just trusted from pasted output).
3. Locked decisions intact; no fabricated numbers; no load generator.
4. ruff / mypy / pytest clean.
5. Interface matches `collab/tasks/<ID>.md` exactly (other tasks depend on it).

Verdict is `APPROVED` or `CHANGES REQUESTED` with a numbered list. Only after `APPROVED` does Claude merge.

## Conflict rules

- Two agents never own the same file. Ownership is in each task's `files:`.
- Shared interfaces live in `serverlab/load/base.py` and `serverlab/experiment/spec.py`, which Claude owns. Need a change? `change-request`.
- Disagreement between agents: both post positions in the channel; Claude decides and logs it in `collab/decisions.md`. Decision is final unless the human overrides.

## Polling convention

Agents have no push channel. At the start of every session and before starting each task, an agent must re-read: `collab/inbox/<name>.md`, `collab/board.md`, and the last 30 lines of `collab/channel.md`.
