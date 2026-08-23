#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT
export TICKET_BOARD_POLKIT_APPROVAL_USER=user

make_source_repo() {
    local source_repo="$1"
    git init "$source_repo" >/dev/null
    git -C "$source_repo" config user.name Test
    git -C "$source_repo" config user.email test@example.com
    mkdir -p "$source_repo/scripts"
    printf '#!/usr/bin/env python3\nprint("old board")\n' >"$source_repo/scripts/ticket-board.py"
    chmod +x "$source_repo/scripts/ticket-board.py"
    git -C "$source_repo" add scripts/ticket-board.py
    git -C "$source_repo" commit -m "old release" >/dev/null
}

commit_new_release() {
    local source_repo="$1"
    printf '#!/usr/bin/env python3\nprint("new board")\n' >"$source_repo/scripts/ticket-board.py"
    chmod +x "$source_repo/scripts/ticket-board.py"
    git -C "$source_repo" add scripts/ticket-board.py
    git -C "$source_repo" commit -m "new release" >/dev/null
}

install_mocks() {
    local mockdir="$1"
    mkdir -p "$mockdir"

    cat >"$mockdir/loginctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "list-sessions" && "${2:-}" == "--no-legend" ]]; then
    printf '3 1000 user seat0\n'
    exit 0
fi
if [[ "${1:-}" == "show-session" && "${3:-}" == "-p" && "${5:-}" == "--value" ]]; then
    case "${4:-}" in
        Active) printf 'yes\n' ;;
        Remote) printf 'no\n' ;;
        Type) printf 'wayland\n' ;;
        *) exit 1 ;;
    esac
    exit 0
fi
exit 1
EOF
    chmod +x "$mockdir/loginctl"

    cat >"$mockdir/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
printf 'systemctl:%s\n' "$*" >>"$TICKET_BOARD_HEALTH_GATE_LOG"
if [[ "${1:-}" == "show" && "${2:-}" == "pgu-ticket-board.service" && "${3:-}" == "-p" && "${4:-}" == "FragmentPath" && "${5:-}" == "--value" ]]; then
    printf '%s\n' "$TICKET_BOARD_HEALTH_GATE_UNIT"
