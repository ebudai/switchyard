#!/usr/bin/env bash
set -euo pipefail

readonly SERVICE_NAME="pgu-ticket-board.service"
readonly SOURCE_REPO="${SOURCE_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
readonly BOARD_ROOT="${BOARD_ROOT:-/home/agent/pgu-ticketboard-live}"
readonly BOARD_RELEASES_DIR="$BOARD_ROOT/releases"
readonly BOARD_CURRENT_LINK="$BOARD_ROOT/current"
readonly BOARD_SCRIPT="${BOARD_SCRIPT:-$BOARD_CURRENT_LINK/scripts/ticket-board.py}"
readonly BOARD_HOST="${BOARD_HOST:-127.0.0.1}"
readonly BOARD_PORT="${BOARD_PORT:-8770}"
readonly BOARD_UNIX_SOCKET="${BOARD_UNIX_SOCKET:-/run/pgu-ticket-board/ticket-board.sock}"
readonly FRAME_ROOT="${FRAME_ROOT:-/tmp/pgu-frames}"
readonly LOG_PATH="${LOG_PATH:-/tmp/pgu-ticket-board.log}"
readonly UNIT_DIR="${UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
readonly UNIT_PATH="$UNIT_DIR/$SERVICE_NAME"
readonly SYSTEM_UNIT_PATH="${TICKET_BOARD_SYSTEM_UNIT_PATH:-/etc/systemd/system/$SERVICE_NAME}"
readonly SYSTEM_UNIT_HASH_RECORD="${TICKET_BOARD_SYSTEM_UNIT_HASH_RECORD:-$BOARD_ROOT/system-unit.sha256}"
readonly DEPLOY_REF="${DEPLOY_REF:-origin/main}"
readonly BOARD_DATABASE_URL="${TICKET_BOARD_DATABASE_URL:-postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_service}"
readonly BOARD_ADMIN_DATABASE_URL="${TICKET_BOARD_ADMIN_DATABASE_URL:-postgresql:///pgu?host=/var/run/postgresql&user=postgres}"
readonly RBAC_SQL="${RBAC_SQL:-$BOARD_CURRENT_LINK/scripts/ticket_board/rbac.sql}"
readonly MIGRATION_RUNNER="${TICKET_BOARD_MIGRATION_RUNNER:-$BOARD_CURRENT_LINK/scripts/ticket-board-migrate}"
readonly SMOKE_PATH="${BOARD_SMOKE_PATH:-/api/board}"
readonly SMOKE_TIMEOUT_SECONDS="${BOARD_SMOKE_TIMEOUT_SECONDS:-10}"
readonly SERVICE_SCOPE="${TICKET_BOARD_SERVICE_SCOPE:-auto}"
readonly POLKIT_APPROVAL_USER="${TICKET_BOARD_POLKIT_APPROVAL_USER:-eric}"
readonly POLKIT_TIMEOUT_SECONDS="${TICKET_BOARD_POLKIT_TIMEOUT_SECONDS:-30}"

usage() {
    cat <<EOF
Usage: scripts/ticket-board-service.sh <install|deploy|deploy-restart|ensure-migrations|ensure-roles|render-unit|start|stop|restart|status|logs>

Manage the PGU ticket board service.

Commands:
  install         Export $DEPLOY_REF into $BOARD_ROOT/current, write the unit, enable and start it
  deploy          Refresh $BOARD_ROOT/current from $DEPLOY_REF without restarting the service
  deploy-restart  Refresh $BOARD_ROOT/current from $DEPLOY_REF and restart the live service
  ensure-migrations
                  Apply SQL migrations and record their names in schema_migrations
  ensure-roles    Apply the idempotent ticket-board RBAC SQL using a privileged admin connection
  render-unit     Print the systemd unit contents to stdout
  start|stop|restart|status
                  Operate on the live service (system unit if installed, else --user)
  logs            Tail $LOG_PATH
EOF
}

die() {
    printf '[ticket-board-service] ERROR: %s\n' "$*" >&2
    exit 1
}

log() {
    printf '[ticket-board-service] %s\n' "$*" >&2
}

runtime_dir() {
    printf '/run/user/%s\n' "$(id -u)"
}

