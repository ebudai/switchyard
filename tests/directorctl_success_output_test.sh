#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

TMUX_LOG="$TMPDIR_T/tmux.log"
CAPTURE_MODE="$TMPDIR_T/capture-mode"
CAPTURE_COUNT="$TMPDIR_T/capture-count"
PANE_IN_MODE="$TMPDIR_T/pane-in-mode"
CANONICAL_DIRECTORCTL="$TMPDIR_T/bin/directorctl"
: >"$TMUX_LOG"

cat >"$TMPDIR_T/tmux" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
LOG_PATH="${TMUX_LOG_PATH:?}"
MODE_PATH="${TMUX_CAPTURE_MODE_PATH:?}"
COUNT_PATH="${TMUX_CAPTURE_COUNT_PATH:?}"
PANE_IN_MODE_PATH="${TMUX_PANE_IN_MODE_PATH:?}"
cmd="${1:-}"
shift || true
case "$cmd" in
  display-message)
    if [ "${*: -1}" = "#{pane_in_mode}" ]; then
      cat "$PANE_IN_MODE_PATH"
      exit 0
    fi
    echo "unexpected tmux display-message args: $*" >&2
    exit 1
    ;;
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
        if [ "$count" -lt 2 ]; then
          cat <<'PANE'
old output
──────────────────────────────────────────────────────────────────────────────────────────── Director ──
❯ User is typing a message
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
      director-agent-output)
        if [ "$count" -lt 2 ]; then
          cat <<'PANE'
old output
Claude is using a tool
● Running shell command
PANE
        else
          cat <<'PANE'
old output
Working
esc to interrupt
PANE
        fi
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
DIRECTORCTL_INSTALL_PATH="$CANONICAL_DIRECTORCTL" "$REPO_ROOT/scripts/install-directorctl" >/dev/null

reset_tmux_state() {
    : >"$TMUX_LOG"
    printf '%s\n' "$1" >"$CAPTURE_MODE"
    printf '0\n' >"$CAPTURE_COUNT"
    printf '0\n' >"$PANE_IN_MODE"
}

run_directorctl() {
    PATH="$TMPDIR_T:$PATH" \
    TMUX_LOG_PATH="$TMUX_LOG" \
    TMUX_CAPTURE_MODE_PATH="$CAPTURE_MODE" \
    TMUX_CAPTURE_COUNT_PATH="$CAPTURE_COUNT" \
    TMUX_PANE_IN_MODE_PATH="$PANE_IN_MODE" \
    DIRECTORCTL_ENTER_DELAY=0 \
    DIRECTORCTL_DIRECTOR_TYPING_MAX_ATTEMPTS=0 \
    "$CANONICAL_DIRECTORCTL" "$@"
}

help_stdout="$(run_directorctl --help)"
if grep -q -- "directorctl clear" <<<"$help_stdout" || grep -q -- "clear        Send Ctrl-C" <<<"$help_stdout"; then
    echo "FAIL: directorctl --help still lists unsafe clear subcommand" >&2
    echo "$help_stdout" >&2
    exit 1
fi

clear_stdout="$TMPDIR_T/clear.stdout"
clear_stderr="$TMPDIR_T/clear.stderr"
if run_directorctl clear pgu-main:0.0 >"$clear_stdout" 2>"$clear_stderr"; then
    echo "FAIL: directorctl clear unexpectedly succeeded" >&2
    exit 1
fi
if ! grep -q -- "error: unknown command: clear" "$clear_stderr"; then
    echo "FAIL: directorctl clear did not report unknown command" >&2
    cat "$clear_stderr" >&2
    exit 1
fi
if grep -q -- "C-c" "$TMUX_LOG"; then
    echo "FAIL: directorctl clear sent Ctrl-C" >&2
    cat "$TMUX_LOG" >&2
    exit 1
fi

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
printf '1\n' >"$PANE_IN_MODE"
copy_mode_stdout="$(run_directorctl send pgu-ops:0.0 'copy mode survives')"
if [[ "$copy_mode_stdout" != "directorctl: delivered to pgu-ops:0.0 (18 chars)" ]]; then
    echo "FAIL: copy-mode send success output mismatch: $copy_mode_stdout" >&2
    exit 1
fi
if ! sed -n '1p' "$TMUX_LOG" | grep -q -- "-t pgu-ops:0.0 -X cancel"; then
    echo "FAIL: copy-mode send did not cancel copy-mode before payload" >&2
    cat "$TMUX_LOG" >&2
    exit 1
fi
if ! grep -q -- "-l copy mode survives" "$TMUX_LOG"; then
    echo "FAIL: copy-mode send did not deliver payload after cancel" >&2
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

reset_tmux_state director-agent-output
agent_stdout="$(run_directorctl callback 'agent director should queue this')"
if [[ "$agent_stdout" != "directorctl: delivered to pgu-director:0.0 (32 chars)" ]]; then
    echo "FAIL: markerless agent-output callback did not deliver: $agent_stdout" >&2
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

reset_tmux_state director-typing
typing_stdout="$TMPDIR_T/director-typing.stdout"
typing_stderr="$TMPDIR_T/director-typing.stderr"
if ! run_directorctl callback 'do not starve User escalation' >"$typing_stdout" 2>"$typing_stderr"; then
    echo "FAIL: bounded director typing callback did not eventually deliver" >&2
    cat "$typing_stderr" >&2
    exit 1
fi
if ! grep -q -- "director still appears to be typing after 0 attempts; delivering anyway" "$typing_stderr"; then
    echo "FAIL: bounded director typing callback did not warn" >&2
    cat "$typing_stderr" >&2
    exit 1
fi
if ! grep -q -- "-l do not starve User escalation" "$TMUX_LOG"; then
    echo "FAIL: bounded director typing callback did not send payload" >&2
    cat "$TMUX_LOG" >&2
    exit 1
fi
if [[ "$(cat "$typing_stdout")" != "directorctl: delivered to pgu-director:0.0 (29 chars)" ]]; then
    echo "FAIL: bounded director typing callback success output mismatch" >&2
    cat "$typing_stdout" >&2
    exit 1
fi

reset_tmux_state capture-fail
capture_fail_stdout="$TMPDIR_T/capture-fail.stdout"
capture_fail_stderr="$TMPDIR_T/capture-fail.stderr"
if run_directorctl callback 'capture still fails fast' >"$capture_fail_stdout" 2>"$capture_fail_stderr"; then
    echo "FAIL: expected capture-fail callback to fail verification" >&2
    exit 1
fi
if ! grep -q -- "director still appears to be typing after 0 attempts; delivering anyway" "$capture_fail_stderr"; then
    echo "FAIL: capture-fail callback did not leave the bounded wait" >&2
    cat "$capture_fail_stderr" >&2
    exit 1
fi

echo "directorctl_success_output_test: ok"
