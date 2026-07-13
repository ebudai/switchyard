#!/usr/bin/env bash
set -euo pipefail

readonly SERVICE_NAME="pgu-ticket-board-notify-listener.service"
readonly SOURCE_REPO="${SOURCE_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
readonly BOARD_ROOT="${BOARD_ROOT:-/home/agent/pgu-ticketboard-live}"
readonly BOARD_RELEASES_DIR="$BOARD_ROOT/releases"
readonly BOARD_CURRENT_LINK="$BOARD_ROOT/current"
readonly LISTENER_SCRIPT="${LISTENER_SCRIPT:-$BOARD_CURRENT_LINK/scripts/ticket-board-notify-listener}"
readonly LOG_PATH="${LOG_PATH:-/tmp/pgu-ticket-board-notify-listener.log}"
readonly UNIT_DIR="${UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
readonly UNIT_PATH="$UNIT_DIR/$SERVICE_NAME"
readonly DEPLOY_REF="${DEPLOY_REF:-origin/main}"
readonly LISTENER_DATABASE_URL="${TICKET_BOARD_NOTIFY_DATABASE_URL:-${TICKET_BOARD_DATABASE_URL:-postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_listener}}"
readonly BOARD_ADMIN_DATABASE_URL="${TICKET_BOARD_ADMIN_DATABASE_URL:-postgresql:///pgu?host=/var/run/postgresql}"
readonly RBAC_SQL="${RBAC_SQL:-$BOARD_CURRENT_LINK/scripts/ticket_board/rbac.sql}"

usage() {
    cat <<EOF
Usage: scripts/ticket-board-notify-listener-service.sh <install|deploy|deploy-restart|render-unit|start|stop|restart|status|logs>

Manage the PGU ticket-board PostgreSQL LISTEN shim as an agent user systemd service.

Commands:
  install         Export $DEPLOY_REF into $BOARD_ROOT/current, write the unit, enable and start it
  deploy          Refresh $BOARD_ROOT/current from $DEPLOY_REF without restarting the service
  deploy-restart  Refresh $BOARD_ROOT/current from $DEPLOY_REF and restart the service
  render-unit     Print the systemd unit contents to stdout
  start|stop|restart|status
                  Pass through to systemctl --user for $SERVICE_NAME
  logs            Tail $LOG_PATH
EOF
}

die() {
    printf '[ticket-board-notify-listener-service] ERROR: %s\n' "$*" >&2
    exit 1
}

log() {
    printf '[ticket-board-notify-listener-service] %s\n' "$*" >&2
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

ensure_database_roles() {
    [[ -f "$RBAC_SQL" ]] || die "missing ticket-board RBAC SQL after deploy: $RBAC_SQL"
    command -v psql >/dev/null 2>&1 || die "psql is required to ensure ticket-board service roles"
    psql -X -v ON_ERROR_STOP=1 "$BOARD_ADMIN_DATABASE_URL" -f "$RBAC_SQL" >/dev/null
    log "ensured ticket-board database roles using $RBAC_SQL"
}

render_unit() {
    cat <<EOF
[Unit]
Description=PGU Ticket Board Notify Listener
After=network.target

[Service]
Type=simple
WorkingDirectory=$BOARD_CURRENT_LINK
ExecStart=/usr/bin/python3 $LISTENER_SCRIPT
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1
Environment=PGHOST=/var/run/postgresql
Environment=PGDATABASE=pgu
Environment=PGUSER=ticket_board_listener
Environment=TICKET_BOARD_DATABASE_URL=$LISTENER_DATABASE_URL
Environment=TICKET_BOARD_NOTIFY_DATABASE_URL=$LISTENER_DATABASE_URL
Environment=PGU_TICKET_BOARD_PANE_STATE_DIR=%t/pgu-ticket-board/pane-state
EnvironmentFile=-%h/.config/pgu/ticket-board-notify-listener.env
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
    [[ -f "$LISTENER_SCRIPT" ]] || die "missing listener script after deploy: $LISTENER_SCRIPT"
    ensure_database_roles
    ensure_user_manager
    write_unit
    systemctl_user daemon-reload
    systemctl_user enable --now "$SERVICE_NAME"
    log "installed + started $SERVICE_NAME"
}

deploy_restart_service() {
    local deployed_sha
    deployed_sha="$(deploy_export)"
    ensure_user_manager
    write_unit
    systemctl_user daemon-reload
    systemctl_user restart "$SERVICE_NAME"
    log "deployed $deployed_sha from $DEPLOY_REF and restarted $SERVICE_NAME"
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
            deploy_export
            ;;
        deploy-restart)
            deploy_restart_service
            ;;
        render-unit)
            render_unit
            ;;
        start|stop|restart|status)
            ensure_user_manager
            systemctl_user "$1" "$SERVICE_NAME"
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
