#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

TMUX_LOG="$TMPDIR_T/tmux.log"
cat >"$TMPDIR_T/tmux" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
LOG_PATH="${TMUX_LOG_PATH:?}"
cmd="${1:-}"
shift || true
case "$cmd" in
  capture-pane)
    cat <<'PANE'
old output
──────────────────────────────────────────────────────────────────────────────────────────── Director ──
❯
────────────────────────────────────────────────────────────────────────────────────────────────────────
Working
PANE
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

LONG_CALLBACK=$'New tickets for you: PGU-86 -- soft file-size limit exceeded: main.cpp (1438 lines); PGU-87 -- soft file-size limit exceeded: renderer.h (1300 lines); PGU-88 -- soft file-size limit exceeded: galaxy_system.h (1378 lines)'

PATH="$TMPDIR_T:$PATH" TMUX_LOG_PATH="$TMUX_LOG" \
    "$REPO_ROOT/scripts/directorctl" callback "$LONG_CALLBACK"

payload_line="$(grep -- '-l' "$TMUX_LOG" | head -n 1 || true)"
if [[ -z "$payload_line" ]]; then
    echo "FAIL: no payload send recorded" >&2
    exit 1
fi
if [[ "$payload_line" == *"please read /tmp/directorctl_payload."* ]]; then
    echo "FAIL: callback still used a pointer payload for the director" >&2
    exit 1
fi
if [[ "$payload_line" == *$'\n'* ]]; then
    echo "FAIL: callback payload still contained newlines" >&2
    exit 1
fi
if [[ ${#payload_line} -gt 230 ]]; then
    echo "FAIL: callback payload was not shortened enough to stay on the direct path" >&2
    printf 'logged payload (%d chars): %s\n' "${#payload_line}" "$payload_line" >&2
    exit 1
fi

echo "directorctl_callback_shortens_director_messages_test: ok"
