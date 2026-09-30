# Sourced by ticket-board-service.sh; not a program of its own.
#
# Board health: proving that a board answers correctly. It holds the release
# canary, a disposable board started from a candidate release before `current`
# moves (its process or its system unit, its temporary directory, its
# environment file and their cleanup), and the probes of the restarted live
# service: HTTP smoke, build id, commit repositories, the local socket and the
# write-token requirement. When each check runs, and what happens when one
# fails (activation, restart, rollback, the listener), stays with the
# orchestration in ticket-board-service.sh (SYRD-516).
#
# Everything here reads the service script's configuration and helpers (`die`,
# `log`, `systemctl_system`, `system_unit_file_path`) when it is called, so a
# caller that sources the service script and redefines a function still
# replaces it.

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

verify_live_build_id() {
    local expected_sha="$1"
    local url="http://$BOARD_HOST:$BOARD_PORT/api/board"
    if python3 - "$url" "$SMOKE_TIMEOUT_SECONDS" "$expected_sha" <<'PY'
import json
import sys
import time
import urllib.error
import urllib.request

url = sys.argv[1]
deadline = time.monotonic() + float(sys.argv[2])
expected = sys.argv[3]
last_error = "not attempted"
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as response:
            if response.status != 200:
                last_error = f"HTTP {response.status}"
                continue
            payload = json.load(response)
        actual = str(payload.get("build_id", "")).strip()
        if actual == expected:
            sys.exit(0)
        last_error = f"build_id {actual!r} != expected {expected!r}"
    except (OSError, ValueError, urllib.error.URLError) as exc:
        last_error = str(exc)
    time.sleep(0.25)
print(f"ticket-board live build-id check failed for {url}: {last_error}", file=sys.stderr)
sys.exit(1)
PY
    then
        log "live build-id verification passed for $expected_sha"
        return 0
    fi
    return 1
}

# The unit names the commit cache the board must verify against, and the board
# says which one it is actually using. They used to be able to disagree: the
# unit put the cache in `Environment=`, an older `ticket-board.env` beside it
# still set the same variable, and systemd lets the file win -- so an upgrade
# restarted a healthy-looking board that refused every newly published commit
# as unknown (SYRD-251). The unit now passes the cache on the command line,
# where no environment file reaches; this reads the running board back rather
# than trusting that it did.
verify_live_commit_repositories() {
    local scope="$1"
    local unit_path url="http://$BOARD_HOST:$BOARD_PORT/api/client-config"
    if [[ "$scope" == "system" ]]; then
        unit_path="$(system_unit_file_path)" || {
            log "commit cache verification failed: no installed $SERVICE_NAME unit names the managed cache"
            return 1
        }
    else
        unit_path="$UNIT_PATH"
    fi
    if python3 - "$url" "$SMOKE_TIMEOUT_SECONDS" "$unit_path" "$RUNTIME_HOME/.config/$PROJECT_SLUG/ticket-board.env" <<'PY'
import json
import os
import shlex
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

url, timeout, unit_path, env_file = sys.argv[1], float(sys.argv[2]), sys.argv[3], sys.argv[4]
argv_cache = environment_cache = ""
try:
    lines = Path(unit_path).read_text(encoding="utf-8").splitlines()
except OSError as exc:
    print(f"ticket-board commit cache check could not read {unit_path}: {exc.strerror}", file=sys.stderr)
    sys.exit(1)
for line in lines:
    line = line.strip()
    if line.startswith("ExecStart="):
        try:
            words = shlex.split(line[len("ExecStart="):])
        except ValueError:
            words = []
        for index, word in enumerate(words):
            if word == "--commit-git-dir" and index + 1 < len(words):
                argv_cache = words[index + 1]
    elif line.startswith("Environment="):
        name, _, value = line[len("Environment="):].strip().strip('"').partition("=")
        if name.strip() == "TICKET_BOARD_COMMIT_GIT_DIR":
            environment_cache = value.strip().strip('"')
managed = argv_cache or environment_cache
if not managed:
    # Nothing declared: the board resolves its own default, and there is no
    # managed value for anything to have overridden.
    print(f"ticket-board commit cache: {unit_path} names none; the board resolves its default")
    sys.exit(0)
expected = [str(Path(item)) for item in managed.split(os.pathsep) if item.strip()]
deadline = time.monotonic() + timeout
last_error = "not attempted"
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=1.0) as response:
            payload = json.load(response)
        actual = payload.get("commit_repositories")
        if not isinstance(actual, list):
            last_error = "the board does not report commit_repositories"
        elif [str(Path(str(item))) for item in actual] == expected:
            print(f"ticket-board commit cache: the board verifies commits against {os.pathsep.join(expected)}")
            sys.exit(0)
        else:
            # Name the file, never its contents: it holds the report secret.
            last_error = (
                f"the board verifies commits against {os.pathsep.join(map(str, actual))}, "
                f"but {unit_path} manages {os.pathsep.join(expected)}; the service environment "
                f"overrides the unit -- look for TICKET_BOARD_COMMIT_GIT_DIR in {env_file}"
            )
    except (OSError, ValueError, urllib.error.URLError) as exc:
        last_error = str(exc)
    time.sleep(0.25)
