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
readonly SMOKE_PATH="${BOARD_SMOKE_PATH:-/api/board}"
readonly SMOKE_TIMEOUT_SECONDS="${BOARD_SMOKE_TIMEOUT_SECONDS:-10}"
readonly BOARD_CANARY_USER_OVERRIDE="${BOARD_CANARY_USER:-}"
readonly BOARD_CANARY_USER="${BOARD_CANARY_USER:-boardsvc}"
readonly BOARD_CANARY_PORT="${BOARD_CANARY_PORT:-}"
readonly BOARD_CANARY_TIMEOUT_SECONDS="${BOARD_CANARY_TIMEOUT_SECONDS:-$SMOKE_TIMEOUT_SECONDS}"
readonly BOARD_CANARY_SOCKET="${BOARD_CANARY_SOCKET:-}"
readonly BOARD_CANARY_UNIT_PREFIX="${BOARD_CANARY_UNIT_PREFIX:-pgu-ticket-board-canary}"
readonly PYTHON_BIN="${TICKET_BOARD_PYTHON:-/usr/bin/python3}"
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

is_board_service_systemctl_action() {
    case "${1:-}" in
        start|stop|restart|status)
            [[ "${2:-}" == "$SERVICE_NAME" && "$#" -eq 2 ]]
            ;;
        *)
            return 1
            ;;
    esac
}

