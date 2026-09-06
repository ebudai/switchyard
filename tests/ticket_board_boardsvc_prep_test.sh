#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/ticket-board-boardsvc-setup.sh"
UNIT="$REPO_ROOT/deploy/systemd/pgu-ticket-board.service.boardsvc"
CANARY_UNIT="$REPO_ROOT/deploy/systemd/pgu-ticket-board-canary.service"
TMPFILES="$REPO_ROOT/deploy/tmpfiles/pgu-ticket-board.conf"
POLKIT_RULE="$REPO_ROOT/deploy/polkit/49-pgu-board-deploy.rules"
RUNBOOK="$REPO_ROOT/docs/ticket-board-boardsvc-peer-auth-runbook.md"
TMPDIR_T="$(mktemp -d)"
trap 'if [[ -n "${PGDATA_DIR:-}" && -d "$PGDATA_DIR" ]]; then pg_ctl -D "$PGDATA_DIR" -m fast -w stop >/dev/null 2>&1 || true; fi; rm -rf "$TMPDIR_T"' EXIT

free_port() {
    python3 - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

[[ -x "$SCRIPT" ]] || {
    echo "FAIL: setup script is missing or not executable" >&2
    exit 1
}
bash -n "$SCRIPT"

grep -q 'useradd -r -M -d /nonexistent -s /usr/sbin/nologin' "$SCRIPT" || {
    echo "FAIL: setup script does not create a non-login system user" >&2
    exit 1
}
grep -q 'peer map=\$PG_IDENT_MAP' "$SCRIPT" || {
    echo "FAIL: setup script does not configure peer auth with an ident map" >&2
    exit 1
}
grep -q 'insert_block_before_local_catchall_once' "$SCRIPT" || {
    echo "FAIL: setup script does not place pg_hba auth before local all/all catch-alls" >&2
    exit 1
}
grep -q 'server_version_num' "$SCRIPT" || {
    echo "FAIL: setup script does not gate pg_hba includes by PostgreSQL version" >&2
    exit 1
}
grep -q '\$PG_IDENT_MAP   \$SERVICE_USER   \$SERVICE_ROLE' "$SCRIPT" || {
    echo "FAIL: setup script does not map boardsvc to ticket_board_service" >&2
    exit 1
}
grep -q 'setfacl -R -m "u:\$SERVICE_USER:rx"' "$SCRIPT" || {
    echo "FAIL: setup script does not grant deploy-tree ACL access" >&2
    exit 1
}
grep -q 'grant_write_acl "\$ASSET_ROOT"' "$SCRIPT" || {
    echo "FAIL: setup script does not grant asset directory write access" >&2
    exit 1
}
grep -q 'setfacl -m "u:\$SERVICE_USER:--x" "\$asset_parent"' "$SCRIPT" || {
    echo "FAIL: setup script does not grant traverse access to the asset parent directory" >&2
    exit 1
}
grep -q 'grant_write_acl "\$FRAME_ROOT"' "$SCRIPT" || {
    echo "FAIL: setup script does not grant shared frame directory write access" >&2
    exit 1
}
if grep -q '/home/agent' "$SCRIPT"; then
    echo "FAIL: setup script still selects the PGU owner home" >&2
    exit 1
fi
"$SCRIPT" --print-root-commands >"$TMPDIR_T/root-commands.out"
grep -q '^set -euo pipefail$' "$TMPDIR_T/root-commands.out" || {
    echo "FAIL: printed root commands must stop on the first failed step" >&2
    exit 1
}
grep -q 'generated operator-commands.sh' "$TMPDIR_T/root-commands.out" || {
    echo "FAIL: setup command plan does not delegate tenant unit installation to generated artifacts" >&2
    exit 1
}
grep -q -- '--apply-peer-auth)' "$SCRIPT" || {
    echo "FAIL: setup script does not expose a peer-auth-only apply mode" >&2
    exit 1
}
grep -q 'apply_peer_auth' "$SCRIPT" || {
    echo "FAIL: setup script does not factor peer-auth setup for reuse" >&2
    exit 1
}
if grep -q 'pgu-tickets"' "$SCRIPT"; then
    echo "FAIL: setup script should not grant boardsvc access to the JSON ticket store" >&2
    exit 1
fi
grep -q 'find "\$path" -type d -exec setfacl -m "d:u:\$SERVICE_USER:rx"' "$SCRIPT" || {
    echo "FAIL: setup script does not set default ACLs only on directories" >&2
    exit 1
}

FAKE_BIN="$TMPDIR_T/bin"
mkdir -p "$FAKE_BIN"
cat >"$FAKE_BIN/id" <<'SH'
#!/bin/sh
if [ "$#" -eq 1 ] && [ "$1" = "-u" ]; then
    printf '0\n'
    exit 0
fi
if [ "$#" -eq 2 ] && [ "$1" = "-u" ] && [ "$2" = "boardsvc" ]; then
    printf '944\n'
    exit 0
fi
exec /usr/bin/id "$@"
SH
cat >"$FAKE_BIN/runuser" <<'SH'
#!/bin/sh
case "$*" in
    *"SHOW hba_file"*)
        printf '%s\n' "$FAKE_HBA_FILE"
        ;;
    *"SHOW ident_file"*)
        printf '%s\n' "$FAKE_IDENT_FILE"
        ;;
    *"SHOW server_version_num"*)
        printf '%s\n' "${FAKE_SERVER_VERSION_NUM:-160000}"
        ;;
    *"SELECT pg_reload_conf();"*)
        printf 'reload\n' >>"$FAKE_RELOADS_FILE"
        printf 't\n'
        ;;
    *)
        printf 'unexpected runuser call: %s\n' "$*" >&2
        exit 64
        ;;
