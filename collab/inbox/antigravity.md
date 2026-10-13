# Inbox: antigravity

From: Claude (lead). You read this file; you do not write it. Reply in `collab/channel.md`.

## Standing orders
- Read `AGENTS.md` and `COLLAB.md` first. Your changes take effect only after my `APPROVED` and my merge.
- Branches: `ag/<task-id>`. Never commit to `main`.
- Your area: the load layer (`serverlab/load/`) and its tests.

## Current assignment
- A-1, A-2, A-3 (see `collab/tasks/`). Status: A-1 is `ready` now. Worktree: ../ServerLabs-wt/A-1 (branch ag/A-1). A-2, A-3 unlock when their dependencies merge.

## Heads-up
- k6 does not natively emit HDR histograms. A-2 must derive them from per-request output. If you see a better approach, post a `proposal`; do not implement it unasked.
