#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

SOURCE_REPO="$TMPDIR_T/source"
DEPLOY_ROOT="$TMPDIR_T/live"
UNIT_DIR="$TMPDIR_T/unit"
mkdir -p "$UNIT_DIR"

git init "$SOURCE_REPO" >/dev/null
git -C "$SOURCE_REPO" config user.name Test
git -C "$SOURCE_REPO" config user.email test@example.com
mkdir -p "$SOURCE_REPO/deploy/systemd" "$SOURCE_REPO/scripts"
printf '#!/usr/bin/env python3\nprint("board")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
cat >"$SOURCE_REPO/deploy/systemd/pgu-ticket-board.service.boardsvc" <<'EOF'
[Unit]
Description=PGU Ticket Board Test boardsvc Unit

[Service]
Type=simple
User=boardsvc
WorkingDirectory=/home/agent/pgu-ticketboard-live/current
RuntimeDirectory=pgu-ticket-board
ExecStart=/usr/bin/python3 /home/agent/pgu-ticketboard-live/current/scripts/ticket-board.py --host 127.0.0.1 --port 8770 --unix-socket /run/pgu-ticket-board/ticket-board.sock --frames /tmp/pgu-frames --assets /home/agent/.claude/pgu-tickets-assets
Environment=PGU_TICKET_BOARD_SOCKET=/run/pgu-ticket-board/ticket-board.sock
Environment=TICKET_BOARD_DATABASE_URL=postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_service

[Install]
WantedBy=multi-user.target
EOF
chmod +x "$SOURCE_REPO/scripts/ticket-board.py"
git -C "$SOURCE_REPO" add deploy/systemd/pgu-ticket-board.service.boardsvc scripts/ticket-board.py
git -C "$SOURCE_REPO" commit -m "seed" >/dev/null

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >/dev/null
deployed_sha="$(git -C "$SOURCE_REPO" rev-parse --verify HEAD^{commit})"

[[ -L "$DEPLOY_ROOT/current" ]] || {
    echo "FAIL: current deploy link missing" >&2
    exit 1
}
[[ "$(cat "$DEPLOY_ROOT/current/.pgu-deploy-sha")" == "$deployed_sha" ]] || {
    echo "FAIL: deploy did not write/activate the expected commit marker" >&2
    exit 1
}
[[ -x "$DEPLOY_ROOT/current/scripts/ticket-board.py" ]] || {
    echo "FAIL: exported board script missing from current deploy" >&2
    exit 1
}
if [[ -d "$DEPLOY_ROOT/current/.git" ]]; then
    echo "FAIL: deploy root should be an export, not a git checkout" >&2
    exit 1
fi

printf 'stale\n' >"$DEPLOY_ROOT/releases/$deployed_sha/.pgu-deploy-sha"
if BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >"$TMPDIR_T/marker.out" 2>"$TMPDIR_T/marker.err"; then
    echo "FAIL: deploy should fail when an existing release has the wrong commit marker" >&2
    exit 1
fi
grep -q 'sha marker mismatch' "$TMPDIR_T/marker.err" || {
    echo "FAIL: stale release marker failure was not reported clearly" >&2
    cat "$TMPDIR_T/marker.err" >&2
    exit 1
}
printf '%s\n' "$deployed_sha" >"$DEPLOY_ROOT/releases/$deployed_sha/.pgu-deploy-sha"

git -C "$SOURCE_REPO" remote add origin "$TMPDIR_T/missing-origin"
if BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >"$TMPDIR_T/fetch.out" 2>"$TMPDIR_T/fetch.err"; then
    echo "FAIL: deploy should fail loudly when git fetch origin fails" >&2
    exit 1
fi
grep -q 'failed to fetch origin' "$TMPDIR_T/fetch.err" || {
    echo "FAIL: fetch failure did not produce a clear stale-deploy error" >&2
    cat "$TMPDIR_T/fetch.err" >&2
    exit 1
}
git -C "$SOURCE_REPO" remote remove origin

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$UNIT_DIR/service.unit"

