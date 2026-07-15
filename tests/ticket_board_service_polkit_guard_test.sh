#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

SOURCE_REPO="$TMPDIR_T/source"
DEPLOY_ROOT="$TMPDIR_T/live"
SYSTEM_UNIT_PATH="$TMPDIR_T/systemd/pgu-ticket-board.service"
MOCKDIR="$TMPDIR_T/mockbin"
LOGFILE="$TMPDIR_T/systemctl.log"
mkdir -p "$MOCKDIR" "$(dirname "$SYSTEM_UNIT_PATH")"
: >"$LOGFILE"

git init "$SOURCE_REPO" >/dev/null
git -C "$SOURCE_REPO" config user.name Test
git -C "$SOURCE_REPO" config user.email test@example.com
mkdir -p "$SOURCE_REPO/scripts"
printf '#!/usr/bin/env python3\nprint("board")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
chmod +x "$SOURCE_REPO/scripts/ticket-board.py"
git -C "$SOURCE_REPO" add scripts/ticket-board.py
git -C "$SOURCE_REPO" commit -m "seed" >/dev/null

touch "$SYSTEM_UNIT_PATH"

cat >"$MOCKDIR/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$TICKET_BOARD_SERVICE_TEST_LOG"
exit 0
EOF
chmod +x "$MOCKDIR/systemctl"

cat >"$MOCKDIR/systemd-run" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'systemd-run %s\n' "$*" >>"$TICKET_BOARD_SERVICE_TEST_LOG"
exit 0
EOF
chmod +x "$MOCKDIR/systemd-run"

cat >"$MOCKDIR/loginctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "list-sessions" && "${2:-}" == "--no-legend" ]]; then
    printf '1 1001 agent \n'
    exit 0
fi
if [[ "${1:-}" == "show-session" ]]; then
    exit 1
fi
echo "unexpected loginctl call: $*" >&2
exit 1
EOF
chmod +x "$MOCKDIR/loginctl"

cat >"$MOCKDIR/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-" ]]; then
    cat >/dev/null
    exit 0
fi
exec /usr/bin/python3 "$@"
EOF
chmod +x "$MOCKDIR/python3"

if PATH="$MOCKDIR:/usr/bin:/bin" \
    TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
    BOARD_ROOT="$DEPLOY_ROOT" \
    SOURCE_REPO="$SOURCE_REPO" \
    DEPLOY_REF=HEAD \
    TICKET_BOARD_SKIP_MIGRATIONS=1 \
    TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >"$TMPDIR_T/out" 2>"$TMPDIR_T/err"; then
    echo "FAIL: deploy-restart should fail fast when Eric has no active graphical session" >&2
    exit 1
fi

grep -q "requires polkit approval from eric's active graphical session" "$TMPDIR_T/err" || {
    echo "FAIL: missing clear no-graphical-session guidance" >&2
    cat "$TMPDIR_T/err" >&2 || true
    exit 1
}
if grep -q '^restart pgu-ticket-board.service$' "$LOGFILE"; then
    echo "FAIL: deploy-restart should not call systemctl restart when no approval path exists" >&2
    cat "$LOGFILE" >&2 || true
    exit 1
fi

cat >"$MOCKDIR/loginctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "list-sessions" && "${2:-}" == "--no-legend" ]]; then
    printf '3 1000 eric seat0\n'
    exit 0
fi
if [[ "${1:-}" == "show-session" && "${3:-}" == "-p" && "${5:-}" == "--value" ]]; then
    case "${4:-}" in
        Active)
            printf 'yes\n'
            ;;
        Remote)
            printf 'no\n'
            ;;
        Type)
            printf 'wayland\n'
            ;;
        *)
            exit 1
            ;;
    esac
    exit 0
fi
exit 1
EOF
chmod +x "$MOCKDIR/loginctl"

cat >"$MOCKDIR/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf '%s\n' "$*" >>"$TICKET_BOARD_SERVICE_TEST_LOG"
if [[ "${1:-}" == "restart" ]]; then
    sleep 2
fi
exit 0
EOF
chmod +x "$MOCKDIR/systemctl"
: >"$LOGFILE"

if PATH="$MOCKDIR:/usr/bin:/bin" \
    TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
    BOARD_ROOT="$DEPLOY_ROOT" \
    SOURCE_REPO="$SOURCE_REPO" \
    DEPLOY_REF=HEAD \
    TICKET_BOARD_SKIP_MIGRATIONS=1 \
    TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
    TICKET_BOARD_POLKIT_TIMEOUT_SECONDS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >"$TMPDIR_T/out-timeout" 2>"$TMPDIR_T/err-timeout"; then
    echo "FAIL: deploy-restart should fail when polkit approval times out" >&2
    exit 1
fi

grep -q "timed out waiting for polkit approval" "$TMPDIR_T/err-timeout" || {
    echo "FAIL: missing timeout guidance for polkit approval" >&2
    cat "$TMPDIR_T/err-timeout" >&2 || true
    exit 1
}
grep -q '^restart pgu-ticket-board.service$' "$LOGFILE" || {
    echo "FAIL: timeout test never attempted the plain systemctl restart" >&2
    cat "$LOGFILE" >&2 || true
    exit 1
}

echo "ticket_board_service_polkit_guard_test: ok"
