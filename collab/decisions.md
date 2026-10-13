# Decisions

Append only. Owner: Claude. Locked design decisions from `plan.md` s3 apply and are not repeated here.

| # | Date | Decision | Why |
|---|---|---|---|
| D-1 | 2026-10-14 | Agents work on branches; only Claude merges to `main`. | Makes approval a mechanical gate instead of a convention. |
| D-2 | 2026-10-14 | Task status, board and verdicts are plain markdown files in `collab/`. | All three tools can read/write files; no extra infrastructure. |
