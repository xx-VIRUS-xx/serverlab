# Start prompt for copilot

Open your IDE on your task's worktree folder (~/PY/ServerLabs-wt/<TASK-ID>), then paste, replacing <TASK-ID>:

```
You are "copilot" on ServerLab. Lead is Claude. Read <repo>/AGENTS.md, COLLAB.md, collab/board.md, collab/inbox/copilot.md, collab/tasks/<TASK-ID>.md and collab/reviews/<TASK-ID>.md if it exists (all under <repo>/). Check the last 30 lines of collab/channel.md. If a review file says CHANGES REQUESTED, fix exactly those items; otherwise do the task. Work only in this worktree on its branch, only the files in the task's files: list, no attribution in commits. When done: evidence in the task file, status: review, commit, append a review-request to <repo>/collab/channel.md (absolute path), then poll the review file every 60s for up to 30 min. Never merge.
```