esac
SH
cat >"$FAKE_BIN/useradd" <<'SH'
#!/bin/sh
printf 'unexpected useradd call: %s\n' "$*" >&2
exit 65
SH
chmod +x "$FAKE_BIN/id" "$FAKE_BIN/runuser" "$FAKE_BIN/useradd"
FAKE_HBA="$TMPDIR_T/pg_hba.conf"
FAKE_IDENT="$TMPDIR_T/pg_ident.conf"
FAKE_RELOADS="$TMPDIR_T/reloads.log"
cat >"$FAKE_HBA" <<'HBA'
# Debian keeps the postgres maintenance rule above the local catch-all.
local   all             postgres                                peer
local   all             all                                     peer
host    all             all             127.0.0.1/32            scram-sha-256
HBA
: >"$FAKE_IDENT"
: >"$FAKE_RELOADS"
PATH="$FAKE_BIN:$PATH" FAKE_HBA_FILE="$FAKE_HBA" FAKE_IDENT_FILE="$FAKE_IDENT" FAKE_RELOADS_FILE="$FAKE_RELOADS" \
    PG_DATABASE=tenant_ticket_board SERVICE_USER=boardsvc SERVICE_ROLE=ticket_board_service PG_IDENT_MAP=pgu_ticket_board_service \
    "$SCRIPT" --apply-peer-auth >/dev/null
PATH="$FAKE_BIN:$PATH" FAKE_HBA_FILE="$FAKE_HBA" FAKE_IDENT_FILE="$FAKE_IDENT" FAKE_RELOADS_FILE="$FAKE_RELOADS" \
    PG_DATABASE=tenant_ticket_board SERVICE_USER=boardsvc SERVICE_ROLE=ticket_board_service PG_IDENT_MAP=pgu_ticket_board_service \
    "$SCRIPT" --apply-peer-auth >/dev/null
FAKE_HBA_INCLUDE="$TMPDIR_T/switchyard-ticket-board.pg_hba.conf"
FAKE_IDENT_INCLUDE="$TMPDIR_T/switchyard-ticket-board.pg_ident.conf"
if [ "$(grep -cFx "include_if_exists $FAKE_HBA_INCLUDE" "$FAKE_HBA")" != "1" ]; then
    echo "FAIL: peer-auth hba include should be inserted idempotently" >&2
    exit 1