grep -q "WorkingDirectory=$DEPLOY_ROOT/current" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not point to deploy current link" >&2
    exit 1
}
grep -q "ExecStart=/usr/bin/python3 $DEPLOY_ROOT/current/scripts/ticket-board.py" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit ExecStart did not point to deploy current script" >&2
    exit 1
}
grep -q '^ExecStartPre=/bin/mkdir -p /tmp/pgu-frames$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not recreate the frame directory before start" >&2
    exit 1
}
grep -q '^ExecStartPre=/bin/chmod 1777 /tmp/pgu-frames$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not restore shared frame directory permissions before start" >&2
    exit 1
}
grep -q '^RuntimeDirectory=pgu-ticket-board$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not request a service-owned runtime directory" >&2
    exit 1
}
grep -q -- "--unix-socket /run/pgu-ticket-board/ticket-board.sock" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not expose the local pane Unix socket" >&2
    exit 1
}
grep -q -- "--frames /tmp/pgu-frames" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not pin the shared frame directory explicitly" >&2
    exit 1
}
retired_backend_flag='--store''-backend'
if grep -q -- "$retired_backend_flag" "$UNIT_DIR/service.unit"; then
    echo "FAIL: rendered unit should not pass the removed backend selector option" >&2
    exit 1
fi
grep -q '^Environment=PGU_TICKET_BOARD_SOCKET=/run/pgu-ticket-board/ticket-board.sock$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not export PGU_TICKET_BOARD_SOCKET" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_DATABASE_URL=postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_service$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not bake the service database URL" >&2
    exit 1
}
grep -q 'ensure_database_roles' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: install path does not ensure ticket-board service roles" >&2
    exit 1
}
grep -q 'apply_database_migrations' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service deploy path does not apply ticket-board migrations" >&2
    exit 1
}
grep -q 'ensure-migrations)' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service script does not expose ensure-migrations for bootstrap testing" >&2
    exit 1
}
grep -q 'TICKET_BOARD_ADMIN_DATABASE_URL="$BOARD_ADMIN_DATABASE_URL" "$migration_runner"' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service migration runner does not use the privileged admin database URL for the selected release" >&2
    exit 1
}
grep -q 'BOARD_ADMIN_DATABASE_URL=".*user=postgres' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service installer admin connection does not default to user=postgres" >&2
    exit 1
}
grep -q 'psql -X -v ON_ERROR_STOP=1 "$BOARD_ADMIN_DATABASE_URL" -f "$RBAC_SQL"' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service installer does not apply RBAC SQL with psql" >&2
    exit 1
}
grep -q 'must connect as a PostgreSQL role with CREATEROLE' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service installer lacks a clear RBAC privilege error" >&2
    exit 1
}
grep -q 'smoke_check_http' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service script lacks a post-start smoke check" >&2
    exit 1
}
grep -q 'urllib.request.urlopen(url, timeout=1.0)' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service smoke check does not perform an HTTP request" >&2
    exit 1
}
grep -q 'resolved_service_scope' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service script does not resolve the live service scope" >&2
    exit 1
}
grep -q 'systemctl_system restart "$SERVICE_NAME"' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: deploy-restart does not restart the system service when it is live" >&2
    exit 1
}
grep -q 'systemctl_user restart "$SERVICE_NAME"' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: deploy-restart no longer supports the user-unit path" >&2
    exit 1
}
grep -q 'quiesce_user_shadow_unit' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: system-scope deploy does not quiesce the shadow user unit" >&2
    exit 1
}
grep -q 'ensure-roles)' "$REPO_ROOT/scripts/ticket-board-service.sh" || {
    echo "FAIL: service script does not expose ensure-roles for bootstrap testing" >&2
    exit 1
}

TICKET_BOARD_DATABASE_URL='postgresql:///custom_board?host=/tmp/pgu-pg&user=ticket_board_service' \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$UNIT_DIR/service-custom-db.unit"

grep -q '^Environment=TICKET_BOARD_DATABASE_URL=postgresql:///custom_board?host=/tmp/pgu-pg&user=ticket_board_service$' "$UNIT_DIR/service-custom-db.unit" || {
    echo "FAIL: unit did not bake caller-provided TICKET_BOARD_DATABASE_URL" >&2
    exit 1
}

MOCKDIR="$TMPDIR_T/mockbin"
LOGFILE="$TMPDIR_T/service-scope.log"
HASH_RECORD="$TMPDIR_T/system-unit.sha256"
SYSTEM_UNIT_PATH="$TMPDIR_T/systemd/pgu-ticket-board.service"
mkdir -p "$MOCKDIR" "$(dirname "$SYSTEM_UNIT_PATH")"
: >"$LOGFILE"

cat >"$MOCKDIR/systemctl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "--user" ]]; then
    shift
    printf 'user:%s\n' "$*" >>"$TICKET_BOARD_SERVICE_TEST_LOG"
    exit 0
