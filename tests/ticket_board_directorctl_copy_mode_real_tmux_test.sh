#!/usr/bin/env bash
set -euo pipefail

if ! command -v tmux >/dev/null 2>&1; then
    echo "ticket_board_directorctl_copy_mode_real_tmux_test: skipped (tmux not installed)"
    exit 0
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
PATH_ORIGINAL="$PATH"
TMUX_REAL="$(command -v tmux)"
SERVER="pgu808-copy-mode-$$"
SESSION="pgu808-copy-mode-$$"
TARGET="$SESSION:0.0"
RECEIVED="$TMPDIR_T/received.txt"
CANONICAL_DIRECTORCTL="$TMPDIR_T/bin/directorctl"

cleanup() {
    "$TMUX_REAL" -L "$SERVER" kill-server >/dev/null 2>&1 || true
    rm -rf "$TMPDIR_T"
}
trap cleanup EXIT

mkdir -p "$TMPDIR_T/bin"

cat >"$TMPDIR_T/bin/tmux" <<EOF
#!/usr/bin/env bash
exec "$TMUX_REAL" -L "$SERVER" "\$@"
EOF
chmod +x "$TMPDIR_T/bin/tmux"

cat >"$TMPDIR_T/receiver.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
: >"${RECEIVED:?}"
while IFS= read -r line; do
    printf '%s\n' "$line" >>"$RECEIVED"
    printf 'received:%s\n' "$line"
done
EOF
chmod +x "$TMPDIR_T/receiver.sh"

DIRECTORCTL_INSTALL_PATH="$CANONICAL_DIRECTORCTL" "$REPO_ROOT/scripts/install-directorctl" >/dev/null

RECEIVED="$RECEIVED" "$TMUX_REAL" -L "$SERVER" new-session -d -s "$SESSION" "$TMPDIR_T/receiver.sh"
for _ in $(seq 1 50); do
    if "$TMUX_REAL" -L "$SERVER" has-session -t "$SESSION" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done
"$TMUX_REAL" -L "$SERVER" has-session -t "$SESSION" >/dev/null

"$TMUX_REAL" -L "$SERVER" copy-mode -t "$TARGET"
if [ "$("$TMUX_REAL" -L "$SERVER" display-message -p -t "$TARGET" "#{pane_in_mode}")" != "1" ]; then
    echo "FAIL: isolated tmux pane did not enter copy-mode" >&2
    exit 1
fi

PAYLOAD="copy mode delivery $$"
PATH="$TMPDIR_T/bin:$PATH_ORIGINAL" \
    DIRECTORCTL_ENTER_DELAY=0 \
    "$CANONICAL_DIRECTORCTL" send "$TARGET" "$PAYLOAD" >/dev/null

for _ in $(seq 1 50); do
    if grep -Fx -- "$PAYLOAD" "$RECEIVED" >/dev/null 2>&1; then
        break
    fi
    sleep 0.1
done

if ! grep -Fx -- "$PAYLOAD" "$RECEIVED" >/dev/null 2>&1; then
    echo "FAIL: directorctl reported success but pane did not receive payload" >&2
    "$TMUX_REAL" -L "$SERVER" capture-pane -p -J -t "$TARGET" >&2 || true
    exit 1
fi

if [ "$("$TMUX_REAL" -L "$SERVER" display-message -p -t "$TARGET" "#{pane_in_mode}")" != "0" ]; then
    echo "FAIL: directorctl left pane in copy-mode" >&2
    exit 1
fi

echo "ticket_board_directorctl_copy_mode_real_tmux_test: ok"
