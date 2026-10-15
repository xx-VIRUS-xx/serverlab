#!/bin/bash
# Usage: scripts/setup-worktree.sh <TASK-ID>   e.g. C-1, A-2
# Creates (or refreshes) ../ServerLabs-wt/<ID> on branch ag/<ID> or cp/<ID>, syncs deps, ensures hooks.
set -euo pipefail
ID=$1
case $ID in A-*) P=ag;; C-*|U-*) P=cp;; *) echo "task id must start with A- or C-"; exit 1;; esac
ROOT=<repo>
WT=$ROOT/../ServerLabs-wt/$ID
cd "$ROOT"
git config core.hooksPath .githooks
if [ ! -d "$WT" ]; then
  git show-ref --verify --quiet refs/heads/$P/$ID && git worktree add -q "$WT" $P/$ID || git worktree add -q -b $P/$ID "$WT" main
fi
cd "$WT"
if [ -z "$(git status --porcelain)" ] && [ "$(git rev-list --count main..HEAD)" = "0" ]; then git merge -q --ff-only main; fi
uv sync -q
echo "ready: $(pwd)  branch $(git rev-parse --abbrev-ref HEAD) @ $(git rev-parse --short HEAD)"