print(f"ticket-board commit cache check failed for {url}: {last_error}", file=sys.stderr)
sys.exit(1)
PY
    then
        return 0
    fi
    return 1
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
    if ! "$PYTHON_BIN" "$BOARD_CURRENT_LINK/scripts/ticket-board-socket-smoke" "$BOARD_UNIX_SOCKET" "$SMOKE_TIMEOUT_SECONDS" "$BOARD_CURRENT_LINK"
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
    local asset_dir="$6"
    (
        cd "$release_dir"
        PYTHONUNBUFFERED=1 \
        TICKET_BOARD_PROJECT="$PROJECT_SLUG" \
        TICKET_BOARD_SOCKET="$socket_path" \
        TICKET_BOARD_COMMIT_GIT_DIR="$COMMIT_GIT_DIR" \
        TICKET_BOARD_DATABASE_URL="$BOARD_DATABASE_URL" \
            "$PYTHON_BIN" "$release_dir/scripts/ticket-board.py" \
                --host "$BOARD_HOST" \
                --port "$port" \
                --unix-socket "$socket_path" \
                --frames "$frame_dir" \
                --assets "$asset_dir"
    ) >"$canary_log" 2>&1 &
    printf '%s\n' "$!"
}

stop_canary_direct() {
    local pid="$1"
    kill "$pid" >/dev/null 2>&1 || true
    wait "$pid" >/dev/null 2>&1 || true
}

systemd_env_value() {
    local value="$1"
    [[ "$value" != *$'\n'* ]] || die "canary environment values must not contain newlines"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    printf '"%s"' "$value"
}

write_canary_env_file() {
    local release_dir="$1"
    local port="$2"
    local socket_path="$3"
    local frame_dir="$4"
    local canary_log="$5"
    local asset_dir="$6"
    local tmp_path
    mkdir -p "$(dirname "$BOARD_CANARY_ENV_FILE")"
    tmp_path="$(mktemp "$(dirname "$BOARD_CANARY_ENV_FILE")/.canary-env.XXXXXX")"
    {
        printf 'BOARD_CANARY_RELEASE_DIR=%s\n' "$(systemd_env_value "$release_dir")"
        printf 'BOARD_CANARY_HOST=%s\n' "$(systemd_env_value "$BOARD_HOST")"
        printf 'BOARD_CANARY_PORT=%s\n' "$(systemd_env_value "$port")"
        printf 'BOARD_CANARY_SOCKET=%s\n' "$(systemd_env_value "$socket_path")"
        printf 'BOARD_CANARY_FRAME_DIR=%s\n' "$(systemd_env_value "$frame_dir")"
        printf 'BOARD_CANARY_ASSET_DIR=%s\n' "$(systemd_env_value "$asset_dir")"
        printf 'BOARD_CANARY_LOG=%s\n' "$(systemd_env_value "$canary_log")"
        printf 'BOARD_CANARY_DATABASE_URL=%s\n' "$(systemd_env_value "$BOARD_DATABASE_URL")"
    } >"$tmp_path"
    chmod 0644 "$tmp_path"
    mv "$tmp_path" "$BOARD_CANARY_ENV_FILE"
}