systemctl_system() {
    if [[ "$(id -u)" == "0" ]]; then
        systemctl "$@"
        return
    fi
    # Eric installs /etc/polkit-1/rules.d/49-pgu-board-restart.rules on the
    # host to allow agent to manage this unit without an interactive KDE prompt.
    if is_board_service_systemctl_action "$@"; then
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

deploy_export_release() {
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
    printf '%s\t%s\n' "$resolved_ref" "$release_dir"
}

activate_release() {
    local release_dir="$1"
    ln -sfn "$release_dir" "$BOARD_CURRENT_LINK"
}

current_release_dir() {
    if [[ -L "$BOARD_CURRENT_LINK" || -e "$BOARD_CURRENT_LINK" ]]; then
        readlink -f "$BOARD_CURRENT_LINK"
    fi
}

deploy_export() {
    local deployed_sha release_dir
    IFS=$'\t' read -r deployed_sha release_dir < <(deploy_export_release)
    activate_release "$release_dir"
    printf '%s\n' "$deployed_sha"
}

apply_database_migrations_for_release() {
    local release_dir="$1"
    local migration_runner="$release_dir/scripts/ticket-board-migrate"
    if [[ "${TICKET_BOARD_SKIP_MIGRATIONS:-}" == "1" ]]; then
        log "skipping ticket-board migrations because TICKET_BOARD_SKIP_MIGRATIONS=1"
        return 0
    fi
    [[ -x "$migration_runner" ]] || die "missing executable migration runner after deploy: $migration_runner"
    TICKET_BOARD_ADMIN_DATABASE_URL="$BOARD_ADMIN_DATABASE_URL" "$migration_runner"
    log "applied ticket-board database migrations using $migration_runner"
}

apply_database_migrations() {
    apply_database_migrations_for_release "$BOARD_CURRENT_LINK"
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

smoke_check_url() {
    local url="$1"
    local timeout_seconds="$2"
    if python3 - "$url" "$timeout_seconds" <<'PY'
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
    then
        log "HTTP smoke check passed at $url"
        return 0
    fi
    return 1
}

smoke_check_http() {
    smoke_check_url "http://$BOARD_HOST:$BOARD_PORT$SMOKE_PATH" "$SMOKE_TIMEOUT_SECONDS"
}

verify_local_socket_available() {
    local socket_dir
    if [[ "${TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY:-}" == "1" ]]; then
        log "skipping post-deploy socket verification because TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY=1"
        return 0
    fi
    socket_dir="$(dirname "$BOARD_UNIX_SOCKET")"
    if [[ ! -d "$socket_dir" ]]; then
        log "post-deploy socket verification failed: missing runtime directory $socket_dir"
        return 1
    fi
    if [[ ! -S "$BOARD_UNIX_SOCKET" ]]; then
        log "post-deploy socket verification failed: missing Unix socket $BOARD_UNIX_SOCKET"
        return 1
    fi
    if ! "$PYTHON_BIN" - "$BOARD_UNIX_SOCKET" "$SMOKE_TIMEOUT_SECONDS" <<'PY'
import json
import socket
import sys
import time

socket_path = sys.argv[1]
deadline = time.monotonic() + float(sys.argv[2])
body = json.dumps({"role": "ops"}).encode("utf-8")
request = (
    b"POST /api/register-caller HTTP/1.1\r\n"
    b"Host: localhost\r\n"
    b"Content-Type: application/json\r\n"
    + f"Content-Length: {len(body)}\r\n".encode("ascii")
    + b"Connection: close\r\n\r\n"
    + body
)
last_error = "not attempted"
while time.monotonic() < deadline:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(1.0)
            sock.connect(socket_path)
            sock.sendall(request)
            response = sock.recv(4096)
        status_line = response.split(b"\r\n", 1)[0]
        if b" 200 " in status_line:
            sys.exit(0)
        last_error = status_line.decode("utf-8", errors="replace")
    except OSError as exc:
        last_error = str(exc)
    time.sleep(0.25)
print(f"ticket-board Unix socket verification failed for {socket_path}: {last_error}", file=sys.stderr)
sys.exit(1)
PY
    then
        return 1
    fi
    log "Unix socket verification passed at $BOARD_UNIX_SOCKET"
}

verify_http_write_token_required() {
    if [[ "${TICKET_BOARD_SKIP_POST_DEPLOY_SOCKET_VERIFY:-}" == "1" ]]; then
        return 0
    fi
    if ! "$PYTHON_BIN" - "http://$BOARD_HOST:$BOARD_PORT/api/register-caller" "$SMOKE_TIMEOUT_SECONDS" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

url = sys.argv[1]
deadline = time.monotonic() + float(sys.argv[2])
body = json.dumps({"role": "ops"}).encode("utf-8")
last_error = "not attempted"
while time.monotonic() < deadline:
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(request, timeout=1.0).close()
        last_error = "HTTP write unexpectedly succeeded without X-PGU-Write-Token"
    except urllib.error.HTTPError as exc:
        if exc.code == 403:
            sys.exit(0)
        last_error = f"HTTP {exc.code}"
    except (OSError, urllib.error.URLError) as exc:
        last_error = str(exc)
    time.sleep(0.25)
print(f"ticket-board HTTP token verification failed for {url}: {last_error}", file=sys.stderr)
sys.exit(1)
PY
    then
        return 1
    fi
    log "HTTP write-token verification passed"
}

verify_post_deploy_system_runtime() {
    verify_local_socket_available && verify_http_write_token_required
}

free_tcp_port() {
    "$PYTHON_BIN" - <<'PY'
import socket

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

start_canary_direct() {
    local release_dir="$1"
    local port="$2"
    local socket_path="$3"
    local frame_dir="$4"
    local canary_log="$5"
    (
        cd "$release_dir"
        PYTHONUNBUFFERED=1 \
        PGU_TICKET_BOARD_SOCKET="$socket_path" \
        TICKET_BOARD_DATABASE_URL="$BOARD_DATABASE_URL" \
            "$PYTHON_BIN" "$release_dir/scripts/ticket-board.py" \
                --host "$BOARD_HOST" \
                --port "$port" \
                --unix-socket "$socket_path" \
                --frames "$frame_dir"
    ) >"$canary_log" 2>&1 &
    printf '%s\n' "$!"
}

stop_canary_direct() {
    local pid="$1"
    kill "$pid" >/dev/null 2>&1 || true
    wait "$pid" >/dev/null 2>&1 || true
}

start_canary_systemd() {
    local release_dir="$1"
    local port="$2"
    local socket_path="$3"
    local frame_dir="$4"
    local unit_name="$5"
    local canary_user="$6"
    local session_check_status=0
    if [[ "$(id -u)" != "0" ]]; then
        polkit_graphical_session_available "$POLKIT_APPROVAL_USER" || session_check_status=$?
        if [[ "$session_check_status" == "1" ]]; then
            die "system service action requires polkit approval from $POLKIT_APPROVAL_USER's active graphical session; run this from that session so the KDE prompt can appear"
        fi
    fi
    systemd-run \
        --quiet \
        --collect \
        --unit "$unit_name" \
        --uid "$canary_user" \
        --property "WorkingDirectory=$release_dir" \
        --setenv "PYTHONUNBUFFERED=1" \
        --setenv "PGU_TICKET_BOARD_SOCKET=$socket_path" \
        --setenv "TICKET_BOARD_DATABASE_URL=$BOARD_DATABASE_URL" \
        "$PYTHON_BIN" "$release_dir/scripts/ticket-board.py" \
            --host "$BOARD_HOST" \
            --port "$port" \
            --unix-socket "$socket_path" \
            --frames "$frame_dir"
}

stop_canary_systemd() {
    local unit_name="$1"
    systemctl_system stop "$unit_name" >/dev/null 2>&1 || true
}

run_release_canary() {
    local release_dir="$1"
    local scope="$2"
    local canary_user="$BOARD_CANARY_USER"
    local port socket_root socket_path frame_dir unit_name current_user canary_log pid="" status=0
    if [[ "${TICKET_BOARD_SKIP_CANARY:-}" == "1" ]]; then
        log "skipping deploy canary because TICKET_BOARD_SKIP_CANARY=1"
        return 0
    fi
    if [[ "$scope" == "user" && -z "$BOARD_CANARY_USER_OVERRIDE" ]]; then
        canary_user="$(id -un)"
    fi
    [[ -x "$release_dir/scripts/ticket-board.py" ]] || die "missing board script in release: $release_dir/scripts/ticket-board.py"
    port="${BOARD_CANARY_PORT:-$(free_tcp_port)}"
    socket_root="$(mktemp -d "${TMPDIR:-/tmp}/pgu-ticket-board-canary.XXXXXX")"
    socket_path="${BOARD_CANARY_SOCKET:-$socket_root/ticket-board.sock}"
    frame_dir="$socket_root/frames"
    canary_log="$socket_root/canary.log"
    unit_name="$BOARD_CANARY_UNIT_PREFIX-$$"
    chmod 1777 "$socket_root"
    mkdir -p "$frame_dir"
    chmod 1777 "$frame_dir"
    log "starting deploy canary for $release_dir as $canary_user on port $port"
    current_user="$(id -un)"
    if [[ "$current_user" == "$canary_user" ]]; then
        pid="$(start_canary_direct "$release_dir" "$port" "$socket_path" "$frame_dir" "$canary_log")"
    else
        start_canary_systemd "$release_dir" "$port" "$socket_path" "$frame_dir" "$unit_name" "$canary_user"
    fi
    smoke_check_url "http://$BOARD_HOST:$port$SMOKE_PATH" "$BOARD_CANARY_TIMEOUT_SECONDS" || status=$?
    if [[ -n "$pid" ]]; then
        stop_canary_direct "$pid"
    else
        stop_canary_systemd "$unit_name"
    fi
    rm -rf "$socket_root"
    if (( status != 0 )); then
        die "canary failed; leaving $BOARD_CURRENT_LINK unchanged"
    fi
    log "canary-pass for $release_dir"
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

render_system_unit() {
    local production_unit="$BOARD_CURRENT_LINK/deploy/systemd/pgu-ticket-board.service.boardsvc"
    if [[ -f "$production_unit" ]]; then
        cat "$production_unit"
        return
    fi
    render_unit
}

write_unit() {
    mkdir -p "$UNIT_DIR"
    render_unit >"$UNIT_PATH"
}

install_system_unit_file() {
    local source_path="$1"
    local destination_path="$2"
    local destination_dir
    destination_dir="$(dirname "$destination_path")"
    if [[ "$(id -u)" == "0" ]]; then
        install -D -m 0644 "$source_path" "$destination_path"
        return
    fi
    if [[ -d "$destination_dir" && -w "$destination_dir" && ( ! -e "$destination_path" || -w "$destination_path" ) ]]; then
        install -D -m 0644 "$source_path" "$destination_path"
        return
    fi
    polkit_graphical_session_available "$POLKIT_APPROVAL_USER" || die "system unit install requires polkit approval from $POLKIT_APPROVAL_USER's active graphical session; run this from that session so the KDE prompt can appear"
    command -v pkexec >/dev/null 2>&1 || die "system unit install requires pkexec because $destination_path is not writable"
    local output status timeout_args=()
    if command -v timeout >/dev/null 2>&1; then
        timeout_args=(timeout --foreground "${POLKIT_TIMEOUT_SECONDS}s")
    fi
    if output="$("${timeout_args[@]}" pkexec /usr/bin/install -D -m 0644 "$source_path" "$destination_path" 2>&1)"; then
        [[ -z "$output" ]] || printf '%s\n' "$output"
        return
    else
        status=$?
    fi
    [[ -z "$output" ]] || printf '%s\n' "$output" >&2
    if [[ "$status" == "124" ]]; then
        die "installing $destination_path timed out waiting for polkit approval"
    fi
    return "$status"
}

install_system_unit_if_changed() {
    local rendered_unit
    rendered_unit="$(mktemp "${TMPDIR:-/tmp}/pgu-ticket-board-unit.XXXXXX")"
    render_system_unit >"$rendered_unit"
    if [[ -f "$SYSTEM_UNIT_PATH" ]] && cmp -s "$rendered_unit" "$SYSTEM_UNIT_PATH"; then
        rm -f "$rendered_unit"
        return 1
    fi
    install_system_unit_file "$rendered_unit" "$SYSTEM_UNIT_PATH"
    rm -f "$rendered_unit"
    log "installed updated system unit at $SYSTEM_UNIT_PATH"
    return 0
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
    local deployed_sha release_dir previous_release scope
    IFS=$'\t' read -r deployed_sha release_dir < <(deploy_export_release)
    previous_release="$(current_release_dir || true)"
    scope="$(resolved_service_scope)"
    apply_database_migrations_for_release "$release_dir"
    run_release_canary "$release_dir" "$scope"
    activate_release "$release_dir"
    restart_live_service "$scope"
    if ! smoke_check_http; then
        rollback_live_service "$scope" "$previous_release"
        exit 1
    fi
    if [[ "$scope" == "system" ]]; then
        if ! verify_post_deploy_system_runtime; then
            rollback_live_service "$scope" "$previous_release"
            exit 1
        fi
    fi
    log "deployed $deployed_sha from $DEPLOY_REF and restarted $SERVICE_NAME ($scope scope)"
}

restart_live_service() {
    local scope="$1"
    if [[ "$scope" == "system" ]]; then
        local unit_installed=0
        quiesce_user_shadow_unit
        if install_system_unit_if_changed; then
            unit_installed=1
        fi
        if (( unit_installed )) || system_unit_reload_required; then
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
}

rollback_live_service() {
    local scope="$1"
    local previous_release="$2"
    if [[ -z "$previous_release" || ! -d "$previous_release" ]]; then
        die "post-restart smoke failed and no previous release is available for rollback"
    fi
    log "post-restart smoke failed; rolling back current to $previous_release"
    activate_release "$previous_release"
    restart_live_service "$scope"
    smoke_check_http
    log "rollback-pass; restored $previous_release"
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

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
