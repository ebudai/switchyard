#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'if [[ -n "${PGDATA_DIR:-}" && -d "$PGDATA_DIR" ]]; then pg_ctl -D "$PGDATA_DIR" -m fast -w stop >/dev/null 2>&1 || true; fi; rm -rf "$TMPDIR_T"' EXIT

for tool in initdb pg_ctl createdb psql; do
    command -v "$tool" >/dev/null 2>&1 || {
        echo "ticket_board_migration_runner_test: skipped, missing $tool"
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

psql_test() {
    psql -X -v ON_ERROR_STOP=1 -tA "$ADMIN_CONN" "$@"
}

PGDATA_DIR="$TMPDIR_T/pgdata"
SOCKET_DIR="$TMPDIR_T/socket"
MIGRATIONS_DIR="$TMPDIR_T/migrations"
mkdir -p "$SOCKET_DIR" "$MIGRATIONS_DIR"
PORT="$(free_port)"
DBNAME="pgu_migration_runner_test"
ADMIN_CONN="host=$SOCKET_DIR port=$PORT dbname=$DBNAME user=postgres"

cat >"$MIGRATIONS_DIR/001_create_probe.sql" <<'SQL'
CREATE SCHEMA IF NOT EXISTS ticket_board;
CREATE TABLE ticket_board.migration_probe (
    id integer PRIMARY KEY,
    value text NOT NULL
);
INSERT INTO ticket_board.migration_probe (id, value) VALUES (1, 'first');
SQL

cat >"$MIGRATIONS_DIR/002_update_probe.sql" <<'SQL'
UPDATE ticket_board.migration_probe
SET value = value || '+second'
WHERE id = 1;
SQL

initdb -D "$PGDATA_DIR" -A trust --no-locale --username=postgres >/dev/null
pg_ctl -D "$PGDATA_DIR" -o "-k $SOCKET_DIR -p $PORT -h ''" -w start >/dev/null
createdb -h "$SOCKET_DIR" -p "$PORT" -U postgres "$DBNAME"

TICKET_BOARD_ADMIN_DATABASE_URL="$ADMIN_CONN" TICKET_BOARD_MIGRATIONS_DIR="$MIGRATIONS_DIR" \
    "$REPO_ROOT/scripts/ticket-board-migrate" >/dev/null

applied="$(
    psql_test <<'SQL'
SELECT string_agg(version::text || ':' || description, ',' ORDER BY version)
FROM ticket_board.schema_migrations;
SQL
)"
[[ "$applied" == "1:create_probe,2:update_probe" ]] || {
    echo "FAIL: migrations were not recorded correctly: $applied" >&2
    exit 1
}

value="$(psql_test -c "SELECT value FROM ticket_board.migration_probe WHERE id = 1;")"
[[ "$value" == "first+second" ]] || {
    echo "FAIL: migrations did not apply in numeric order: $value" >&2
    exit 1
}

TICKET_BOARD_ADMIN_DATABASE_URL="$ADMIN_CONN" TICKET_BOARD_MIGRATIONS_DIR="$MIGRATIONS_DIR" \
    "$REPO_ROOT/scripts/ticket-board-migrate" >/dev/null

count="$(psql_test -c "SELECT count(*) FROM ticket_board.schema_migrations;")"
[[ "$count" == "2" ]] || {
    echo "FAIL: rerun should not duplicate migration records: $count" >&2
    exit 1
}

value_after_rerun="$(psql_test -c "SELECT value FROM ticket_board.migration_probe WHERE id = 1;")"
[[ "$value_after_rerun" == "first+second" ]] || {
    echo "FAIL: rerun should skip applied migrations: $value_after_rerun" >&2
    exit 1
}

cat >"$MIGRATIONS_DIR/003_late_probe.sql" <<'SQL'
UPDATE ticket_board.migration_probe
SET value = value || '+third'
WHERE id = 1;
SQL

TICKET_BOARD_ADMIN_DATABASE_URL="$ADMIN_CONN" TICKET_BOARD_MIGRATIONS_DIR="$MIGRATIONS_DIR" \
    "$REPO_ROOT/scripts/ticket-board-migrate" >/dev/null

late_applied="$(
    psql_test <<'SQL'
SELECT string_agg(version::text || ':' || description, ',' ORDER BY version)
FROM ticket_board.schema_migrations;
SQL
)"
[[ "$late_applied" == "1:create_probe,2:update_probe,3:late_probe" ]] || {
    echo "FAIL: late migration was not recorded correctly: $late_applied" >&2
    exit 1
}

late_value="$(psql_test -c "SELECT value FROM ticket_board.migration_probe WHERE id = 1;")"
[[ "$late_value" == "first+second+third" ]] || {
    echo "FAIL: late migration did not apply exactly once: $late_value" >&2
    exit 1
}

SOURCE_REPO="$TMPDIR_T/source"
DEPLOY_ROOT="$TMPDIR_T/live"
mkdir -p "$SOURCE_REPO/scripts/ticket_board/migrations"
git init "$SOURCE_REPO" >/dev/null
git -C "$SOURCE_REPO" config user.name Test
git -C "$SOURCE_REPO" config user.email test@example.com
cp "$REPO_ROOT/scripts/ticket-board-migrate" "$SOURCE_REPO/scripts/ticket-board-migrate"
chmod +x "$SOURCE_REPO/scripts/ticket-board-migrate"
printf '#!/usr/bin/env python3\nprint("board")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
chmod +x "$SOURCE_REPO/scripts/ticket-board.py"
cat >"$SOURCE_REPO/scripts/ticket_board/migrations/004_deploy_probe.sql" <<'SQL'
UPDATE ticket_board.migration_probe
SET value = value || '+deploy'
WHERE id = 1;
SQL
git -C "$SOURCE_REPO" add scripts/ticket-board-migrate scripts/ticket-board.py scripts/ticket_board/migrations/004_deploy_probe.sql
git -C "$SOURCE_REPO" commit -m "seed deploy migration" >/dev/null

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD TICKET_BOARD_ADMIN_DATABASE_URL="$ADMIN_CONN" \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >/dev/null

deploy_value="$(psql_test -c "SELECT value FROM ticket_board.migration_probe WHERE id = 1;")"
[[ "$deploy_value" == "first+second+third+deploy" ]] || {
    echo "FAIL: service deploy did not apply pending migration: $deploy_value" >&2
    exit 1
}

deploy_applied="$(
    psql_test <<'SQL'
SELECT string_agg(version::text || ':' || description, ',' ORDER BY version)
FROM ticket_board.schema_migrations;
SQL
)"
[[ "$deploy_applied" == "1:create_probe,2:update_probe,3:late_probe,4:deploy_probe" ]] || {
    echo "FAIL: service deploy did not record the pending migration: $deploy_applied" >&2
    exit 1
}

echo "ticket_board_migration_runner_test: ok"