start_canary_systemd() {
    local release_dir="$1"
    local port="$2"
    local socket_path="$3"
    local frame_dir="$4"
    local unit_name="$5"
    local canary_user="$6"
    local canary_log="$7"
    local asset_dir="$8"
    [[ "$unit_name" == "$BOARD_CANARY_SERVICE_NAME" ]] || die "system canary must use $BOARD_CANARY_SERVICE_NAME"
    [[ "$canary_user" == "boardsvc" ]] || die "system canary uses static $BOARD_CANARY_SERVICE_NAME and cannot override BOARD_CANARY_USER"
    write_canary_env_file "$release_dir" "$port" "$socket_path" "$frame_dir" "$canary_log" "$asset_dir"
    systemctl_system stop "$unit_name" >/dev/null 2>&1 || true
    systemctl_system start "$unit_name"
}

stop_canary_systemd() {
    local unit_name="$1"
    systemctl_system stop "$unit_name" >/dev/null 2>&1 || true
}

run_release_canary() {
    local release_dir="$1"
    local scope="$2"
    local canary_user="$BOARD_CANARY_USER"
    local port socket_root socket_path frame_dir asset_dir unit_name current_user canary_log pid="" status=0
    if [[ "${TICKET_BOARD_SKIP_CANARY:-}" == "1" ]]; then
        log "skipping deploy canary because TICKET_BOARD_SKIP_CANARY=1"
        return 0
    fi
    if [[ "$scope" == "user" && -z "$BOARD_CANARY_USER_OVERRIDE" ]]; then
        canary_user="$(id -un)"
    fi
    [[ -x "$release_dir/scripts/ticket-board.py" ]] || die "missing board script in release: $release_dir/scripts/ticket-board.py"
    port="${BOARD_CANARY_PORT:-$(free_tcp_port)}"
    socket_root="$(mktemp -d "${TMPDIR:-/tmp}/$PROJECT_SLUG-ticket-board-canary.XXXXXX")"
    socket_path="${BOARD_CANARY_SOCKET:-$socket_root/ticket-board.sock}"
    frame_dir="$socket_root/frames"
    asset_dir="$socket_root/assets"
    canary_log="$socket_root/canary.log"
    unit_name="$BOARD_CANARY_SERVICE_NAME"
    chmod 1777 "$socket_root"
    mkdir -p "$frame_dir" "$asset_dir"
    chmod 1777 "$frame_dir" "$asset_dir"
    log "starting deploy canary for $release_dir as $canary_user on port $port"
    current_user="$(id -un)"
    if [[ "$current_user" == "$canary_user" ]]; then
        pid="$(start_canary_direct "$release_dir" "$port" "$socket_path" "$frame_dir" "$canary_log" "$asset_dir")"
    else
        start_canary_systemd "$release_dir" "$port" "$socket_path" "$frame_dir" "$unit_name" "$canary_user" "$canary_log" "$asset_dir" || status=$?
    fi
    if (( status == 0 )); then
        smoke_check_url "http://$BOARD_HOST:$port$SMOKE_PATH" "$BOARD_CANARY_TIMEOUT_SECONDS" || status=$?
    fi
    if [[ -n "$pid" ]]; then
        stop_canary_direct "$pid"
    else
        if (( status != 0 )); then
            systemctl_system status "$unit_name" --no-pager -l >&2 || true
        fi
        stop_canary_systemd "$unit_name"
    fi
    if (( status != 0 )); then
        if [[ -s "$canary_log" ]]; then
            log "canary log follows:"
            sed 's/^/[ticket-board-service] canary: /' "$canary_log" >&2
        else
            log "canary log was empty at $canary_log"
        fi
        rm -rf "$socket_root"
        die "canary failed; leaving $BOARD_CURRENT_LINK unchanged"
    fi
    rm -rf "$socket_root"
    log "canary-pass for $release_dir"
}
