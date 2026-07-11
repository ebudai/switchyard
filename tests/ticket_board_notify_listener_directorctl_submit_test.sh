#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

TMUX_LOG="$TMPDIR_T/tmux.log"
CAPTURE_COUNT="$TMPDIR_T/capture-count"
printf '0\n' >"$CAPTURE_COUNT"

cat >"$TMPDIR_T/tmux" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
LOG_PATH="${TMUX_LOG_PATH:?}"
COUNT_PATH="${TMUX_CAPTURE_COUNT_PATH:?}"
cmd="${1:-}"
shift || true
case "$cmd" in
  capture-pane)
    count="$(cat "$COUNT_PATH")"
    count=$((count + 1))
    printf '%s\n' "$count" >"$COUNT_PATH"
    if [ "$count" -lt 2 ]; then
      cat <<'PANE'
old output
Codex
> Ready ticket for you: PGU-215 -- Submit
PANE
    else
      cat <<'PANE'
old output
Codex
Working
esc to interrupt
PANE
    fi
    ;;
  send-keys)
    printf '%s\n' "$*" >>"$LOG_PATH"
    ;;
  *)
    echo "unexpected tmux command: $cmd" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$TMPDIR_T/tmux"

python3 - <<'PY' "$REPO_ROOT" "$TMPDIR_T" "$TMUX_LOG" "$CAPTURE_COUNT"
import os
import sys
from pathlib import Path

root = Path(sys.argv[1])
tmpdir = Path(sys.argv[2])
tmux_log = Path(sys.argv[3])
sys.path.insert(0, str(root))

from scripts.ticket_board.notify_listener import DirectorctlSender

payload = "Ready ticket for you: PGU-215 -- Submit"
env = os.environ.copy()
env["PATH"] = f"{tmpdir}:{env['PATH']}"
env["TMUX_LOG_PATH"] = str(tmux_log)
env["TMUX_CAPTURE_COUNT_PATH"] = sys.argv[4]
env["PGU_DIRECTORCTL_ENTER_DELAY"] = "0"

import subprocess

original_run = subprocess.run

def run_with_env(args, **kwargs):
    kwargs.setdefault("env", env)
    return original_run(args, **kwargs)

try:
    subprocess.run = run_with_env
    DirectorctlSender(str(root / "scripts" / "directorctl"))("pgu-ops:0.0", payload)
finally:
    subprocess.run = original_run
PY

payload_sends="$(grep -c -- "-l Ready ticket for you: PGU-215 -- Submit" "$TMUX_LOG")"
enter_sends="$(grep -c -- " Enter$" "$TMUX_LOG")"

if [ "$payload_sends" -ne 1 ]; then
    echo "FAIL: expected one payload send, saw $payload_sends" >&2
    cat "$TMUX_LOG" >&2
    exit 1
fi
if [ "$enter_sends" -ne 1 ]; then
    echo "FAIL: expected one Enter submission, saw $enter_sends" >&2
    cat "$TMUX_LOG" >&2
    exit 1
fi
if [ "$(cat "$CAPTURE_COUNT")" -lt 2 ]; then
    echo "FAIL: directorctl did not verify submit transition" >&2
    exit 1
fi

echo "ticket_board_notify_listener_directorctl_submit_test: ok"
