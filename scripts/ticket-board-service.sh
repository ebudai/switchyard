#!/usr/bin/env bash
set -euo pipefail

PROJECT_SLUG="${TICKET_BOARD_PROJECT:-${PGU_TICKET_BOARD_PROJECT:-pgu}}"
PROJECT_DB_IDENT="${PROJECT_SLUG//-/_}"
DEFAULT_DATABASE_NAME="$PROJECT_DB_IDENT"
if [[ "$PROJECT_SLUG" != "pgu" ]]; then
    DEFAULT_DATABASE_NAME="${PROJECT_DB_IDENT}_ticket_board"
fi
readonly PROJECT_SLUG PROJECT_DB_IDENT DEFAULT_DATABASE_NAME
readonly SERVICE_NAME="${TICKET_BOARD_SERVICE_NAME:-$PROJECT_SLUG-ticket-board.service}"
readonly SERVICE_SCRIPT_REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
readonly SOURCE_REPO="${SOURCE_REPO:-$SERVICE_SCRIPT_REPO}"
resolve_commit_git_dir() {
    if [[ -n "${TICKET_BOARD_COMMIT_GIT_DIR:-}" ]]; then
        printf '%s\n' "$TICKET_BOARD_COMMIT_GIT_DIR"
        return
    fi
    local python_for_resolver
    python_for_resolver="/usr/bin/python3"
    if [[ ! -x "$python_for_resolver" ]]; then
        python_for_resolver="python3"
    fi
    PYTHONPATH="$SERVICE_SCRIPT_REPO/scripts:$SOURCE_REPO/scripts${PYTHONPATH:+:$PYTHONPATH}" "$python_for_resolver" - "$PROJECT_SLUG" "${HOME:-}" <<'PY'
import sys
from pathlib import Path

from ticket_board.commit_repos import commit_git_dir_env_for_project

project = sys.argv[1]
home = sys.argv[2] or str(Path.home())
print(commit_git_dir_env_for_project(project=project, owner_home=home))
PY
}
COMMIT_GIT_DIR="$(resolve_commit_git_dir)"
readonly COMMIT_GIT_DIR
readonly OWNER_HOME="${TICKET_BOARD_OWNER_HOME:-}"
readonly RUNTIME_HOME="${OWNER_HOME:-$HOME}"
readonly BOARD_ROOT="${BOARD_ROOT:-${OWNER_HOME:+$OWNER_HOME/$PROJECT_SLUG-ticketboard-live}}"
readonly BOARD_RELEASES_DIR="$BOARD_ROOT/releases"
readonly BOARD_CURRENT_LINK="$BOARD_ROOT/current"
readonly BOARD_SCRIPT="${BOARD_SCRIPT:-$BOARD_CURRENT_LINK/scripts/ticket-board.py}"
readonly BOARD_HOST="${BOARD_HOST:-127.0.0.1}"
readonly BOARD_PORT="${BOARD_PORT:-8770}"
readonly BOARD_UNIX_SOCKET="${BOARD_UNIX_SOCKET:-/run/$PROJECT_SLUG-ticket-board/ticket-board.sock}"
readonly SMOKE_PATH="${BOARD_SMOKE_PATH:-/api/board}"
readonly SMOKE_TIMEOUT_SECONDS="${BOARD_SMOKE_TIMEOUT_SECONDS:-10}"
readonly BOARD_CANARY_USER_OVERRIDE="${BOARD_CANARY_USER:-}"
readonly BOARD_CANARY_USER="${BOARD_CANARY_USER:-boardsvc}"
readonly BOARD_CANARY_PORT="${BOARD_CANARY_PORT:-}"
readonly BOARD_CANARY_TIMEOUT_SECONDS="${BOARD_CANARY_TIMEOUT_SECONDS:-$SMOKE_TIMEOUT_SECONDS}"
readonly BOARD_CANARY_SOCKET="${BOARD_CANARY_SOCKET:-}"
readonly BOARD_CANARY_SERVICE_NAME="${BOARD_CANARY_SERVICE_NAME:-$PROJECT_SLUG-ticket-board-canary.service}"
readonly BOARD_CANARY_ENV_FILE="${BOARD_CANARY_ENV_FILE:-$BOARD_ROOT/canary.env}"
readonly SWITCHYARD_SHARED_PYTHON="${SWITCHYARD_SHARED_PYTHON:-/opt/switchyard/venv/bin/python}"
DEFAULT_TICKET_BOARD_PYTHON="/usr/bin/python3"
if [[ -x "$SWITCHYARD_SHARED_PYTHON" ]]; then
    DEFAULT_TICKET_BOARD_PYTHON="$SWITCHYARD_SHARED_PYTHON"