ensure_user_manager() {
    local user runtime
    user="$(id -un)"
    runtime="$(runtime_dir)"
    loginctl enable-linger "$user" >/dev/null 2>&1 || true
    for _ in $(seq 1 50); do
        [[ -d "$runtime" && -S "$runtime/bus" ]] && return 0
        sleep 0.1
    done
    die "systemd user bus unavailable at $runtime/bus"
}

systemctl_user() {
    local runtime
    runtime="$(runtime_dir)"
    XDG_RUNTIME_DIR="$runtime" \
    DBUS_SESSION_BUS_ADDRESS="unix:path=$runtime/bus" \
        systemctl --user "$@"
}

systemctl_system() {
    if [[ "$(id -u)" == "0" ]]; then
        systemctl "$@"
        return
    fi
    local session_check_status=0
    polkit_graphical_session_available "$POLKIT_APPROVAL_USER" || session_check_status=$?
    if [[ "$session_check_status" == "1" ]]; then
        die "system service action requires polkit approval from $POLKIT_APPROVAL_USER's active graphical session; run this from that session so the KDE prompt can appear"
    fi
    run_systemctl_with_polkit_timeout "$@"
}

loginctl_session_property() {
    local session_id="$1"
    local property="$2"
    loginctl show-session "$session_id" -p "$property" --value 2>/dev/null || true
}

polkit_graphical_session_available() {
    local target_user="$1"
    local session_id listed_user active remote session_type
    command -v loginctl >/dev/null 2>&1 || return 2
    while read -r session_id _ listed_user _; do
        [[ -n "$session_id" ]] || continue
        [[ "$listed_user" == "$target_user" ]] || continue
        active="$(loginctl_session_property "$session_id" Active)"
        remote="$(loginctl_session_property "$session_id" Remote)"
        session_type="$(loginctl_session_property "$session_id" Type)"
        if [[ "$active" == "yes" && "$remote" != "yes" && ( "$session_type" == "wayland" || "$session_type" == "x11" ) ]]; then
            return 0
        fi
    done < <(loginctl list-sessions --no-legend 2>/dev/null || true)
    return 1
}

run_systemctl_with_polkit_timeout() {
    local output status
    local timeout_args=()
    if command -v timeout >/dev/null 2>&1; then
        timeout_args=(timeout --foreground "${POLKIT_TIMEOUT_SECONDS}s")
    fi
    if output="$("${timeout_args[@]}" systemctl "$@" 2>&1)"; then
        if [[ -n "$output" ]]; then
            printf '%s\n' "$output"
        fi
        return 0
    else
        status=$?
    fi
    if [[ -n "$output" ]]; then
        printf '%s\n' "$output" >&2
    fi
    if [[ "$status" == "124" ]]; then
        die "systemctl $* timed out waiting for polkit approval; run this from $POLKIT_APPROVAL_USER's active graphical session so the KDE prompt can be approved"
    fi
    if [[ "$output" == *"Interactive authentication required"* ]]; then
        die "systemctl $* could not reach an interactive polkit approval flow; run this from $POLKIT_APPROVAL_USER's active graphical session so the KDE prompt can appear"
    fi
    return "$status"
}

system_unit_fragment_path() {
    systemctl show "$SERVICE_NAME" -p FragmentPath --value 2>/dev/null || true
}

system_unit_file_path() {
    local fragment
    fragment="$(system_unit_fragment_path)"
    if [[ -n "$fragment" && -f "$fragment" ]]; then
        printf '%s\n' "$fragment"
        return 0
    fi
    if [[ -f "$SYSTEM_UNIT_PATH" ]]; then
        printf '%s\n' "$SYSTEM_UNIT_PATH"
        return 0
    fi
    return 1
}

system_unit_hash() {
    local unit_path
    unit_path="$(system_unit_file_path)" || return 1
    sha256sum "$unit_path" | awk '{print $1}'
}

system_unit_reload_required() {
    local current_hash recorded_hash=""
    current_hash="$(system_unit_hash)" || return 1
    [[ -f "$SYSTEM_UNIT_HASH_RECORD" ]] || return 1
    recorded_hash="$(<"$SYSTEM_UNIT_HASH_RECORD")"
    [[ "$current_hash" != "$recorded_hash" ]]
}

record_system_unit_hash() {
    local current_hash
    current_hash="$(system_unit_hash)" || return 0
    mkdir -p "$(dirname "$SYSTEM_UNIT_HASH_RECORD")"
    printf '%s\n' "$current_hash" >"$SYSTEM_UNIT_HASH_RECORD"
}

