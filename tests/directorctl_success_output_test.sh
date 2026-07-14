#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

TMUX_LOG="$TMPDIR_T/tmux.log"
CAPTURE_MODE="$TMPDIR_T/capture-mode"
CAPTURE_COUNT="$TMPDIR_T/capture-count"

cat >"$TMPDIR_T/tmux" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
LOG_PATH="${TMUX_LOG_PATH:?}"
MODE_PATH="${TMUX_CAPTURE_MODE_PATH:?}"
COUNT_PATH="${TMUX_CAPTURE_COUNT_PATH:?}"
cmd="${1:-}"
shift || true
case "$cmd" in
  capture-pane)
    mode="$(cat "$MODE_PATH")"
    count="$(cat "$COUNT_PATH")"
    count=$((count + 1))
    printf '%s\n' "$count" >"$COUNT_PATH"
    case "$mode" in
      success)
        if [ "$count" -lt 2 ]; then
          cat <<'PANE'
old output
Codex
> queued
PANE
        else
          cat <<'PANE'
old output
Working
esc to interrupt
PANE
        fi
        ;;
      director-idle)
        if [ "$count" -lt 2 ]; then
          cat <<'PANE'
old output
──────────────────────────────────────────────────────────────────────────────────────────── Director ──
❯
────────────────────────────────────────────────────────────────────────────────────────────────────────
PANE
        else
          cat <<'PANE'
old output
Working
esc to interrupt
PANE
        fi
        ;;
      director-typing)
        cat <<'PANE'
old output
──────────────────────────────────────────────────────────────────────────────────────────── Director ──
❯ Eric is typing a message
────────────────────────────────────────────────────────────────────────────────────────────────────────
PANE
        ;;
      empty)
        ;;
      capture-fail)
        exit 2
        ;;
      failure)
        cat <<'PANE'
old output
Codex
> queued
PANE
        ;;
      *)
        echo "unexpected capture mode: $mode" >&2
        exit 1
        ;;
    esac
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

reset_tmux_state() {
    : >"$TMUX_LOG"
    printf '%s\n' "$1" >"$CAPTURE_MODE"
    printf '0\n' >"$CAPTURE_COUNT"
}

run_directorctl() {
    PATH="$TMPDIR_T:$PATH" \
    TMUX_LOG_PATH="$TMUX_LOG" \
    TMUX_CAPTURE_MODE_PATH="$CAPTURE_MODE" \
    TMUX_CAPTURE_COUNT_PATH="$CAPTURE_COUNT" \
    PGU_DIRECTORCTL_ENTER_DELAY=0 \
    PGU_DIRECTORCTL_DIRECTOR_TYPING_MAX_ATTEMPTS=0 \
    "$REPO_ROOT/scripts/directorctl" "$@"
}

reset_tmux_state success
send_stdout="$(run_directorctl send pgu-ops:0.0 'hello world')"
if [[ "$send_stdout" != "directorctl: delivered to pgu-ops:0.0 (11 chars)" ]]; then
    echo "FAIL: send success output mismatch: $send_stdout" >&2
    exit 1
fi

send_payloads="$(grep -c -- "-l hello world" "$TMUX_LOG" || true)"
if [ "$send_payloads" -ne 1 ]; then
    echo "FAIL: send did not write the expected direct payload" >&2
    cat "$TMUX_LOG" >&2
    exit 1
fi

reset_tmux_state success
printf 'first line\nsecond line\n' >"$TMPDIR_T/payload.txt"
send_file_stdout="$(run_directorctl send-file pgu-main:0.0 "$TMPDIR_T/payload.txt")"
if [[ "$send_file_stdout" != "directorctl: delivered to pgu-main:0.0 (22 chars, staged via file)" ]]; then
    echo "FAIL: send-file success output mismatch: $send_file_stdout" >&2
    exit 1
fi
if ! grep -q -- "please read /tmp/directorctl_payload\..* (first line) and follow the instructions therein" "$TMUX_LOG"; then
    echo "FAIL: send-file did not use the staged pointer path" >&2
    cat "$TMUX_LOG" >&2
    exit 1
fi

reset_tmux_state director-idle
callback_stdout="$(run_directorctl callback 'ops, please re-check PGU-356')"
if [[ "$callback_stdout" != "directorctl: delivered to pgu-director:0.0 (28 chars)" ]]; then
    echo "FAIL: callback success output mismatch: $callback_stdout" >&2
    exit 1
fi

reset_tmux_state failure
failure_stdout="$TMPDIR_T/failure.stdout"
failure_stderr="$TMPDIR_T/failure.stderr"
if run_directorctl send pgu-ops:0.0 'will not verify' >"$failure_stdout" 2>"$failure_stderr"; then
    echo "FAIL: expected send verification failure to exit non-zero" >&2
    exit 1
fi
if [ -s "$failure_stdout" ]; then
    echo "FAIL: verification failure printed a false success line" >&2
    cat "$failure_stdout" >&2
    exit 1
fi
if ! grep -q -- "warning: pgu-ops:0.0 did not show a submission transition" "$failure_stderr"; then
    echo "FAIL: verification failure did not report the distinct warning" >&2
    cat "$failure_stderr" >&2
    exit 1
fi

for mode in director-typing empty capture-fail; do
    reset_tmux_state "$mode"
    hold_stdout="$TMPDIR_T/$mode.stdout"
    hold_stderr="$TMPDIR_T/$mode.stderr"
    if run_directorctl callback 'do not clobber Eric' >"$hold_stdout" 2>"$hold_stderr"; then
        echo "FAIL: expected $mode director callback to hold delivery" >&2
        exit 1
    fi
    if [ -s "$hold_stdout" ]; then
        echo "FAIL: $mode hold printed a false success line" >&2
        cat "$hold_stdout" >&2
        exit 1
    fi
    if [ -s "$TMUX_LOG" ]; then
        echo "FAIL: $mode hold sent keys into the director pane" >&2
        cat "$TMUX_LOG" >&2
        exit 1
    fi
    if ! grep -q -- "director is typing after 0 attempts; holding delivery" "$hold_stderr"; then
        echo "FAIL: $mode hold did not report held delivery" >&2
        cat "$hold_stderr" >&2
        exit 1
    fi
done

echo "directorctl_success_output_test: ok"