fi
readonly DEFAULT_TICKET_BOARD_PYTHON
readonly PYTHON_BIN="${TICKET_BOARD_PYTHON:-$DEFAULT_TICKET_BOARD_PYTHON}"
if [[ -z "${FRAME_ROOT:-}" ]]; then
    if [[ "$PROJECT_SLUG" == "pgu" ]]; then
        FRAME_ROOT="/tmp/pgu-frames"
    else
        FRAME_ROOT="$RUNTIME_HOME/.claude/$PROJECT_SLUG-ticket-frames"
    fi
fi
readonly FRAME_ROOT
readonly LOG_PATH="${LOG_PATH:-/tmp/$PROJECT_SLUG-ticket-board.log}"
readonly UNIT_DIR="${UNIT_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user}"
readonly UNIT_PATH="$UNIT_DIR/$SERVICE_NAME"
readonly SYSTEM_UNIT_PATH="${TICKET_BOARD_SYSTEM_UNIT_PATH:-/etc/systemd/system/$SERVICE_NAME}"
readonly PROVISIONED_SYSTEM_UNIT="${TICKET_BOARD_PROVISIONED_SYSTEM_UNIT:-}"
readonly SYSTEM_UNIT_HASH_RECORD="${TICKET_BOARD_SYSTEM_UNIT_HASH_RECORD:-$BOARD_ROOT/system-unit.sha256}"
readonly DEPLOY_REF="${DEPLOY_REF:-origin/main}"
readonly SWITCHYARD_RELEASE_MARKER_NAME=".switchyard-release.json"
readonly BOARD_DATABASE_URL="${TICKET_BOARD_DATABASE_URL:-postgresql:///$DEFAULT_DATABASE_NAME?host=/var/run/postgresql&user=ticket_board_service}"
readonly BOARD_ADMIN_DATABASE_URL="${TICKET_BOARD_ADMIN_DATABASE_URL:-postgresql:///$DEFAULT_DATABASE_NAME?host=/var/run/postgresql&user=postgres}"
readonly RBAC_SQL="${RBAC_SQL:-$BOARD_CURRENT_LINK/scripts/ticket_board/rbac.sql}"
readonly MIGRATION_RUNNER="${TICKET_BOARD_MIGRATION_RUNNER:-$BOARD_CURRENT_LINK/scripts/ticket-board-migrate}"
readonly SERVICE_SCOPE="${TICKET_BOARD_SERVICE_SCOPE:-auto}"
readonly POLKIT_APPROVAL_USER="${TICKET_BOARD_POLKIT_APPROVAL_USER:-eric}"
readonly POLKIT_TIMEOUT_SECONDS="${TICKET_BOARD_POLKIT_TIMEOUT_SECONDS:-30}"