fi
postgres_line="$(grep -nFx 'local   all             postgres                                peer' "$FAKE_HBA" | cut -d: -f1)"
include_line="$(grep -nFx "include_if_exists $FAKE_HBA_INCLUDE" "$FAKE_HBA" | cut -d: -f1)"
catchall_line="$(grep -nFx 'local   all             all                                     peer' "$FAKE_HBA" | cut -d: -f1)"
if [ "$postgres_line" -ge "$include_line" ] || [ "$include_line" -ge "$catchall_line" ]; then
    echo "FAIL: peer-auth include must be after local all/postgres and before local all/all" >&2
    nl -ba "$FAKE_HBA" >&2
    exit 1
fi
if [ "$(grep -cFx 'local   tenant_ticket_board   ticket_board_service   peer map=pgu_ticket_board_service' "$FAKE_HBA_INCLUDE")" != "1" ]; then
    echo "FAIL: peer-auth hba line should be written idempotently to the include file" >&2
    exit 1
fi
if [ "$(grep -cFx "include_if_exists $FAKE_IDENT_INCLUDE" "$FAKE_IDENT")" != "1" ]; then
    echo "FAIL: peer-auth ident include should be appended idempotently" >&2
    exit 1
fi
if [ "$(grep -cFx 'pgu_ticket_board_service   boardsvc   ticket_board_service' "$FAKE_IDENT_INCLUDE")" != "1" ]; then
    echo "FAIL: peer-auth ident line should be written idempotently to the include file" >&2
    exit 1
fi
if [ "$(grep -cFx 'reload' "$FAKE_RELOADS")" != "2" ]; then
    echo "FAIL: peer-auth apply should reload PostgreSQL on each run" >&2
    exit 1
fi

FAKE_HBA_15="$TMPDIR_T/pg15_pg_hba.conf"
FAKE_IDENT_15="$TMPDIR_T/pg15_pg_ident.conf"
FAKE_RELOADS_15="$TMPDIR_T/pg15_reloads.log"
cat >"$FAKE_HBA_15" <<'HBA'
# Debian keeps the postgres maintenance rule above the local catch-all.
local   all             postgres                                peer
local   all             all                                     peer
host    all             all             127.0.0.1/32            scram-sha-256
HBA
: >"$FAKE_IDENT_15"
: >"$FAKE_RELOADS_15"
PATH="$FAKE_BIN:$PATH" FAKE_SERVER_VERSION_NUM=150000 FAKE_HBA_FILE="$FAKE_HBA_15" FAKE_IDENT_FILE="$FAKE_IDENT_15" FAKE_RELOADS_FILE="$FAKE_RELOADS_15" \
    PG_DATABASE=tenant_ticket_board SERVICE_USER=boardsvc SERVICE_ROLE=ticket_board_service PG_IDENT_MAP=pgu_ticket_board_service \
    "$SCRIPT" --apply-peer-auth >/dev/null
PATH="$FAKE_BIN:$PATH" FAKE_SERVER_VERSION_NUM=150000 FAKE_HBA_FILE="$FAKE_HBA_15" FAKE_IDENT_FILE="$FAKE_IDENT_15" FAKE_RELOADS_FILE="$FAKE_RELOADS_15" \
    PG_DATABASE=tenant_ticket_board SERVICE_USER=boardsvc SERVICE_ROLE=ticket_board_service PG_IDENT_MAP=pgu_ticket_board_service \
    "$SCRIPT" --apply-peer-auth >/dev/null
if grep -q '^include_if_exists ' "$FAKE_HBA_15"; then
    echo "FAIL: PostgreSQL 15 fallback should insert the direct hba rule, not an include" >&2
    nl -ba "$FAKE_HBA_15" >&2
    exit 1
fi
if [ "$(grep -cFx 'local   tenant_ticket_board   ticket_board_service   peer map=pgu_ticket_board_service' "$FAKE_HBA_15")" != "1" ]; then
    echo "FAIL: PostgreSQL 15 fallback should insert the direct hba rule idempotently" >&2
    exit 1
fi
postgres_line="$(grep -nFx 'local   all             postgres                                peer' "$FAKE_HBA_15" | cut -d: -f1)"
rule_line="$(grep -nFx 'local   tenant_ticket_board   ticket_board_service   peer map=pgu_ticket_board_service' "$FAKE_HBA_15" | cut -d: -f1)"
catchall_line="$(grep -nFx 'local   all             all                                     peer' "$FAKE_HBA_15" | cut -d: -f1)"
if [ "$postgres_line" -ge "$rule_line" ] || [ "$rule_line" -ge "$catchall_line" ]; then
    echo "FAIL: PostgreSQL 15 fallback hba rule must be after local all/postgres and before local all/all" >&2
    nl -ba "$FAKE_HBA_15" >&2
    exit 1