system_service_installed() {
    local fragment
    fragment="$(system_unit_fragment_path)"
    [[ -n "$fragment" ]] || [[ -f "$SYSTEM_UNIT_PATH" ]]
}

resolved_service_scope() {
    case "$SERVICE_SCOPE" in
        auto)
            if system_service_installed; then
                printf 'system\n'
            else
                printf 'user\n'
            fi
            ;;
        user|system)
            printf '%s\n' "$SERVICE_SCOPE"
            ;;
        *)
            die "invalid TICKET_BOARD_SERVICE_SCOPE=$SERVICE_SCOPE (expected auto|user|system)"
            ;;
    esac
}

quiesce_user_shadow_unit() {
    local runtime
    runtime="$(runtime_dir)"
    if [[ ! -d "$runtime" || ! -S "$runtime/bus" ]]; then
        return
    fi
    systemctl_user stop "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl_user disable "$SERVICE_NAME" >/dev/null 2>&1 || true
    systemctl_user reset-failed "$SERVICE_NAME" >/dev/null 2>&1 || true
}

ensure_source_repo() {
    git -C "$SOURCE_REPO" rev-parse --show-toplevel >/dev/null 2>&1 || die "missing source repo: $SOURCE_REPO"
}

maybe_fetch_origin() {
    if git -C "$SOURCE_REPO" remote get-url origin >/dev/null 2>&1; then
        git -C "$SOURCE_REPO" fetch origin >/dev/null 2>&1
    fi
}

deploy_export() {
    local resolved_ref release_dir tmp_dir
    ensure_source_repo
    maybe_fetch_origin
    resolved_ref="$(git -C "$SOURCE_REPO" rev-parse "$DEPLOY_REF")" || die "failed to resolve $DEPLOY_REF in $SOURCE_REPO"
    mkdir -p "$BOARD_RELEASES_DIR"
    release_dir="$BOARD_RELEASES_DIR/$resolved_ref"
    if [[ ! -d "$release_dir" ]]; then
        tmp_dir="$BOARD_RELEASES_DIR/.tmp-$resolved_ref.$$"
        rm -rf "$tmp_dir"
        mkdir -p "$tmp_dir"
        git -C "$SOURCE_REPO" archive "$resolved_ref" | tar -x -C "$tmp_dir"
        mv "$tmp_dir" "$release_dir"
    fi
    ln -sfn "$release_dir" "$BOARD_CURRENT_LINK"
    printf '%s\n' "$resolved_ref"
}

apply_database_migrations() {
    if [[ "${TICKET_BOARD_SKIP_MIGRATIONS:-}" == "1" ]]; then
        log "skipping ticket-board migrations because TICKET_BOARD_SKIP_MIGRATIONS=1"
        return 0
    fi
    [[ -x "$MIGRATION_RUNNER" ]] || die "missing executable migration runner after deploy: $MIGRATION_RUNNER"
    TICKET_BOARD_ADMIN_DATABASE_URL="$BOARD_ADMIN_DATABASE_URL" "$MIGRATION_RUNNER"
    log "applied ticket-board database migrations using $MIGRATION_RUNNER"
}

ensure_database_roles() {
    [[ -f "$RBAC_SQL" ]] || die "missing ticket-board RBAC SQL after deploy: $RBAC_SQL"
    command -v psql >/dev/null 2>&1 || die "psql is required to ensure ticket-board service roles"
    local output
    if ! output="$(psql -X -v ON_ERROR_STOP=1 "$BOARD_ADMIN_DATABASE_URL" -f "$RBAC_SQL" 2>&1)"; then
        printf '[ticket-board-service] ERROR: failed to ensure ticket-board database roles using TICKET_BOARD_ADMIN_DATABASE_URL. This must connect as a PostgreSQL role with CREATEROLE (default: user=postgres); override TICKET_BOARD_ADMIN_DATABASE_URL for nonstandard clusters.\n' >&2
        printf '%s\n' "$output" >&2
        exit 1
    fi
    log "ensured ticket-board database roles using $RBAC_SQL"
}

