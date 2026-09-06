#!/usr/bin/env bash
set -euo pipefail

PROJECT_SLUG="${TICKET_BOARD_PROJECT:-${PGU_TICKET_BOARD_PROJECT:-pgu}}"
PROJECT_DB_IDENT="${PROJECT_SLUG//-/_}"
DEFAULT_DATABASE_NAME="$PROJECT_DB_IDENT"
if [[ "$PROJECT_SLUG" != "pgu" ]]; then
    DEFAULT_DATABASE_NAME="${PROJECT_DB_IDENT}_ticket_board"
fi
readonly PROJECT_SLUG PROJECT_DB_IDENT DEFAULT_DATABASE_NAME
readonly SERVICE_NAME="${TICKET_BOARD_NOTIFY_LISTENER_SERVICE_NAME:-$PROJECT_SLUG-ticket-board-notify-listener.service}"
readonly SOURCE_REPO="${SOURCE_REPO:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
readonly OWNER_HOME="${TICKET_BOARD_OWNER_HOME:-}"
readonly BOARD_ROOT="${BOARD_ROOT:-${OWNER_HOME:+$OWNER_HOME/$PROJECT_SLUG-ticketboard-live}}"
readonly BOARD_RELEASES_DIR="$BOARD_ROOT/releases"
readonly BOARD_CURRENT_LINK="$BOARD_ROOT/current"
readonly LISTENER_SCRIPT="${LISTENER_SCRIPT:-$BOARD_CURRENT_LINK/scripts/ticket-board-notify-listener}"
readonly LOG_PATH="${LOG_PATH:-/tmp/$PROJECT_SLUG-ticket-board-notify-listener.log}"
readonly UNIT_DIR="${UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
readonly UNIT_PATH="$UNIT_DIR/$SERVICE_NAME"
readonly DEPLOY_REF="${DEPLOY_REF:-origin/main}"
readonly SWITCHYARD_RELEASE_MARKER_NAME=".switchyard-release.json"
readonly LISTENER_DATABASE_URL="${TICKET_BOARD_NOTIFY_DATABASE_URL:-${TICKET_BOARD_DATABASE_URL:-postgresql:///$DEFAULT_DATABASE_NAME?host=/var/run/postgresql&user=ticket_board_listener}}"
readonly BOARD_ADMIN_DATABASE_URL="${TICKET_BOARD_ADMIN_DATABASE_URL:-postgresql:///$DEFAULT_DATABASE_NAME?host=/var/run/postgresql&user=postgres}"
readonly RBAC_SQL="${RBAC_SQL:-$BOARD_CURRENT_LINK/scripts/ticket_board/rbac.sql}"
readonly MIGRATION_RUNNER="${TICKET_BOARD_MIGRATION_RUNNER:-$BOARD_CURRENT_LINK/scripts/ticket-board-migrate}"
readonly SWITCHYARD_SHARED_PYTHON="${SWITCHYARD_SHARED_PYTHON:-/opt/switchyard/venv/bin/python}"
DEFAULT_TICKET_BOARD_PYTHON="/usr/bin/python3"
if [[ -x "$SWITCHYARD_SHARED_PYTHON" ]]; then
    DEFAULT_TICKET_BOARD_PYTHON="$SWITCHYARD_SHARED_PYTHON"
fi
readonly DEFAULT_TICKET_BOARD_PYTHON
readonly PYTHON_BIN="${TICKET_BOARD_PYTHON:-$DEFAULT_TICKET_BOARD_PYTHON}"