fi
if [ "$(grep -cFx 'pgu_ticket_board_service   boardsvc   ticket_board_service' "$FAKE_IDENT_15")" != "1" ]; then
    echo "FAIL: PostgreSQL 15 fallback should append the direct ident map idempotently" >&2
    exit 1
fi

if command -v initdb >/dev/null 2>&1 && command -v pg_ctl >/dev/null 2>&1 && command -v createdb >/dev/null 2>&1 && command -v psql >/dev/null 2>&1; then
    PGDATA_DIR="$TMPDIR_T/pgdata"
    SOCKET_DIR="$TMPDIR_T/socket"
    mkdir -p "$SOCKET_DIR"
    PORT="$(free_port)"
    REACHABILITY_DB="switchyard_peer_reachability"
    CURRENT_OS_USER="$(id -un)"

    initdb -D "$PGDATA_DIR" -A trust --no-locale --username=postgres >/dev/null
    pg_ctl -D "$PGDATA_DIR" -o "-k $SOCKET_DIR -p $PORT -h ''" -w start >/dev/null
    createdb -h "$SOCKET_DIR" -p "$PORT" -U postgres "$REACHABILITY_DB"
    psql -X -v ON_ERROR_STOP=1 -h "$SOCKET_DIR" -p "$PORT" -U postgres "$REACHABILITY_DB" \
        -c "CREATE ROLE ticket_board_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;" >/dev/null
    pg_ctl -D "$PGDATA_DIR" -m fast -w stop >/dev/null

    cat >"$PGDATA_DIR/pg_hba.conf" <<HBA
local   all                         postgres                                peer
include_if_exists $PGDATA_DIR/switchyard-ticket-board.pg_hba.conf
local   all                         all                                     peer
HBA
    cat >"$PGDATA_DIR/switchyard-ticket-board.pg_hba.conf" <<HBA
local   $REACHABILITY_DB            ticket_board_service                    peer map=pgu_ticket_board_service
HBA
    cat >"$PGDATA_DIR/pg_ident.conf" <<IDENT
include_if_exists $PGDATA_DIR/switchyard-ticket-board.pg_ident.conf
IDENT
    cat >"$PGDATA_DIR/switchyard-ticket-board.pg_ident.conf" <<IDENT
pgu_ticket_board_service            $CURRENT_OS_USER                        ticket_board_service
IDENT

    pg_ctl -D "$PGDATA_DIR" -o "-k $SOCKET_DIR -p $PORT -h ''" -w start >/dev/null
    reached_user="$(
        psql -X -v ON_ERROR_STOP=1 -h "$SOCKET_DIR" -p "$PORT" -U ticket_board_service "$REACHABILITY_DB" -tA \
            -c "SELECT current_user"
    )"
    if [ "$reached_user" != "ticket_board_service" ]; then
        echo "FAIL: peer-auth reachability returned unexpected current_user: $reached_user" >&2
        exit 1
    fi
    postgres_line="$(grep -n '^local[[:space:]]\+all[[:space:]]\+postgres[[:space:]]\+peer' "$PGDATA_DIR/pg_hba.conf" | cut -d: -f1)"
    include_line="$(grep -n "^include_if_exists $PGDATA_DIR/switchyard-ticket-board.pg_hba.conf$" "$PGDATA_DIR/pg_hba.conf" | cut -d: -f1)"
    catchall_line="$(grep -n '^local[[:space:]]\+all[[:space:]]\+all[[:space:]]\+peer' "$PGDATA_DIR/pg_hba.conf" | cut -d: -f1)"
    if [ "$postgres_line" -ge "$include_line" ] || [ "$include_line" -ge "$catchall_line" ]; then
        echo "FAIL: reachability cluster did not preserve local all/postgres before the switchyard include" >&2
        nl -ba "$PGDATA_DIR/pg_hba.conf" >&2
        exit 1
    fi
    pg_ctl -D "$PGDATA_DIR" -m fast -w stop >/dev/null
    PGDATA_DIR=""
