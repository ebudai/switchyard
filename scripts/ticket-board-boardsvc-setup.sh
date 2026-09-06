#!/usr/bin/env bash
set -euo pipefail

# PREP artifact for PGU-197. Do not run during ticket implementation.
# Eric runs this as root during the supervised cutover, after reviewing the
# printed plan and setting PG_DATABASE if the production DB name is not "pgu".

SERVICE_USER="${SERVICE_USER:-boardsvc}"
SERVICE_ROLE="${SERVICE_ROLE:-ticket_board_service}"
PROJECT_SLUG="${TICKET_BOARD_PROJECT:-${PGU_TICKET_BOARD_PROJECT:-pgu}}"
if [[ -z "${PG_DATABASE:-}" ]]; then
    PG_DATABASE="${PROJECT_SLUG//-/_}"
    [[ "$PROJECT_SLUG" == "pgu" ]] || PG_DATABASE="${PG_DATABASE}_ticket_board"
fi
PG_IDENT_MAP="${PG_IDENT_MAP:-${PROJECT_SLUG//-/_}_ticket_board_service}"
OWNER_HOME="${TICKET_BOARD_OWNER_HOME:-}"
BOARD_ROOT="${BOARD_ROOT:-${OWNER_HOME:+$OWNER_HOME/$PROJECT_SLUG-ticketboard-live}}"
ASSET_ROOT="${ASSET_ROOT:-${OWNER_HOME:+$OWNER_HOME/.claude/$PROJECT_SLUG-tickets-assets}}"
if [[ -z "${FRAME_ROOT:-}" ]]; then
    if [[ "$PROJECT_SLUG" == "pgu" ]]; then
        FRAME_ROOT="/tmp/pgu-frames"
    else
        FRAME_ROOT="${OWNER_HOME:+$OWNER_HOME/.claude/$PROJECT_SLUG-ticket-frames}"
    fi
fi

usage() {
    cat <<EOF
Usage:
  $0 --print-root-commands
  sudo PG_DATABASE=<db> $0 --apply-peer-auth
  sudo PG_DATABASE=<db> $0 --apply

Creates the $SERVICE_USER OS user, installs reachable peer-auth pg_hba/pg_ident
rules for $SERVICE_ROLE, reloads PostgreSQL, and grants $SERVICE_USER access to the
configured tenant deploy, asset, and frame directories using ACLs. Values are shown by
--print-root-commands.
EOF
}

print_root_commands() {
    cat <<EOF
set -euo pipefail

# 1. Create $SERVICE_USER, add peer auth for $SERVICE_ROLE, reload PostgreSQL,
#    and grant deploy/assets/frames ACL access.
sudo TICKET_BOARD_PROJECT=$PROJECT_SLUG TICKET_BOARD_OWNER_HOME=$OWNER_HOME \\
  PG_DATABASE=$PG_DATABASE PG_IDENT_MAP=$PG_IDENT_MAP BOARD_ROOT=$BOARD_ROOT \\
  ASSET_ROOT=$ASSET_ROOT FRAME_ROOT=$FRAME_ROOT SERVICE_USER=$SERVICE_USER SERVICE_ROLE=$SERVICE_ROLE \\
  scripts/ticket-board-boardsvc-setup.sh --apply

# Install the project-specific service, tmpfiles, and polkit artifacts with the
# generated operator-commands.sh. Shared source does not embed tenant units.
EOF
}

require_root() {
    if [[ "$(id -u)" != "0" ]]; then
        printf 'ERROR: --apply must be run as root during the supervised cutover.\n' >&2
        exit 1
    fi
}