usage() {
    cat <<EOF
Usage: scripts/ticket-board-notify-listener-service.sh <install|deploy|deploy-restart|ensure-migrations|ensure-roles|render-unit|start|stop|restart|status|logs>

Manage a ticket-board PostgreSQL LISTEN shim as a user systemd service.

Commands:
  install         Export $DEPLOY_REF into $BOARD_ROOT/current, write the unit, enable and start it
  deploy          Refresh $BOARD_ROOT/current from $DEPLOY_REF without restarting the service
  deploy-restart  Refresh $BOARD_ROOT/current from $DEPLOY_REF and restart the service
  ensure-migrations
                  Apply SQL migrations and record their names in schema_migrations
  ensure-roles    Apply the idempotent ticket-board RBAC SQL using a privileged admin connection
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

require_tenant_paths() {
    [[ -n "$BOARD_ROOT" ]] || die "BOARD_ROOT is required (or set TICKET_BOARD_OWNER_HOME so the tenant release root can be derived); refusing to select another tenant's home"
    [[ "$BOARD_ROOT" == /* ]] || die "BOARD_ROOT must be an absolute tenant path: $BOARD_ROOT"
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

source_repo_owner() {
    stat -c '%U' "$SOURCE_REPO" 2>/dev/null || true
}

git_source() {
    local owner
    owner="$(source_repo_owner)"
    if [[ "$(id -u)" == "0" && -n "$owner" && "$owner" != "root" && "$owner" != "UNKNOWN" ]]; then
        sudo -u "$owner" -H git -C "$SOURCE_REPO" "$@"
        return
    fi
    git -c "safe.directory=$SOURCE_REPO" -C "$SOURCE_REPO" "$@"
}

ensure_source_repo() {
    [[ -d "$SOURCE_REPO" ]] || die "missing source repo path: $SOURCE_REPO"
    local output
    if ! output="$(git_source rev-parse --show-toplevel 2>&1)"; then
        [[ -z "$output" ]] || printf '%s\n' "$output" >&2
        die "source repo is not a readable git checkout or Switchyard release: $SOURCE_REPO"
    fi
}

source_release_commit() {
    local marker="$SOURCE_REPO/$SWITCHYARD_RELEASE_MARKER_NAME"
    [[ -f "$marker" ]] || return 1
    python3 - "$marker" <<'PY'
import json
import sys

marker = sys.argv[1]
try:
    with open(marker, encoding="utf-8") as handle:
        payload = json.load(handle)
except (OSError, json.JSONDecodeError) as exc:
    print(exc, file=sys.stderr)
    raise SystemExit(2)

commit = str(payload.get("commit") or "").strip()
if not commit:
    print(f"{marker} has no commit", file=sys.stderr)
    raise SystemExit(2)
print(commit)
PY
}

maybe_fetch_origin() {
    if git_source remote get-url origin >/dev/null 2>&1; then
        local output
        if ! output="$(git_source fetch origin 2>&1)"; then
            [[ -z "$output" ]] || printf '%s\n' "$output" >&2
            die "failed to fetch origin in $SOURCE_REPO; refusing stale deploy of $DEPLOY_REF"
        fi
    fi
}

verify_release_sha() {
    local release_dir="$1"
    local expected_sha="$2"
    local marker="$release_dir/.pgu-deploy-sha"
    local actual_sha
    [[ -f "$marker" ]] || die "release $release_dir is missing deploy sha marker"
    actual_sha="$(<"$marker")"
    [[ "$actual_sha" == "$expected_sha" ]] || die "release $release_dir sha marker mismatch: expected $expected_sha, got $actual_sha"
}

verify_current_release_sha() {
    local expected_sha="$1"
    verify_release_sha "$BOARD_CURRENT_LINK" "$expected_sha"
}

deploy_export() {
    local release_commit release_commit_status=0 resolved_ref release_dir source_kind tmp_dir
    release_commit="$(source_release_commit)" || release_commit_status=$?
    if [[ "$release_commit_status" == "0" ]]; then
        resolved_ref="$release_commit"
        source_kind="release"
    else
        [[ "$release_commit_status" == "1" ]] || die "source release marker is invalid: $SOURCE_REPO/$SWITCHYARD_RELEASE_MARKER_NAME"
        ensure_source_repo
        maybe_fetch_origin
        resolved_ref="$(git_source rev-parse --verify "$DEPLOY_REF^{commit}")" || die "failed to resolve $DEPLOY_REF in $SOURCE_REPO"
        source_kind="git"
    fi
    mkdir -p "$BOARD_RELEASES_DIR"
    release_dir="$BOARD_RELEASES_DIR/$resolved_ref"
    if [[ ! -d "$release_dir" ]]; then
        tmp_dir="$BOARD_RELEASES_DIR/.tmp-$resolved_ref.$$"
        rm -rf "$tmp_dir"
        mkdir -p "$tmp_dir"
        if [[ "$source_kind" == "release" ]]; then
            tar -C "$SOURCE_REPO" --exclude='./.git' -cf - . | tar -x -C "$tmp_dir"
        else
            git_source archive "$resolved_ref" | tar -x -C "$tmp_dir"
        fi
        printf '%s\n' "$resolved_ref" >"$tmp_dir/.pgu-deploy-sha"
        mv "$tmp_dir" "$release_dir"
    fi
    verify_release_sha "$release_dir" "$resolved_ref"
    ln -sfn "$release_dir" "$BOARD_CURRENT_LINK"
    verify_current_release_sha "$resolved_ref"
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
        printf '[ticket-board-notify-listener-service] ERROR: failed to ensure ticket-board database roles using TICKET_BOARD_ADMIN_DATABASE_URL. This must connect as a PostgreSQL role with CREATEROLE (default: user=postgres); override TICKET_BOARD_ADMIN_DATABASE_URL for nonstandard clusters.\n' >&2
        printf '%s\n' "$output" >&2
        exit 1
    fi
    log "ensured ticket-board database roles using $RBAC_SQL"
}

render_unit() {
    cat <<EOF
[Unit]
Description=$PROJECT_SLUG Ticket Board Notify Listener
After=network.target

[Service]
Type=simple
WorkingDirectory=$BOARD_CURRENT_LINK
ExecStart=$PYTHON_BIN $LISTENER_SCRIPT --directorctl $BOARD_CURRENT_LINK/scripts/directorctl
Restart=always
RestartSec=2
Environment=PYTHONUNBUFFERED=1
Environment=PGHOST=/var/run/postgresql
Environment=PGDATABASE=$DEFAULT_DATABASE_NAME
Environment=PGUSER=ticket_board_listener
Environment=TICKET_BOARD_DATABASE_URL=$LISTENER_DATABASE_URL
Environment=TICKET_BOARD_NOTIFY_DATABASE_URL=$LISTENER_DATABASE_URL
Environment=TICKET_BOARD_PROJECT=$PROJECT_SLUG
Environment=TICKET_BOARD_DIRECTORCTL=$BOARD_CURRENT_LINK/scripts/directorctl
Environment=TICKET_BOARD_PANE_STATE_DIR=%t/$PROJECT_SLUG-ticket-board/pane-state
EnvironmentFile=-%h/.config/$PROJECT_SLUG/ticket-board-notify-listener.env
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
    apply_database_migrations
    ensure_database_roles
    ensure_user_manager
    write_unit
    systemctl_user daemon-reload
    systemctl_user enable --now "$SERVICE_NAME"
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
    deployed_sha="$(deploy_export)"
    apply_database_migrations
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
        -h|--help)
            usage
            return
            ;;
    esac
    if [[ "$1" != "ensure-roles" ]]; then
        require_tenant_paths
    fi
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
        start|stop|restart|status)
            ensure_user_manager
            systemctl_user "$1" "$SERVICE_NAME"
            ;;
        logs)
            show_logs
            ;;
        *)
            die "unknown command: $1"
            ;;
    esac
}

main "$@"
