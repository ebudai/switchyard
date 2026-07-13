#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'if [[ -n "${PGDATA_DIR:-}" && -d "$PGDATA_DIR" ]]; then pg_ctl -D "$PGDATA_DIR" -m fast -w stop >/dev/null 2>&1 || true; fi; rm -rf "$TMPDIR_T"' EXIT

for tool in initdb pg_ctl createdb psql; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ticket_board_service_roles_install_test: skipped, missing $tool"
        exit 0
    }
done

free_port() {
    python3 - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

PGDATA_DIR="$TMPDIR_T/pgdata"
SOCKET_DIR="$TMPDIR_T/socket"
mkdir -p "$SOCKET_DIR"
PORT="$(free_port)"
DBNAME="pgu_roles_install_test"
ADMIN_CONN="host=$SOCKET_DIR port=$PORT dbname=$DBNAME user=postgres"
AGENT_CONN="host=$SOCKET_DIR port=$PORT dbname=$DBNAME user=pgu_agent"

initdb -D "$PGDATA_DIR" -A trust --no-locale --username=postgres >/dev/null
pg_ctl -D "$PGDATA_DIR" -o "-k $SOCKET_DIR -p $PORT -h ''" -w start >/dev/null
createdb -h "$SOCKET_DIR" -p "$PORT" -U postgres "$DBNAME"

psql -X -v ON_ERROR_STOP=1 "$ADMIN_CONN" -f "$REPO_ROOT/scripts/ticket_board/schema.sql" >/dev/null
psql -X -v ON_ERROR_STOP=1 "$ADMIN_CONN" -c "CREATE ROLE pgu_agent LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;" >/dev/null

if TICKET_BOARD_ADMIN_DATABASE_URL="$AGENT_CONN" RBAC_SQL="$REPO_ROOT/scripts/ticket_board/rbac.sql" \
    "$REPO_ROOT/scripts/ticket-board-service.sh" ensure-roles >"$TMPDIR_T/unprivileged.out" 2>"$TMPDIR_T/unprivileged.err"; then
    echo "FAIL: unprivileged ensure-roles unexpectedly succeeded" >&2
    exit 1
fi
grep -q 'must connect as a PostgreSQL role with CREATEROLE' "$TMPDIR_T/unprivileged.err" || {
    echo "FAIL: unprivileged ensure-roles did not explain the required privilege" >&2
    cat "$TMPDIR_T/unprivileged.err" >&2
    exit 1
}

TICKET_BOARD_ADMIN_DATABASE_URL="$ADMIN_CONN" RBAC_SQL="$REPO_ROOT/scripts/ticket_board/rbac.sql" \
    "$REPO_ROOT/scripts/ticket-board-service.sh" ensure-roles >/dev/null
TICKET_BOARD_ADMIN_DATABASE_URL="$ADMIN_CONN" RBAC_SQL="$REPO_ROOT/scripts/ticket_board/rbac.sql" \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" ensure-roles >/dev/null

roles="$(
    psql -X -v ON_ERROR_STOP=1 -tA "$ADMIN_CONN" <<'SQL'
SELECT string_agg(rolname, ',' ORDER BY rolname)
FROM pg_roles
WHERE rolname IN ('research', 'ticket_board_service', 'ticket_board_listener');
SQL
)"

[[ "$roles" == "research,ticket_board_listener,ticket_board_service" ]] || {
    echo "FAIL: service role bootstrap did not create expected roles: $roles" >&2
    exit 1
}

echo "ticket_board_service_roles_install_test: ok"