require_storage_paths() {
    local name value
    for name in OWNER_HOME BOARD_ROOT ASSET_ROOT FRAME_ROOT; do
        value="${!name:-}"
        if [[ -z "$value" ]]; then
            printf 'ERROR: %s is required; set TICKET_BOARD_OWNER_HOME or the explicit tenant path variables.\n' "$name" >&2
            exit 1
        fi
        if [[ "$value" != /* ]]; then
            printf 'ERROR: %s must be an absolute tenant path: %s\n' "$name" "$value" >&2
            exit 1
        fi
    done
}

append_once() {
    local line="$1"
    local file="$2"
    grep -qxF "$line" "$file" || printf '%s\n' "$line" >>"$file"
}

insert_block_before_local_catchall_once() {
    local file="$1"
    local marker="$2"
    local record="$3"
    python3 - "$file" "$marker" "$record" <<'PY'
from __future__ import annotations

import sys
from pathlib import Path

path = Path(sys.argv[1])
marker = sys.argv[2]
record = sys.argv[3]
lines = path.read_text(encoding="utf-8").splitlines()
if record in lines:
    raise SystemExit(0)

insert_at = len(lines)
for index, line in enumerate(lines):
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        continue
    fields = stripped.split()
    if len(fields) >= 4 and fields[0] == "local" and fields[1] == "all" and fields[2] == "all":
        insert_at = index
        break

block = [marker, record]
lines[insert_at:insert_at] = block
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
}

postgres_setting() {
    local setting="$1"
    runuser -u postgres -- psql -Atqc "SHOW $setting"
}

postgres_server_version_num() {
    postgres_setting server_version_num
}

supports_hba_includes() {
    local version_num
    version_num="$(postgres_server_version_num)"
    [[ "$version_num" =~ ^[0-9]+$ ]] && (( version_num >= 160000 ))
}

render_include_path() {
    local path="$1"
    if [[ "$path" =~ [[:space:]#] ]]; then
        printf 'ERROR: PostgreSQL include path contains whitespace or #: %s\n' "$path" >&2
        exit 1
    fi
    printf '%s' "$path"
}

install_peer_hba_rule() {
    local hba_file="$1"
    local hba_line="$2"
    local hba_marker="# PGU ticket board service peer auth: $SERVICE_USER -> $SERVICE_ROLE"
    if supports_hba_includes; then
        local hba_include_file hba_include_line
        hba_include_file="$(dirname "$hba_file")/switchyard-ticket-board.pg_hba.conf"
        hba_include_line="include_if_exists $(render_include_path "$hba_include_file")"
        touch "$hba_include_file"
        chmod 0644 "$hba_include_file"
        append_once "$hba_marker" "$hba_include_file"
        append_once "$hba_line" "$hba_include_file"
        insert_block_before_local_catchall_once "$hba_file" "$hba_marker" "$hba_include_line"
    else
        insert_block_before_local_catchall_once "$hba_file" "$hba_marker" "$hba_line"
    fi
}

install_peer_ident_rule() {
    local ident_file="$1"
    local ident_line="$2"
    local ident_marker="# PGU ticket board service peer auth: $SERVICE_USER -> $SERVICE_ROLE"
    if supports_hba_includes; then
        local ident_include_file ident_include_line
        ident_include_file="$(dirname "$ident_file")/switchyard-ticket-board.pg_ident.conf"
        ident_include_line="include_if_exists $(render_include_path "$ident_include_file")"
        touch "$ident_include_file"
        chmod 0644 "$ident_include_file"
        append_once "$ident_marker" "$ident_include_file"
        append_once "$ident_line" "$ident_include_file"
        append_once "$ident_include_line" "$ident_file"
    else
        append_once "$ident_marker" "$ident_file"
        append_once "$ident_line" "$ident_file"
    fi
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

ensure_service_user() {
    if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
        useradd -r -M -d /nonexistent -s /usr/sbin/nologin "$SERVICE_USER"
    fi
}

apply_peer_auth() {
    require_root

    ensure_service_user

    local hba_file ident_file hba_line ident_line
    hba_file="$(postgres_setting hba_file)"
    ident_file="$(postgres_setting ident_file)"
    hba_line="local   $PG_DATABASE   $SERVICE_ROLE   peer map=$PG_IDENT_MAP"
    ident_line="$PG_IDENT_MAP   $SERVICE_USER   $SERVICE_ROLE"

    install_peer_hba_rule "$hba_file" "$hba_line"
    install_peer_ident_rule "$ident_file" "$ident_line"
    runuser -u postgres -- psql -Atqc "SELECT pg_reload_conf();"
}

apply_setup() {
    require_root
    require_storage_paths
    apply_peer_auth

    if ! command -v setfacl >/dev/null 2>&1; then
        printf 'ERROR: setfacl is required; install the acl package or apply equivalent ownership manually.\n' >&2
        exit 1
    fi
    local asset_parent
    asset_parent="$(dirname "$ASSET_ROOT")"
    mkdir -p "$asset_parent"
    setfacl -m "u:$SERVICE_USER:--x" "$OWNER_HOME"
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
        --apply-peer-auth)
            apply_peer_auth
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