fi
printf 'system:%s\n' "$*" >>"$TICKET_BOARD_SERVICE_TEST_LOG"
if [[ "${1:-}" == "show" && "${2:-}" == "pgu-ticket-board.service" && "${3:-}" == "-p" && "${4:-}" == "FragmentPath" && "${5:-}" == "--value" ]]; then
    printf '%s\n' "$TICKET_BOARD_SYSTEM_UNIT_PATH"
fi
EOF
chmod +x "$MOCKDIR/systemctl"

cat >"$MOCKDIR/systemd-run" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
echo "systemd-run must not be used for the board deploy canary: $*" >&2
exit 98
EOF
chmod +x "$MOCKDIR/systemd-run"

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

cp "$DEPLOY_ROOT/current/deploy/systemd/pgu-ticket-board.service.boardsvc" "$SYSTEM_UNIT_PATH"
sha256sum "$SYSTEM_UNIT_PATH" | awk '{print $1}' >"$HASH_RECORD"

PATH="$MOCKDIR:/usr/bin:/bin" \
TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
TICKET_BOARD_SYSTEM_UNIT_HASH_RECORD="$HASH_RECORD" \
TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY=1 \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >/dev/null

grep -q '^system:show pgu-ticket-board.service -p FragmentPath --value$' "$LOGFILE" || {
    echo "FAIL: deploy-restart did not inspect the system unit fragment path" >&2
    cat "$LOGFILE" >&2
    exit 1
}
if grep -q '^system:daemon-reload$' "$LOGFILE"; then
    echo "FAIL: code-only deploy-restart should not reload the system service manager" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
grep -q '^system:restart pgu-ticket-board.service$' "$LOGFILE" || {
    echo "FAIL: deploy-restart did not restart the live system unit" >&2
    cat "$LOGFILE" >&2
    exit 1
}
grep -q '^system:stop pgu-ticket-board-canary.service$' "$LOGFILE" || {
    echo "FAIL: deploy-restart did not stop the fixed canary unit" >&2
    cat "$LOGFILE" >&2
    exit 1
}
grep -q '^system:start pgu-ticket-board-canary.service$' "$LOGFILE" || {
    echo "FAIL: deploy-restart did not start the fixed canary unit" >&2
    cat "$LOGFILE" >&2
    exit 1
}
if grep -q '^user:restart pgu-ticket-board.service$' "$LOGFILE"; then
    echo "FAIL: deploy-restart incorrectly restarted the shadow user unit" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
if grep -q 'sudo' "$LOGFILE"; then
    echo "FAIL: deploy-restart should use plain systemctl, not sudo" >&2
    cat "$LOGFILE" >&2
    exit 1
fi
[[ -s "$HASH_RECORD" ]] || {
    echo "FAIL: deploy-restart did not record the live system unit hash" >&2
    exit 1
}

printf 'stale unit\n' >"$SYSTEM_UNIT_PATH"
printf 'stale-unit-hash\n' >"$HASH_RECORD"
: >"$LOGFILE"

PATH="$MOCKDIR:/usr/bin:/bin" \
TICKET_BOARD_SERVICE_TEST_LOG="$LOGFILE" \
TICKET_BOARD_SYSTEM_UNIT_PATH="$SYSTEM_UNIT_PATH" \
TICKET_BOARD_SYSTEM_UNIT_HASH_RECORD="$HASH_RECORD" \
TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY=1 \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_SKIP_MIGRATIONS=1 \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy-restart >/dev/null

grep -q '^RuntimeDirectory=pgu-ticket-board$' "$SYSTEM_UNIT_PATH" || {
    echo "FAIL: deploy-restart did not install the rendered system unit" >&2
    cat "$SYSTEM_UNIT_PATH" >&2
    exit 1
}
grep -q '^User=boardsvc$' "$SYSTEM_UNIT_PATH" || {
    echo "FAIL: deploy-restart did not install the release boardsvc system unit" >&2
    cat "$SYSTEM_UNIT_PATH" >&2
    exit 1
}
grep -q '^system:daemon-reload$' "$LOGFILE" || {
    echo "FAIL: deploy-restart should reload systemd when the unit hash changes" >&2
    cat "$LOGFILE" >&2
    exit 1
}
grep -q '^system:restart pgu-ticket-board.service$' "$LOGFILE" || {
    echo "FAIL: changed-unit deploy-restart did not restart the live system unit" >&2
    cat "$LOGFILE" >&2
    exit 1
}
grep -q '^system:start pgu-ticket-board-canary.service$' "$LOGFILE" || {
    echo "FAIL: changed-unit deploy-restart did not start the fixed canary unit" >&2
    cat "$LOGFILE" >&2
    exit 1
}

echo "ticket_board_service_deploy_test: ok"