else
    echo "ticket_board_boardsvc_prep_test: reachability check skipped, missing PostgreSQL test tools" >&2
fi

grep -q '^User=boardsvc$' "$UNIT" || {
    echo "FAIL: proposed unit does not run as boardsvc" >&2
    exit 1
}
grep -q '^RuntimeDirectory=pgu-ticket-board$' "$UNIT" || {
    echo "FAIL: proposed unit does not request a service-owned runtime directory" >&2
    exit 1
}
grep -q -- '--unix-socket /run/pgu-ticket-board/ticket-board.sock' "$UNIT" || {
    echo "FAIL: proposed unit does not bind the pane socket in the runtime directory" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_SOCKET=/run/pgu-ticket-board/ticket-board.sock$' "$UNIT" || {
    echo "FAIL: proposed unit does not export the runtime socket path" >&2
    exit 1
}
grep -q '^EnvironmentFile=-/home/agent/.config/pgu/ticket-board.env$' "$UNIT" || {
    echo "FAIL: proposed unit does not load the tenant report token env file" >&2
    exit 1
}
grep -q '^Environment=PGUSER=ticket_board_service$' "$UNIT" || {
    echo "FAIL: proposed unit does not select ticket_board_service" >&2
    exit 1
}
grep -q '^Environment=TICKET_BOARD_DATABASE_URL=postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_service$' "$UNIT" || {
    echo "FAIL: proposed unit does not bake the service database URL" >&2
    exit 1
}
grep -q '^Environment=HOME=/home/agent$' "$UNIT" || {
    echo "FAIL: proposed unit does not set a stable HOME for remaining expanduser paths" >&2
    exit 1
}
if grep -q '^ExecStartPre=/bin/mkdir -p /tmp/pgu-frames$' "$UNIT"; then
    echo "FAIL: proposed sandboxed unit must not pretend ExecStartPre can create ReadWritePaths targets" >&2
    exit 1
fi
if grep -q '^ExecStartPre=/bin/chmod 1777 /tmp/pgu-frames$' "$UNIT"; then
    echo "FAIL: proposed sandboxed unit must not carry dead frame-directory chmod pre-start hooks" >&2
    exit 1
fi
retired_backend_flag='--store''-backend'
if grep -q -- "$retired_backend_flag" "$UNIT"; then
    echo "FAIL: proposed unit should not pass the removed backend selector option" >&2
    exit 1
fi
grep -q -- '--frames /tmp/pgu-frames --assets /home/agent/.claude/pgu-tickets-assets' "$UNIT" || {
    echo "FAIL: proposed unit does not use explicit shared frames/assets paths" >&2
    exit 1
}
grep -q '^ReadWritePaths=/home/agent/.claude/pgu-tickets-assets /tmp/pgu-frames$' "$UNIT" || {
    echo "FAIL: proposed unit does not limit writable paths to assets and frames" >&2
    exit 1
}
if grep -q '^PrivateTmp=true$' "$UNIT"; then
    echo "FAIL: proposed unit must not isolate /tmp/pgu-frames with PrivateTmp" >&2
    exit 1
fi
if grep -Eq 'PASSWORD|PGPASSWORD|postgresql://[^ ]*:' "$UNIT"; then
    echo "FAIL: proposed unit appears to contain a password-bearing credential" >&2
    exit 1
fi
[[ -f "$TMPFILES" ]] || {
    echo "FAIL: tmpfiles definition is missing" >&2
    exit 1
}
grep -q '^d /tmp/pgu-frames 1777 root root -$' "$TMPFILES" || {
    echo "FAIL: tmpfiles definition does not recreate the shared frame directory on boot" >&2
    exit 1
}
[[ -f "$POLKIT_RULE" ]] || {
    echo "FAIL: board deploy polkit rule is missing" >&2
    exit 1
}
grep -q 'unit === "pgu-ticket-board-canary.service"' "$POLKIT_RULE" || {
    echo "FAIL: board deploy polkit rule does not cover the fixed deploy canary unit" >&2
    exit 1
}
if grep -q 'pgu-ticket-board-canary-' "$POLKIT_RULE"; then
    echo "FAIL: board deploy polkit rule must not cover caller-defined transient canary units" >&2
    exit 1
