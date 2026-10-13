#!/bin/bash
# Lead-side watcher: one line per change in the channel, agent branch tips, or task status on a branch.
ROOT=<repo>
cd "$ROOT" || exit 1

snap() {
  echo "channel-lines:$(wc -l < collab/channel.md | tr -d ' ')"
  for b in $(git for-each-ref --format='%(refname:short)' refs/heads/ag refs/heads/cp); do
    id=${b#*/}
    s=$(git show "$b:collab/tasks/$id.md" 2>/dev/null | sed -n 's/^status: *//p' | head -1)
    echo "$b tip=$(git rev-parse --short "$b") status=$s"
  done
}

prev=$(snap)
while true; do
  sleep 20
  cur=$(snap)
  if [ "$cur" != "$prev" ]; then
    diff <(echo "$prev") <(echo "$cur") | grep '^>'
    prev=$cur
  fi
done
