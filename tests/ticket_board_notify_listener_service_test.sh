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
mkdir -p "$SOURCE_REPO/scripts"
printf '#!/usr/bin/env python3\nprint("listener")\n' >"$SOURCE_REPO/scripts/ticket-board-notify-listener"
chmod +x "$SOURCE_REPO/scripts/ticket-board-notify-listener"
git -C "$SOURCE_REPO" add scripts/ticket-board-notify-listener
git -C "$SOURCE_REPO" commit -m "seed" >/dev/null

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" deploy >/dev/null

[[ -L "$DEPLOY_ROOT/current" ]] || {
    echo "FAIL: current deploy link missing" >&2
    exit 1
}
[[ -x "$DEPLOY_ROOT/current/scripts/ticket-board-notify-listener" ]] || {
    echo "FAIL: exported listener script missing from current deploy" >&2
    exit 1
}
if [[ -d "$DEPLOY_ROOT/current/.git" ]]; then
    echo "FAIL: deploy root should be an export, not a git checkout" >&2
    exit 1
fi

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" render-unit >"$UNIT_DIR/service.unit"

grep -q "WorkingDirectory=$DEPLOY_ROOT/current" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not point to deploy current link" >&2
    exit 1
}
grep -q "ExecStart=/usr/bin/python3 $DEPLOY_ROOT/current/scripts/ticket-board-notify-listener" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit ExecStart did not point to deployed listener script" >&2
    exit 1
}
grep -q '^Environment=PGUSER=ticket_board_listener$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not select ticket_board_listener by default" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_DATABASE_URL=postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_listener$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: listener unit did not bake the shared database URL" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_NOTIFY_DATABASE_URL=postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_listener$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: listener unit did not bake the notify database URL" >&2
    exit 1
}
grep -q '^Environment=PGU_TICKET_BOARD_PANE_STATE_DIR=%t/pgu-ticket-board/pane-state$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not set the shared pane hook state directory" >&2
    exit 1
}
grep -q '^Restart=always$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: listener unit should always restart after drops/crashes" >&2
    exit 1
}
if grep -q '^RuntimeMaxSec=' "$UNIT_DIR/service.unit"; then
    echo "FAIL: queue listener should not rely on RuntimeMaxSec recycle" >&2
    exit 1
fi
grep -q 'ensure_database_roles' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener install path does not ensure ticket-board service roles" >&2
    exit 1
}
grep -q 'BOARD_ADMIN_DATABASE_URL=".*user=postgres' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener installer admin connection does not default to user=postgres" >&2
    exit 1
}
grep -q 'psql -X -v ON_ERROR_STOP=1 "$BOARD_ADMIN_DATABASE_URL" -f "$RBAC_SQL"' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener installer does not apply RBAC SQL with psql" >&2
    exit 1
}
grep -q 'must connect as a PostgreSQL role with CREATEROLE' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener installer lacks a clear RBAC privilege error" >&2
    exit 1
}
grep -q 'ensure-roles)' "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" || {
    echo "FAIL: listener script does not expose ensure-roles for bootstrap testing" >&2
    exit 1
}

TICKET_BOARD_NOTIFY_DATABASE_URL='postgresql:///custom_board?host=/tmp/pgu-pg&user=ticket_board_listener' \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" render-unit >"$UNIT_DIR/listener-custom-db.unit"

grep -q '^Environment=TICKET_BOARD_NOTIFY_DATABASE_URL=postgresql:///custom_board?host=/tmp/pgu-pg&user=ticket_board_listener$' "$UNIT_DIR/listener-custom-db.unit" || {
    echo "FAIL: listener unit did not bake caller-provided TICKET_BOARD_NOTIFY_DATABASE_URL" >&2
    exit 1
}

TICKET_BOARD_DATABASE_URL='postgresql:///shared_board?host=/tmp/pgu-pg&user=ticket_board_listener' \
BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" render-unit >"$UNIT_DIR/listener-shared-db.unit"

grep -q '^Environment=TICKET_BOARD_DATABASE_URL=postgresql:///shared_board?host=/tmp/pgu-pg&user=ticket_board_listener$' "$UNIT_DIR/listener-shared-db.unit" || {
    echo "FAIL: listener unit did not honor caller-provided TICKET_BOARD_DATABASE_URL" >&2
    exit 1
}

TICKET_BOARD_DATABASE_URL='postgresql:///shared_board?host=/tmp/pgu-pg&user=ticket_board_listener' python3 - <<'PY'
from scripts.ticket_board.notify_listener import database_url_from_environment

assert database_url_from_environment() == "postgresql:///shared_board?host=/tmp/pgu-pg&user=ticket_board_listener"
PY

echo "ticket_board_notify_listener_service_test: ok"