smoke_check_http() {
    local url
    url="http://$BOARD_HOST:$BOARD_PORT$SMOKE_PATH"
    python3 - "$url" "$SMOKE_TIMEOUT_SECONDS" <<'PY'
import sys
import time
import urllib.error
import urllib.request

url = sys.argv[1]
deadline = time.monotonic() + float(sys.argv[2])
last_error = "not attempted"
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as response:
            if response.status == 200:
                sys.exit(0)
            last_error = f"HTTP {response.status}"
    except (OSError, urllib.error.URLError) as exc:
        last_error = str(exc)
    time.sleep(0.25)
print(f"ticket-board smoke check failed for {url}: {last_error}", file=sys.stderr)
sys.exit(1)
PY
    log "HTTP smoke check passed at $url"
}

render_unit() {
    cat <<EOF
[Unit]
Description=PGU Ticket Board
After=network.target

[Service]
Type=simple
WorkingDirectory=$BOARD_CURRENT_LINK
RuntimeDirectory=pgu-ticket-board
ExecStartPre=/bin/mkdir -p $FRAME_ROOT
ExecStartPre=/bin/chmod 1777 $FRAME_ROOT
ExecStart=/usr/bin/python3 $BOARD_SCRIPT --host $BOARD_HOST --port $BOARD_PORT --unix-socket $BOARD_UNIX_SOCKET --frames $FRAME_ROOT
Restart=on-failure
RestartSec=2
Environment=PYTHONUNBUFFERED=1
Environment=PGU_TICKET_BOARD_SOCKET=$BOARD_UNIX_SOCKET
Environment=TICKET_BOARD_DATABASE_URL=$BOARD_DATABASE_URL
StandardOutput=append:$LOG_PATH
StandardError=append:$LOG_PATH

[Install]
WantedBy=default.target
EOF
}

write_unit() {
    mkdir -p "$UNIT_DIR"
    render_unit >"$UNIT_PATH"
}

install_service() {
    deploy_export >/dev/null
    [[ -f "$BOARD_SCRIPT" ]] || die "missing board script after deploy: $BOARD_SCRIPT"
    apply_database_migrations
    ensure_database_roles
    ensure_user_manager
    write_unit
    systemctl_user daemon-reload
    systemctl_user enable --now "$SERVICE_NAME"
    smoke_check_http
    log "installed + started $SERVICE_NAME"
}

deploy_service() {
    local deployed_sha
    deployed_sha="$(deploy_export)"
    apply_database_migrations
    printf '%s\n' "$deployed_sha"
}

deploy_restart_service() {
    local deployed_sha
    local scope
    deployed_sha="$(deploy_export)"
    apply_database_migrations
    scope="$(resolved_service_scope)"
    if [[ "$scope" == "system" ]]; then
        quiesce_user_shadow_unit
        if system_unit_reload_required; then
            systemctl_system daemon-reload
        fi
        record_system_unit_hash
        systemctl_system restart "$SERVICE_NAME"
    else
        ensure_user_manager
        write_unit
        systemctl_user daemon-reload
        systemctl_user restart "$SERVICE_NAME"
    fi
    smoke_check_http
    log "deployed $deployed_sha from $DEPLOY_REF and restarted $SERVICE_NAME ($scope scope)"
}

show_logs() {
    touch "$LOG_PATH"
    tail -n 80 -f "$LOG_PATH"
}

main() {
    (($# == 1)) || {
        usage >&2
        exit 1
    }

    case "$1" in
        install)
            install_service
            ;;
        deploy)
            deploy_service
            ;;
        deploy-restart)
            deploy_restart_service
            ;;
        ensure-migrations)
            apply_database_migrations
            ;;
        ensure-roles)
            ensure_database_roles
            ;;
        render-unit)
            render_unit
            ;;
        start|restart)
            if [[ "$(resolved_service_scope)" == "system" ]]; then
                quiesce_user_shadow_unit
                systemctl_system daemon-reload
                systemctl_system "$1" "$SERVICE_NAME"
            else
                ensure_user_manager
                systemctl_user "$1" "$SERVICE_NAME"
            fi
            smoke_check_http
            ;;
        stop|status)
            if [[ "$(resolved_service_scope)" == "system" ]]; then
                systemctl_system "$1" "$SERVICE_NAME"
            else
                ensure_user_manager
                systemctl_user "$1" "$SERVICE_NAME"
            fi
            ;;
        logs)
            show_logs
            ;;
        -h|--help)
            usage
            ;;
        *)
            die "unknown command: $1"
            ;;
    esac
}

main "$@"
