#!/usr/bin/env bash
# Tear down a tmux session started for verification. Never deletes artifacts/.
set -euo pipefail
session="${1:-}"
if [[ -n "$session" ]]; then
  if tmux has-session -t "$session" 2>/dev/null; then
    tmux send-keys -t "$session" C-c || true
    sleep 1
    if tmux has-session -t "$session" 2>/dev/null; then
      tmux kill-session -t "$session" || true
    fi
    echo "cleanup: ended tmux session $session"
  else
    echo "cleanup: no tmux session $session"
  fi
else
  echo "cleanup: no tmux session named (artifacts retained)"
fi
# Scratch only
rm -f /tmp/walkietalk-verify-*.wav 2>/dev/null || true
echo "cleanup: artifacts/ left intact"