fi
if [[ "${1:-}" == "start" && "${2:-}" == "pgu-ticket-board-canary.service" ]]; then
    # shellcheck source=/dev/null
    source "$TICKET_BOARD_HEALTH_GATE_CANARY_ENV"
    printf 'mock canary boot log\n' >>"$BOARD_CANARY_LOG"
    socket_parent="$(dirname "$BOARD_CANARY_SOCKET")"
    socket_parent_mode="$(stat -c %a "$socket_parent")"
    socket_parent_other="${socket_parent_mode: -1}"
    case "$socket_parent_other" in
        3|7)
            ;;
        *)
            echo "canary socket parent is not writable/traversable by boardsvc: $socket_parent mode $socket_parent_mode" >&2
            exit 1
            ;;
    esac
    case "$BOARD_CANARY_ASSET_DIR" in
        /tmp/*)
            ;;
        *)
            echo "canary asset dir is not under /tmp: $BOARD_CANARY_ASSET_DIR" >&2
            exit 1
            ;;
    esac
fi
EOF
    chmod +x "$mockdir/systemctl"

    cat >"$mockdir/systemd-run" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "systemd-run must not be used for the board deploy canary: $*" >&2
exit 98
EOF
    chmod +x "$mockdir/systemd-run"

    cat >"$mockdir/python3" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "-" ]]; then
    url="${2:-}"
    cat >/dev/null
    printf 'smoke:%s\n' "$url" >>"$TICKET_BOARD_HEALTH_GATE_LOG"
    if [[ -f "${TICKET_BOARD_FAIL_CANARY_SMOKE_FILE:-}" && "$url" == *:19001/* ]]; then
        exit 1
    fi
    if [[ -f "${TICKET_BOARD_FAIL_LIVE_SMOKE_ONCE:-}" && "$url" == *:8770/* ]]; then
        rm -f "$TICKET_BOARD_FAIL_LIVE_SMOKE_ONCE"
        exit 1
    fi
    exit 0
fi
exec /usr/bin/python3 "$@"
EOF
    chmod +x "$mockdir/python3"
}

run_service() {
    local source_repo="$1"
    local deploy_root="$2"
    local skip_socket_verify="${TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY:-1}"
    shift 2
    PATH="$MOCKDIR:/usr/bin:/bin" \
    TICKET_BOARD_HEALTH_GATE_LOG="$LOGFILE" \
    TICKET_BOARD_HEALTH_GATE_UNIT="$SYSTEM_UNIT_PATH" \
    TICKET_BOARD_FAIL_CANARY_SMOKE_FILE="$FAIL_CANARY_FILE" \
    TICKET_BOARD_FAIL_LIVE_SMOKE_ONCE="${TICKET_BOARD_FAIL_LIVE_SMOKE_ONCE:-}" \
    TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
    TICKET_BOARD_SYSTEM_UNIT_HASH_RECORD="$HASH_RECORD" \
    TICKET_BOARD_HEALTH_GATE_CANARY_ENV="$deploy_root/canary.env" \
    TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY="$skip_socket_verify" \
    TICKET_BOARD_SKIP_MIGRATIONS=1 \
    BOARD_CANARY_PORT=19001 \
    BOARD_CANARY_TIMEOUT_SECONDS=1 \
    BOARD_SMOKE_TIMEOUT_SECONDS=1 \
    BOARD_ROOT="$deploy_root" \
    SOURCE_REPO="$source_repo" \
    DEPLOY_REF=HEAD \
        "$REPO_ROOT/scripts/ticket-board-service.sh" "$@"
}

SOURCE_REPO="$TMPDIR_T/source"
DEPLOY_ROOT="$TMPDIR_T/live"
MOCKDIR="$TMPDIR_T/mockbin"
LOGFILE="$TMPDIR_T/health-gate.log"
SYSTEM_UNIT_PATH="$TMPDIR_T/systemd/pgu-ticket-board.service"
HASH_RECORD="$TMPDIR_T/system-unit.sha256"
FAIL_CANARY_FILE="$TMPDIR_T/fail-canary"
mkdir -p "$(dirname "$SYSTEM_UNIT_PATH")"
touch "$SYSTEM_UNIT_PATH"
install_mocks "$MOCKDIR"
make_source_repo "$SOURCE_REPO"

run_service "$SOURCE_REPO" "$DEPLOY_ROOT" deploy >/dev/null
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$SYSTEM_UNIT_PATH"
old_current="$(readlink -f "$DEPLOY_ROOT/current")"
commit_new_release "$SOURCE_REPO"

: >"$LOGFILE"
touch "$FAIL_CANARY_FILE"
if run_service "$SOURCE_REPO" "$DEPLOY_ROOT" deploy-restart >"$TMPDIR_T/canary-fail.out" 2>"$TMPDIR_T/canary-fail.err"; then
    echo "FAIL: deploy-restart should fail when the pre-flight canary fails" >&2
    cat "$LOGFILE" >&2 || true
    exit 1
fi
rm -f "$FAIL_CANARY_FILE"
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$old_current" ]] || {
    echo "FAIL: failed canary changed the live current symlink" >&2
    exit 1
}
grep -q '^systemctl:start pgu-ticket-board-canary.service$' "$LOGFILE" || {
    echo "FAIL: canary did not run through the fixed systemd service" >&2
    cat "$LOGFILE" >&2
    exit 1
}
grep -q '^systemctl:stop pgu-ticket-board-canary.service$' "$LOGFILE" || {
    echo "FAIL: failed canary did not stop the fixed systemd service" >&2
    cat "$LOGFILE" >&2
    exit 1
}
grep -q '^BOARD_CANARY_RELEASE_DIR=' "$DEPLOY_ROOT/canary.env" || {
    echo "FAIL: deploy did not write the canary environment file" >&2
    cat "$DEPLOY_ROOT/canary.env" >&2 || true
    exit 1
}
grep -q '^BOARD_CANARY_ASSET_DIR="/tmp/' "$DEPLOY_ROOT/canary.env" || {
    echo "FAIL: canary environment did not use an explicit temporary assets directory" >&2
    cat "$DEPLOY_ROOT/canary.env" >&2
    exit 1
}
grep -q 'canary: mock canary boot log' "$TMPDIR_T/canary-fail.err" || {
    echo "FAIL: failed canary did not surface captured canary log output" >&2
    cat "$TMPDIR_T/canary-fail.err" >&2
    exit 1
}
if grep -q 'systemctl:restart pgu-ticket-board.service' "$LOGFILE"; then
    echo "FAIL: failed canary should not restart the live service" >&2
    cat "$LOGFILE" >&2
    exit 1
fi

: >"$LOGFILE"
run_service "$SOURCE_REPO" "$DEPLOY_ROOT" deploy-restart >/dev/null
new_current="$(readlink -f "$DEPLOY_ROOT/current")"
[[ "$new_current" != "$old_current" ]] || {
    echo "FAIL: healthy deploy did not advance the current symlink" >&2
    exit 1
}
grep -q 'systemctl:restart pgu-ticket-board.service' "$LOGFILE" || {
    echo "FAIL: healthy deploy did not restart the live service" >&2
    cat "$LOGFILE" >&2
    exit 1
}

printf '#!/usr/bin/env python3\nprint("newer board")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
git -C "$SOURCE_REPO" add scripts/ticket-board.py
git -C "$SOURCE_REPO" commit -m "newer release" >/dev/null
: >"$LOGFILE"
FAIL_ONCE="$TMPDIR_T/fail-live-once"
touch "$FAIL_ONCE"
export TICKET_BOARD_FAIL_LIVE_SMOKE_ONCE="$FAIL_ONCE"
if run_service "$SOURCE_REPO" "$DEPLOY_ROOT" deploy-restart >"$TMPDIR_T/rollback.out" 2>"$TMPDIR_T/rollback.err"; then
    echo "FAIL: deploy-restart should exit nonzero after rolling back a failed post-restart smoke" >&2
    exit 1
fi
unset TICKET_BOARD_FAIL_LIVE_SMOKE_ONCE
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$new_current" ]] || {
    echo "FAIL: post-restart smoke failure did not roll back current" >&2
    exit 1
}
restart_count="$(grep -c 'systemctl:restart pgu-ticket-board.service' "$LOGFILE" || true)"
[[ "$restart_count" == "2" ]] || {
    echo "FAIL: rollback should restart once for deploy and once after restoring previous release, saw $restart_count" >&2
    cat "$LOGFILE" >&2
    exit 1
}

printf '#!/usr/bin/env python3\nprint("socketless board")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
git -C "$SOURCE_REPO" add scripts/ticket-board.py
git -C "$SOURCE_REPO" commit -m "socketless release" >/dev/null
: >"$LOGFILE"
export TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY=0
BOARD_UNIX_SOCKET="$TMPDIR_T/missing-runtime/ticket-board.sock" \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$SYSTEM_UNIT_PATH"
if BOARD_UNIX_SOCKET="$TMPDIR_T/missing-runtime/ticket-board.sock" run_service "$SOURCE_REPO" "$DEPLOY_ROOT" deploy-restart >"$TMPDIR_T/socket-verify.out" 2>"$TMPDIR_T/socket-verify.err"; then
    echo "FAIL: deploy-restart should exit nonzero after rolling back failed socket verification" >&2
    exit 1
fi
unset TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY
grep -q 'post-deploy socket verification failed: missing runtime directory' "$TMPDIR_T/socket-verify.err" || {
    echo "FAIL: socket verification failure was not reported" >&2
    cat "$TMPDIR_T/socket-verify.err" >&2
    exit 1
}
[[ "$(readlink -f "$DEPLOY_ROOT/current")" == "$new_current" ]] || {
    echo "FAIL: post-deploy socket verification failure did not roll back current" >&2
    exit 1
}
restart_count="$(grep -c 'systemctl:restart pgu-ticket-board.service' "$LOGFILE" || true)"
[[ "$restart_count" == "2" ]] || {
    echo "FAIL: socket verification rollback should restart once for deploy and once after restoring previous release, saw $restart_count" >&2
    cat "$LOGFILE" >&2
    exit 1
}

echo "ticket_board_service_health_gate_test: ok"