fi
[[ -f "$CANARY_UNIT" ]] || {
    echo "FAIL: fixed deploy canary systemd unit is missing" >&2
    exit 1
}
grep -q '^User=boardsvc$' "$CANARY_UNIT" || {
    echo "FAIL: fixed canary unit does not run as boardsvc" >&2
    exit 1
}
grep -q '^EnvironmentFile=/home/agent/pgu-ticketboard-live/canary.env$' "$CANARY_UNIT" || {
    echo "FAIL: fixed canary unit does not read the deploy-written environment file" >&2
    exit 1
}
grep -q '^ReadWritePaths=/tmp$' "$CANARY_UNIT" || {
    echo "FAIL: fixed canary unit does not limit writable paths to /tmp" >&2
    exit 1
}
grep -q '^NoNewPrivileges=true$' "$CANARY_UNIT" || {
    echo "FAIL: fixed canary unit does not pin NoNewPrivileges" >&2
    exit 1
}
if grep -Eq '^\[Install\]$|^WantedBy=' "$CANARY_UNIT"; then
    echo "FAIL: fixed canary unit must not be enable-able at boot" >&2
    exit 1
fi
if grep -Eq '%i|systemd-run|pgu-ticket-board-canary-' "$CANARY_UNIT"; then
    echo "FAIL: fixed canary unit must not interpolate instance names or use transient units" >&2
    exit 1
fi

grep -q 'parser.add_argument("--assets"' "$REPO_ROOT/scripts/ticket_board/cli.py" || {
    echo "FAIL: CLI does not expose an explicit assets directory" >&2
    exit 1
}
HOME=/nonexistent python3 - <<'PY'
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.ticket_board.app import TicketBoardApp

with TemporaryDirectory(prefix="boardsvc-path-resolution.") as tmp:
    root = Path(tmp)
    frames = root / "frames"
    assets = root / "assets"
    app = TicketBoardApp(frames, assets, database_url="")
    assert str(app.frame_dir) == str(frames.resolve()), app.frame_dir
    assert str(app.asset_dir) == str(assets.resolve()), app.asset_dir
    assert "/nonexistent" not in str(app.frame_dir)
    assert "/nonexistent" not in str(app.asset_dir)
    assert getattr(app, "store_backend") == "postgres"
PY
grep -q 'PGU-197 is a prep-only package' "$RUNBOOK" || {
    echo "FAIL: runbook does not mark the package prep-only" >&2
    exit 1
}
grep -q 'This package targets the PGU-198/PGU-209 single-writer board' "$RUNBOOK" || {
    echo "FAIL: runbook does not document DB-backed ticket-store boundary" >&2
    exit 1
}
grep -q 'tmpfiles' "$RUNBOOK" || {
    echo "FAIL: runbook does not document tmpfiles-based frame directory recreation" >&2
    exit 1
}
grep -q '49-pgu-board-deploy.rules' "$RUNBOOK" || {
    echo "FAIL: runbook does not document the board deploy polkit rule" >&2
    exit 1
}
grep -q 'Do not add an ACL for `/home/agent/.claude/pgu-tickets`' "$RUNBOOK" || {
    echo "FAIL: runbook does not document DB-backed ticket-store boundary" >&2
    exit 1
}
grep -q 'Verify' "$RUNBOOK" || {
    echo "FAIL: runbook lacks verification steps" >&2
    exit 1
}
grep -q 'EXPECTED_TICKETS=' "$RUNBOOK" || {
    echo "FAIL: runbook lacks real ticket-count verification" >&2
    exit 1
}
grep -q 'data\["store_backend"\] == "postgres"' "$RUNBOOK" || {
    echo "FAIL: runbook does not verify the board is using the Postgres backend" >&2
    exit 1
}
grep -q 'Roll Back' "$RUNBOOK" || {
    echo "FAIL: runbook lacks rollback steps" >&2
    exit 1
}

echo "ticket_board_boardsvc_prep_test: ok"
