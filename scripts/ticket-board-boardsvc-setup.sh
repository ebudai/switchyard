#!/usr/bin/env bash
set -euo pipefail

# PREP artifact for PGU-197. Do not run during ticket implementation.
# Eric runs this as root during the supervised cutover, after reviewing the
# printed plan and setting PG_DATABASE if the production DB name is not "pgu".

SERVICE_USER="${SERVICE_USER:-boardsvc}"
SERVICE_ROLE="${SERVICE_ROLE:-ticket_board_service}"
PG_DATABASE="${PG_DATABASE:-pgu}"
PG_IDENT_MAP="${PG_IDENT_MAP:-pgu_ticket_board_service}"
BOARD_ROOT="${BOARD_ROOT:-/home/agent/pgu-ticketboard-live}"
ASSET_ROOT="${ASSET_ROOT:-/home/agent/.claude/pgu-tickets-assets}"
FRAME_ROOT="${FRAME_ROOT:-/tmp/pgu-frames}"

usage() {
    cat <<EOF
Usage:
  $0 --print-root-commands
  sudo PG_DATABASE=<db> $0 --apply

Creates the $SERVICE_USER OS user, appends peer-auth pg_hba/pg_ident rules for
$SERVICE_ROLE, reloads PostgreSQL, and grants $SERVICE_USER access to the
deploy, asset, and frame directories using ACLs. Defaults are shown by
--print-root-commands.
EOF
}

print_root_commands() {
    cat <<EOF
# 1. Create $SERVICE_USER, add peer auth for $SERVICE_ROLE, reload PostgreSQL,
#    and grant deploy/assets/frames ACL access.
sudo PG_DATABASE=$PG_DATABASE PG_IDENT_MAP=$PG_IDENT_MAP BOARD_ROOT=$BOARD_ROOT \\
  ASSET_ROOT=$ASSET_ROOT FRAME_ROOT=$FRAME_ROOT SERVICE_USER=$SERVICE_USER SERVICE_ROLE=$SERVICE_ROLE \\
  scripts/ticket-board-boardsvc-setup.sh --apply

# 2. Install the proposed system unit, tmpfiles entry, and deploy polkit rule
#    after reviewing/editing PGDATABASE.
sudo install -m 0644 deploy/systemd/pgu-ticket-board.service.boardsvc /etc/systemd/system/pgu-ticket-board.service
sudo install -m 0644 deploy/systemd/pgu-ticket-board-canary.service /etc/systemd/system/pgu-ticket-board-canary.service
sudo install -m 0644 deploy/tmpfiles/pgu-ticket-board.conf /etc/tmpfiles.d/pgu-ticket-board.conf
sudo install -m 0644 deploy/polkit/49-pgu-board-deploy.rules /etc/polkit-1/rules.d/49-pgu-board-deploy.rules
sudo systemd-tmpfiles --create /etc/tmpfiles.d/pgu-ticket-board.conf

# 3. Reload systemd and start the board under $SERVICE_USER.
sudo systemctl daemon-reload
sudo systemctl enable --now pgu-ticket-board.service
EOF
}

require_root() {
    if [[ "$(id -u)" != "0" ]]; then
        printf 'ERROR: --apply must be run as root during the supervised cutover.\n' >&2
        exit 1
    fi
}

append_once() {
    local line="$1"
    local file="$2"
    grep -qxF "$line" "$file" || printf '%s\n' "$line" >>"$file"
}

postgres_setting() {
    local setting="$1"
    runuser -u postgres -- psql -Atqc "SHOW $setting"
}

grant_read_exec_acl() {
    local path="$1"
    setfacl -R -m "u:$SERVICE_USER:rx" "$path"
    find "$path" -type d -exec setfacl -m "d:u:$SERVICE_USER:rx" {} +
}

grant_write_acl() {
    local path="$1"
    mkdir -p "$path"
    setfacl -R -m "u:$SERVICE_USER:rwx" "$path"
    find "$path" -type d -exec setfacl -m "d:u:$SERVICE_USER:rwx" {} +
}

apply_setup() {
    require_root

    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        useradd -r -M -d /nonexistent -s /usr/sbin/nologin "$SERVICE_USER"
    fi

    local hba_file ident_file hba_line ident_line
    hba_file="$(postgres_setting hba_file)"
    ident_file="$(postgres_setting ident_file)"
    hba_line="local   $PG_DATABASE   $SERVICE_ROLE   peer map=$PG_IDENT_MAP"
    ident_line="$PG_IDENT_MAP   $SERVICE_USER   $SERVICE_ROLE"

    append_once "# PGU ticket board service peer auth: $SERVICE_USER -> $SERVICE_ROLE" "$hba_file"
    append_once "$hba_line" "$hba_file"
    append_once "# PGU ticket board service peer auth: $SERVICE_USER -> $SERVICE_ROLE" "$ident_file"
    append_once "$ident_line" "$ident_file"
    runuser -u postgres -- psql -Atqc "SELECT pg_reload_conf();"

    if ! command -v setfacl >/dev/null 2>&1; then
        printf 'ERROR: setfacl is required; install the acl package or apply equivalent ownership manually.\n' >&2
        exit 1
    fi
    local asset_parent
    asset_parent="$(dirname "$ASSET_ROOT")"
    mkdir -p "$asset_parent"
    setfacl -m "u:$SERVICE_USER:--x" /home/agent
    setfacl -m "u:$SERVICE_USER:--x" "$asset_parent"
    grant_read_exec_acl "$BOARD_ROOT"
    grant_write_acl "$ASSET_ROOT"
    grant_write_acl "$FRAME_ROOT"

    printf 'boardsvc peer-auth setup staged for DB %s, deploy root %s, assets %s, frames %s\n' \
        "$PG_DATABASE" "$BOARD_ROOT" "$ASSET_ROOT" "$FRAME_ROOT"
}

main() {
    case "${1:-}" in
        --print-root-commands)
            print_root_commands
            ;;
        --apply)
            apply_setup
            ;;
        -h|--help)
            usage
            ;;
        *)
            usage >&2
            exit 1
            ;;
    esac
}

main "$@"