usage() {
    cat <<EOF
Usage: scripts/ticket-board-service.sh <install|deploy|deploy-restart|ensure-migrations|ensure-roles|render-unit|start|stop|restart|status|logs>

Manage a ticket board service.

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

require_tenant_paths() {
    [[ -n "$BOARD_ROOT" ]] || die "BOARD_ROOT is required (or set TICKET_BOARD_OWNER_HOME so the tenant release root can be derived); refusing to select another tenant's home"
    [[ "$BOARD_ROOT" == /* ]] || die "BOARD_ROOT must be an absolute tenant path: $BOARD_ROOT"
    if [[ -n "$OWNER_HOME" ]]; then
        [[ "$OWNER_HOME" == /* ]] || die "TICKET_BOARD_OWNER_HOME must be absolute: $OWNER_HOME"
    fi
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
        start)
            [[ "$#" -eq 2 ]] || return 1
            [[ "${2:-}" == "$SERVICE_NAME" ]] || [[ "${2:-}" == "$BOARD_CANARY_SERVICE_NAME" ]]
            ;;
        restart)
            [[ "${2:-}" == "$SERVICE_NAME" && "$#" -eq 2 ]]
            ;;
        stop)
            [[ "$#" -eq 2 ]] || return 1
            [[ "${2:-}" == "$SERVICE_NAME" ]] || [[ "${2:-}" == "$BOARD_CANARY_SERVICE_NAME" ]]
            ;;
        status)
            if [[ "${2:-}" == "$SERVICE_NAME" ]]; then
                [[ "$#" -eq 2 ]]
                return
            fi
            [[ "${2:-}" == "$BOARD_CANARY_SERVICE_NAME" ]] || return 1
            [[ "$#" -eq 2 ]] || [[ "$#" -eq 4 && "${3:-}" == "--no-pager" && "${4:-}" == "-l" ]]
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
    # Eric installs deploy/polkit/49-pgu-board-deploy.rules on the host to let
    # agent manage this unit without an interactive KDE prompt.
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

record_system_unit_hash() {
    local current_hash
    current_hash="$(system_unit_hash)" || return 0
    mkdir -p "$(dirname "$SYSTEM_UNIT_HASH_RECORD")"
    printf '%s\n' "$current_hash" >"$SYSTEM_UNIT_HASH_RECORD"
}

system_unit_needs_daemon_reload() {
    local needs_reload
    needs_reload="$(systemctl show "$SERVICE_NAME" -p NeedDaemonReload --value 2>/dev/null || true)"
    [[ "$needs_reload" == "yes" ]]
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

#: The mode a published release root is given, independent of the umask the
#: deploy happened to inherit.
#:
#: 0750 rather than 0755, and the difference is the ACL mask rather than the
#: group bits. The board runs as its own system account (User=boardsvc), which
#: is not in the release owner's group, so group bits grant it nothing on their
#: own. What grants it access is the named entry `user:<service>:r-x` that the
#: releases directory's default ACL puts on every new release root -- and a
#: named entry is capped by the access mask, which POSIX derives from the mode's
#: group bits. A root created under umask 077 is 0700, so the mask is `---` and
#: the inherited entry reads `#effective:---`: present, and worth nothing. Mode
#: 0750 sets the mask to r-x and the entry starts working.
#:
#: 0755 would also work today and is rejected as surface rather than access:
#: every ancestor is already reached through named entries, and the owner's home
#: is `drwx--x---` granting `--x` to the service account alone, so `other` bits
#: on a release root are unreachable by anybody they would nominally admit. They
#: would become real only if that home were ever loosened -- which is the moment
#: nobody would think to re-audit a release root (SYRD-89 R3).
readonly BOARD_RELEASE_ROOT_MODE="0750"

#: Whether a path grants r-x to one named account, evaluated the way the kernel
#: evaluates it rather than inferred from mode bits.
#:
#: Mode bits are not the answer. Group r-x proves nothing unless the account is
#: in the owning group, and on this deployment it is not: the board runs as its
#: own system account and the release is owned by the tenant. What grants access
#: is a named ACL entry, and a named entry is only worth what the mask allows.
#: Reading `drwxr-x---` and concluding the service can get in accepted a plain
#: 0750 root with no entry for it at all, which fails with EACCES the moment a
#: different uid tries (SYRD-89 R3 audit).
#:
#: Refuses anything that is not a real directory before it looks at permissions:
#: `stat` reports a symlink as `lrwxrwxrwx`, whose `other` bits are r-x, so a
#: symlink satisfied every mode test there was.
path_grants_rx_to() {
    local path="$1" account="$2"
    [[ -n "$account" ]] || return 1
    [[ -L "$path" ]] && return 1
    [[ -d "$path" ]] || return 1
    python3 - "$path" "$account" <<'PY'
import os
import pwd
import grp
import stat
import subprocess
import sys

path, account = sys.argv[1], sys.argv[2]
# By name, or by uid when the account is configured numerically or has no
# passwd entry reachable from here.
try:
    entry = pwd.getpwnam(account)
    uid, primary_gid = entry.pw_uid, entry.pw_gid
except KeyError:
    if not account.isdigit():
        raise SystemExit(1)
    uid = int(account)
    try:
        primary_gid = pwd.getpwuid(uid).pw_gid
    except KeyError:
        primary_gid = uid
info = os.lstat(path)
if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
    raise SystemExit(1)

NEED = 0b101  # r-x

def granted(bits):
    return (bits & NEED) == NEED

# The owner is decided by the mode's owner bits and nothing else.
if info.st_uid == uid:
    raise SystemExit(0 if granted((info.st_mode >> 6) & 7) else 1)

groups = {primary_gid}
try:
    groups |= {g.gr_gid for g in grp.getgrall() if account in g.gr_mem}
except Exception:
    pass

named_user = None
named_groups = []
mask = None
owning_group = (info.st_mode >> 3) & 7
other = info.st_mode & 7
try:
    acl = subprocess.run(
        ["getfacl", "-p", "--omit-header", "--absolute-names", path],
        capture_output=True, text=True, check=False,
    )
    lines = acl.stdout.splitlines() if acl.returncode == 0 else []
except OSError:
    lines = []

def bits(text):
    value = 0
    if "r" in text: value |= 0b100
    if "w" in text: value |= 0b010
    if "x" in text: value |= 0b001
    return value

for line in lines:
    line = line.split("#", 1)[0].strip()
    if not line or line.startswith("default:"):
        continue
    parts = line.split(":")
    if len(parts) != 3:
        continue
    kind, who, perms = parts
    if kind == "mask" and not who:
        mask = bits(perms)
    elif kind == "user" and who == account:
        named_user = bits(perms)
    elif kind == "group":
        if not who:
            named_groups.append((info.st_gid, bits(perms)))
        else:
            try:
                named_groups.append((grp.getgrnam(who).gr_gid, bits(perms)))
            except KeyError:
                continue

# POSIX.1e evaluation order: named user, then any matching group, then other.
if named_user is not None:
    effective = named_user if mask is None else named_user & mask
    raise SystemExit(0 if granted(effective) else 1)

matching = [b for gid, b in named_groups if gid in groups]
if not matching and info.st_gid in groups:
    matching = [owning_group]
if matching:
    effective = max(matching)
    if mask is not None:
        effective &= mask
    raise SystemExit(0 if granted(effective) else 1)

raise SystemExit(0 if granted(other) else 1)
PY
}

#: Whether this root admits the account the canary will run as.
release_root_is_serviceable() {
    path_grants_rx_to "$1" "$BOARD_CANARY_USER"
}

#: Repair a root the service cannot traverse, or refuse with something an
#: operator can act on. Only ever raises the mask, which is what activates a
#: named entry root already placed there; it never invents a grant. A root that
#: has no entry for the service account is not something a deploy should decide
#: to create one for, so that fails with the exact command instead.
#:
#: Called only after verify_release_sha has passed, so a root is never repaired
#: before its provenance is known: fixing the permissions on a tree that is not
#: the release it claims to be would make a wrong tree reachable (SYRD-89 R3).
ensure_release_root_serviceable() {
    local root="$1"
    # Shape before permissions, and before any early return: a symlink reports
    # `lrwxrwxrwx` and would otherwise be waved through by its `other` bits.
    [[ ! -L "$root" ]] || die "release root is a symlink, not a release directory: $root"
    [[ -d "$root" ]] || die "release root is not a directory: $root"
    release_root_is_serviceable "$root" && return 0
    chmod g+rx "$root" 2>/dev/null || true
    release_root_is_serviceable "$root" && return 0
    die "release root $root is mode $(stat -c '%a' "$root" 2>/dev/null || echo unknown) and $BOARD_CANARY_USER cannot traverse it. Raising the mask was not enough, so it carries no ACL entry for that account. Run: setfacl -m u:$BOARD_CANARY_USER:r-x $root"
}

deploy_export_release() {
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
        # Before publication, not after: between the mv and the canary the
        # release is already the tree the service will be pointed at, and the
        # mode it carries there is whatever umask the caller happened to have.
        # A deploy run under umask 077 published a 0700 root and the canary
        # failed with EACCES opening its own entry point (SYRD-89 R3). Only the
        # root: the modes inside were extracted from the archive and are the
        # release's own.
        chmod "$BOARD_RELEASE_ROOT_MODE" "$tmp_dir" || \
            die "could not normalize the release root mode: $tmp_dir"
        mv "$tmp_dir" "$release_dir"
    fi
    verify_release_sha "$release_dir" "$resolved_ref"
    # Reused roots too, and only once the sha above has proved which release
    # this is.
    ensure_release_root_serviceable "$release_dir"
    printf '%s\t%s\n' "$resolved_ref" "$release_dir"
}

activate_release() {
    local release_dir="$1"
    ln -sfn "$release_dir" "$BOARD_CURRENT_LINK"
}

verify_current_release_sha() {
    local expected_sha="$1"
    verify_release_sha "$BOARD_CURRENT_LINK" "$expected_sha"
}

current_release_dir() {
    if [[ -L "$BOARD_CURRENT_LINK" || -e "$BOARD_CURRENT_LINK" ]]; then
        readlink -f "$BOARD_CURRENT_LINK"
    fi
}

deploy_export() {
    local deployed_sha export_result release_dir
    if ! export_result="$(deploy_export_release)"; then
        return 1
    fi
    IFS=$'\t' read -r deployed_sha release_dir <<<"$export_result"
    [[ -n "$deployed_sha" && -n "$release_dir" ]] || die "deploy export did not return a release path"
    activate_release "$release_dir"
    verify_current_release_sha "$deployed_sha"
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
    if ! "$PYTHON_BIN" - "$BOARD_UNIX_SOCKET" "$SMOKE_TIMEOUT_SECONDS" "$BOARD_CURRENT_LINK" <<'SMOKEPY'
import json
import os
import socket
import sys
import time
from pathlib import Path

socket_path = sys.argv[1]
deadline = time.monotonic() + float(sys.argv[2])
sys.path.insert(0, str(Path(sys.argv[3]) / "scripts"))


def send(request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        sock.connect(socket_path)
        sock.sendall(request)
        return sock.recv(4096)


def status_of(response):
    return response.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")


# Reachability: a read needs no caller role, so this proves the socket is
# serving without depending on the authorization path.
read_request = (
    b"GET /api/board HTTP/1.1\r\n"
    b"Host: localhost\r\n"
    b"Connection: close\r\n\r\n"
)
last_error = "not attempted"
serving = False
while time.monotonic() < deadline:
    try:
        line = status_of(send(read_request))
        if " 200 " in line:
            serving = True
            break
        last_error = line
    except OSError as exc:
        last_error = str(exc)
    time.sleep(0.25)
if not serving:
    print("ticket-board Unix socket verification failed for %s: %s" % (socket_path, last_error), file=sys.stderr)
    sys.exit(1)

# The deploy process is outside every registered launcher pane. Its uid and a
# claimed role must never be sufficient authority.
body = json.dumps({"role": "director"}).encode("utf-8")
claim = (
    b"POST /api/register-caller HTTP/1.1\r\n"
    b"Host: localhost\r\n"
    b"Content-Type: application/json\r\n"
    + ("Content-Length: %d\r\n" % len(body)).encode("ascii")
    + b"Connection: close\r\n\r\n"
    + body
)
try:
    line = status_of(send(claim))
except OSError as exc:
    print("ticket-board socket role-binding check failed: %s" % exc, file=sys.stderr)
    sys.exit(1)
if " 200 " in line:
    print(
        "ticket-board socket process-binding check FAILED: an unregistered process was "
        "granted director (%s)" % line,
        file=sys.stderr,
    )
    sys.exit(1)
print("ticket-board process binding refused an unregistered process as expected (%s)" % line)
SMOKEPY
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

render_unit() {
    cat <<EOF
[Unit]
Description=$PROJECT_SLUG Ticket Board
After=network.target

[Service]
Type=simple
WorkingDirectory=$BOARD_CURRENT_LINK
RuntimeDirectory=$PROJECT_SLUG-ticket-board
ExecStartPre=/bin/mkdir -p $FRAME_ROOT
ExecStartPre=/bin/chmod 1777 $FRAME_ROOT
ExecStart=$PYTHON_BIN $BOARD_SCRIPT --host $BOARD_HOST --port $BOARD_PORT --unix-socket $BOARD_UNIX_SOCKET --frames $FRAME_ROOT
Restart=on-failure
RestartSec=2
EnvironmentFile=-$RUNTIME_HOME/.config/$PROJECT_SLUG/ticket-board.env
Environment=PYTHONUNBUFFERED=1
Environment=HOME=$RUNTIME_HOME
Environment=TICKET_BOARD_DIRECTORCTL=$BOARD_CURRENT_LINK/scripts/directorctl
Environment=TICKET_BOARD_PROJECT=$PROJECT_SLUG
Environment=TICKET_BOARD_SOCKET=$BOARD_UNIX_SOCKET
Environment=TICKET_BOARD_COMMIT_GIT_DIR=$COMMIT_GIT_DIR
Environment=TICKET_BOARD_DATABASE_URL=$BOARD_DATABASE_URL
StandardOutput=append:$LOG_PATH
StandardError=append:$LOG_PATH

[Install]
WantedBy=default.target
EOF
}

render_system_unit_for_release() {
    local release_dir="$1"
    local production_unit
    if production_unit="$(system_unit_candidate_path_for_release "$release_dir")"; then
        cat "$production_unit"
        return
    fi
    render_unit
}

system_unit_candidate_path_for_release() {
    local release_dir="$1"
    local production_unit
    if [[ -n "$PROVISIONED_SYSTEM_UNIT" ]]; then
        [[ "$PROVISIONED_SYSTEM_UNIT" == /* ]] || die "TICKET_BOARD_PROVISIONED_SYSTEM_UNIT must be absolute: $PROVISIONED_SYSTEM_UNIT"
        printf '%s\n' "$PROVISIONED_SYSTEM_UNIT"
        [[ -f "$PROVISIONED_SYSTEM_UNIT" ]] || return 1
        return 0
    fi
    production_unit="$release_dir/deploy/systemd/$SERVICE_NAME.boardsvc"
    if [[ ! -f "$production_unit" && "$PROJECT_SLUG" == "pgu" ]]; then
        production_unit="$release_dir/deploy/systemd/pgu-ticket-board.service.boardsvc"
    fi
    printf '%s\n' "$production_unit"
    [[ -f "$production_unit" ]] || return 1
}

render_system_unit() {
    render_system_unit_for_release "$BOARD_CURRENT_LINK"
}

write_unit() {
    mkdir -p "$UNIT_DIR"
    render_unit >"$UNIT_PATH"
}

changed_unit_directives() {
    awk '
        /^[+-][^-+@]/ {
            line = substr($0, 2)
            sub(/^[[:space:]]+/, "", line)
            sub(/[[:space:]]+$/, "", line)
            if (line == "") next
            if (line ~ /^\[/) {
                print line
                next
            }
            split(line, parts, "=")
            print parts[1]
        }
    ' "$1" | sort -u
}

join_directives() {
    paste -sd ',' - | sed 's/,/, /g'
}

classify_system_unit_drift() {
    local diff_file="$1"
    local directives sensitive
    directives="$(changed_unit_directives "$diff_file")"
    if [[ -z "$directives" ]]; then
        printf 'unclassified unit drift\n'
        return
    fi
    if ! grep -Ev '^(Environment)$' <<<"$directives" >/dev/null; then
        printf 'Environment-only drift\n'
        return
    fi
    sensitive="$(grep -E '^(User|Group|SupplementaryGroups|WorkingDirectory|ExecStart|ExecStartPre|RuntimeDirectory|StateDirectory|ReadWritePaths|Protect[A-Za-z]*|Private[A-Za-z]*|NoNewPrivileges|CapabilityBoundingSet|Restrict[A-Za-z]*|SystemCall[A-Za-z]*)$' <<<"$directives" | join_directives || true)"
    if [[ -n "$sensitive" ]]; then
        printf 'sensitive unit drift: %s\n' "$sensitive"
        return
    fi
    printf 'unit drift in directives: %s\n' "$(join_directives <<<"$directives")"
}

report_system_unit_drift() {
    local installed_unit="$1"
    local subject_label="$2"
    local diff_file="$3"
    log "$subject_label"
    log "installed system unit path: $installed_unit"
    log "system unit drift classification: $(classify_system_unit_drift "$diff_file")"
    log "system unit unified diff:"
    sed 's/^/[ticket-board-service]   /' "$diff_file" >&2
}

assert_system_unit_reload_not_required_for_release() {
    local release_dir="$1"
    local rendered_unit installed_unit candidate_path subject_label diff_file
    installed_unit="$(system_unit_file_path)" || die "system service scope resolved to system, but no installed $SERVICE_NAME unit file was found"
    rendered_unit="$(mktemp "${TMPDIR:-/tmp}/$PROJECT_SLUG-ticket-board-unit.XXXXXX")"
    diff_file="$(mktemp "${TMPDIR:-/tmp}/$PROJECT_SLUG-ticket-board-unit-diff.XXXXXX")"
    render_system_unit_for_release "$release_dir" >"$rendered_unit"
    if candidate_path="$(system_unit_candidate_path_for_release "$release_dir")"; then
        subject_label="candidate system unit path: $candidate_path"
    else
        subject_label="release contains no production system unit at expected path: $candidate_path; generic render below is diagnostic only and must not be installed"
    fi
    if ! cmp -s "$rendered_unit" "$installed_unit"; then
        diff -u --label "installed:$installed_unit" --label "$subject_label" "$installed_unit" "$rendered_unit" >"$diff_file" || true
        report_system_unit_drift "$installed_unit" "$subject_label" "$diff_file"
        rm -f "$rendered_unit" "$diff_file"
        if [[ -f "$candidate_path" ]]; then
            die "candidate system unit for $release_dir differs from installed $installed_unit; daemon-reload is required but is intentionally outside the board deploy polkit grant. Ask an operator to install the candidate unit from $candidate_path and run systemctl daemon-reload, then rerun deploy-restart."
        fi
        die "release $release_dir contains no production system unit at expected path $candidate_path; the generic render above is diagnostic only and must not be installed. Add the production unit to the release or ask an operator to install an operator-reviewed production unit and run systemctl daemon-reload, then rerun deploy-restart."
    fi
    rm -f "$rendered_unit" "$diff_file"
    if system_unit_needs_daemon_reload; then
        die "systemd reports NeedDaemonReload=yes for $SERVICE_NAME; daemon-reload is required but is intentionally outside the board deploy polkit grant. Ask an operator to run systemctl daemon-reload, then rerun deploy-restart."
    fi
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
    local deployed_sha export_result release_dir previous_release scope
    if ! export_result="$(deploy_export_release)"; then
        return 1
    fi
    IFS=$'\t' read -r deployed_sha release_dir <<<"$export_result"
    [[ -n "$deployed_sha" && -n "$release_dir" ]] || die "deploy export did not return a release path"
    previous_release="$(current_release_dir || true)"
    scope="$(resolved_service_scope)"
    if [[ "$scope" == "system" ]]; then
        assert_system_unit_reload_not_required_for_release "$release_dir"
    fi
    apply_database_migrations_for_release "$release_dir"
    run_release_canary "$release_dir" "$scope"
    activate_release "$release_dir"
    verify_current_release_sha "$deployed_sha"
    restart_live_service "$scope"
    if ! smoke_check_http; then
        rollback_live_service "$scope" "$previous_release"
        exit 1
    fi
    if ! verify_live_build_id "$deployed_sha"; then
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
        quiesce_user_shadow_unit
        assert_system_unit_reload_not_required_for_release "$BOARD_CURRENT_LINK"
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
        start|restart)
            if [[ "$(resolved_service_scope)" == "system" ]]; then
                quiesce_user_shadow_unit
                assert_system_unit_reload_not_required_for_release "$BOARD_CURRENT_LINK"
                record_system_unit_hash
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
        *)
            die "unknown command: $1"
            ;;
    esac
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
