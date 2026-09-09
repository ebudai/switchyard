#!/usr/bin/env bash
set -Eeuo pipefail

# SYRD-85 operator artifact. Audit these bytes, install this file as
# /etc/switchyard/provision/syrd/SYRD-85-activate-audited-board.sh with
# root:root ownership and mode 0555, record its SHA-256, and run it once as
# root. This script deliberately changes only the syrd board database/release
# and restarts the existing board service. It does not install a unit or touch
# the shared Switchyard release, launcher, sessions, accounts, or upgrade
# journal.

readonly PROJECT="syrd"
readonly PROJECT_OWNER="switchyard-agent"
readonly LEGACY_APP_ACCOUNT="syrd-app"
readonly SERVICE="syrd-ticket-board.service"
readonly EXPECTED_ARTIFACT_PATH="/etc/switchyard/provision/syrd/SYRD-85-activate-audited-board.sh"
readonly EXPECTED_PREVIOUS="b769ceab80bb42500f8839dbde46e92e15c29a23"
readonly EXPECTED_TARGET="0eace2e8cb40c0adf04a3cfe4c61cf7e8e593b59"
readonly EXPECTED_TREE="a619d7ea42e063d33ae4a14abf7c6d1dd0209db0"
readonly EXPECTED_UNIT_HASH="64165c5da5a12f1ff3ba3fd33eef35c360b8057aa90d4517820ba2e5dbee3e57"
readonly EXPECTED_CANARY_UNIT_HASH="606af13075d8464a09b5e39b095909883fe333158490d8414ababb2273e31f3b"
readonly PUBLIC_REMOTE="https://github.com/ebudai/switchyard.git"
readonly PUBLIC_REF="refs/heads/main"
readonly CACHE="/home/switchyard-agent/syrd-source-cache.git"
readonly CACHE_REF="refs/remotes/origin/main"
readonly BOARD_ROOT="/home/switchyard-agent/syrd-ticketboard-live"
readonly BOARD_URL="http://127.0.0.1:23326"
readonly BOARD_SOCKET="/run/syrd-ticket-board/ticket-board.sock"
readonly UNIT="/etc/systemd/system/syrd-ticket-board.service"
readonly CANARY_SERVICE="syrd-ticket-board-canary.service"
readonly CANARY_UNIT="/etc/systemd/system/syrd-ticket-board-canary.service"
readonly ADMIN_DATABASE_URL="postgresql:///syrd_ticket_board?host=/var/run/postgresql&user=postgres"
readonly SERVICE_DATABASE_URL="postgresql:///syrd_ticket_board?host=/var/run/postgresql&user=ticket_board_service"

work_root=""
source_dir=""
mutation_started=0
deployment_complete=0

die() {
    printf 'SYRD-85 activation: ERROR: %s\n' "$*" >&2
    exit 1
}

sha256_file() {
    sha256sum "$1" | awk '{print $1}'
}

board_build_id() {
    curl -fsS "$BOARD_URL/api/board" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("build_id", ""))'
}

current_release() {
    readlink -f "$BOARD_ROOT/current"
}

verify_artifact_trust() {
    [[ "$(id -u)" == "0" ]] || die "run as root"
    local invoked resolved owner group mode
    invoked="${BASH_SOURCE[0]}"
    [[ "$invoked" == /* ]] || die "invoke the installed artifact by its absolute path"
    [[ ! -L "$invoked" ]] || die "operator artifact must not be a symlink"
    resolved="$(readlink -f "$invoked")"
    [[ "$resolved" == "$EXPECTED_ARTIFACT_PATH" ]] || \
        die "artifact path is $resolved, expected $EXPECTED_ARTIFACT_PATH"
    read -r owner group mode < <(stat -c '%U %G %a' "$resolved")
    [[ "$owner $group $mode" == "root root 555" ]] || \
        die "artifact is $owner:$group mode $mode, expected root:root mode 555"
    python3 - "$resolved" <<'PY'
import os
import stat
import sys
from pathlib import Path

path = Path(sys.argv[1])
for component in reversed((path.parent, *path.parent.parents)):
    info = os.lstat(component)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise SystemExit(f"untrusted artifact path component: {component}")
    if info.st_uid != 0 or info.st_mode & 0o022:
        raise SystemExit(f"artifact path component is not root-owned and non-writable: {component}")
PY
    printf 'SYRD-85 activation: artifact_sha256=%s\n' "$(sha256_file "$resolved")"
}

require_commands() {
    local command_name
    for command_name in awk curl git grep id mktemp psql python3 readlink rm sha256sum stat sudo systemctl; do
        command -v "$command_name" >/dev/null 2>&1 || die "missing command: $command_name"
    done
}

verify_exact_source() {
    local public_commit cache_commit cache_tree
    public_commit="$(git ls-remote "$PUBLIC_REMOTE" "$PUBLIC_REF" | awk 'NR == 1 {print $1}')"
    [[ "$public_commit" == "$EXPECTED_TARGET" ]] || \
        die "public main is $public_commit, expected $EXPECTED_TARGET"
    cache_commit="$(git -c "safe.directory=$CACHE" --git-dir="$CACHE" rev-parse "$CACHE_REF^{commit}")"
    [[ "$cache_commit" == "$EXPECTED_TARGET" ]] || \
        die "trusted cache main is $cache_commit, expected $EXPECTED_TARGET"
    cache_tree="$(git -c "safe.directory=$CACHE" --git-dir="$CACHE" rev-parse "$CACHE_REF^{tree}")"
    [[ "$cache_tree" == "$EXPECTED_TREE" ]] || \
        die "trusted cache tree is $cache_tree, expected $EXPECTED_TREE"
    git -c "safe.directory=$CACHE" --git-dir="$CACHE" cat-file -e "$EXPECTED_PREVIOUS^{commit}" || \
        die "trusted cache lacks previous release $EXPECTED_PREVIOUS"
}

verify_unit_and_legacy_authority() {
    local fragment main_pid owner group mode unit_hash
    [[ -f "$UNIT" && ! -L "$UNIT" ]] || die "installed unit is missing or a symlink: $UNIT"
    unit_hash="$(sha256_file "$UNIT")"
    [[ "$unit_hash" == "$EXPECTED_UNIT_HASH" ]] || \
        die "installed unit hash is $unit_hash, expected $EXPECTED_UNIT_HASH"
    read -r owner group mode < <(stat -c '%U %G %a' "$UNIT")
    [[ "$owner $group $mode" == "root root 644" ]] || \
        die "installed unit is $owner:$group mode $mode, expected root:root mode 644"
    fragment="$(systemctl show "$SERVICE" -p FragmentPath --value)"
    [[ "$(readlink -f "$fragment")" == "$UNIT" ]] || \
        die "loaded unit fragment is $fragment, expected $UNIT"
    [[ "$(systemctl show "$SERVICE" -p NeedDaemonReload --value)" == "no" ]] || \
        die "systemd reports NeedDaemonReload=yes"
    systemctl is-active --quiet "$SERVICE" || die "$SERVICE is not active"
    if grep -q 'TICKET_BOARD_PROCESS_AUTHORITY' "$UNIT"; then
        die "installed unit contains TICKET_BOARD_PROCESS_AUTHORITY; legacy authority was required"
    fi
    main_pid="$(systemctl show "$SERVICE" -p MainPID --value)"
    [[ "$main_pid" =~ ^[1-9][0-9]*$ && -r "/proc/$main_pid/environ" ]] || \
        die "cannot inspect live board process environment"
    python3 - "/proc/$main_pid/environ" <<'PY'
import sys

entries = open(sys.argv[1], "rb").read().split(b"\0")
if any(item.startswith(b"TICKET_BOARD_PROCESS_AUTHORITY=") for item in entries):
    raise SystemExit("live board process unexpectedly defines TICKET_BOARD_PROCESS_AUTHORITY")
PY
}

verify_canary_unit() {
    local fragment owner group mode unit_hash
    [[ -f "$CANARY_UNIT" && ! -L "$CANARY_UNIT" ]] || \
        die "installed canary unit is missing or a symlink: $CANARY_UNIT"
    unit_hash="$(sha256_file "$CANARY_UNIT")"
    [[ "$unit_hash" == "$EXPECTED_CANARY_UNIT_HASH" ]] || \
        die "installed canary unit hash is $unit_hash, expected $EXPECTED_CANARY_UNIT_HASH"
    read -r owner group mode < <(stat -c '%U %G %a' "$CANARY_UNIT")
    [[ "$owner $group $mode" == "root root 644" ]] || \
        die "installed canary unit is $owner:$group mode $mode, expected root:root mode 644"
    fragment="$(systemctl show "$CANARY_SERVICE" -p FragmentPath --value)"
    [[ "$(readlink -f "$fragment")" == "$CANARY_UNIT" ]] || \
        die "loaded canary unit fragment is $fragment, expected $CANARY_UNIT"
    [[ "$(systemctl show "$CANARY_SERVICE" -p NeedDaemonReload --value)" == "no" ]] || \
        die "systemd reports that the canary unit needs daemon-reload"
}

verify_database_admin() {
    [[ "$(sudo -u postgres psql -X -Atq "$ADMIN_DATABASE_URL" -c 'SELECT 1')" == "1" ]] || \
        die "PostgreSQL administration preflight failed"
}

verify_browser_dependencies() {
    id "$LEGACY_APP_ACCOUNT" >/dev/null 2>&1 || \
        die "missing legacy App account for browser verification"
    sudo -u "$LEGACY_APP_ACCOUNT" -H \
        env PLAYWRIGHT_BROWSERS_PATH="/home/$LEGACY_APP_ACCOUNT/.cache/ms-playwright" \
        /usr/bin/python3 - <<'PY'
from pathlib import Path
from playwright.sync_api import sync_playwright

with sync_playwright() as playwright:
    executable = Path(playwright.chromium.executable_path)
    if not executable.is_file() or not executable.stat().st_mode & 0o111:
        raise SystemExit(f"Chromium executable is unavailable: {executable}")
PY
}

verify_release_pointer() {
    local expected="$1"
    local resolved marker
    resolved="$(current_release)"
    [[ "$resolved" == "$BOARD_ROOT/releases/$expected" ]] || \
        die "current release is $resolved, expected $BOARD_ROOT/releases/$expected"
    marker="$resolved/.pgu-deploy-sha"
    [[ -f "$marker" ]] || die "release $resolved lacks its deploy marker"
    [[ "$(<"$marker")" == "$expected" ]] || die "release marker does not match $expected"
}

verify_http_and_build() {
    local expected="$1"
    local actual root_status board_status
    root_status="$(curl -sS -o /dev/null -w '%{http_code}' "$BOARD_URL/")"
    board_status="$(curl -sS -o /dev/null -w '%{http_code}' "$BOARD_URL/api/board")"
    [[ "$root_status" == "200" && "$board_status" == "200" ]] || \
        die "HTTP health failed: root=$root_status board=$board_status"
    actual="$(board_build_id)"
    [[ "$actual" == "$expected" ]] || die "live build is $actual, expected $expected"
}

verify_socket_contract() {
    local dir_owner dir_group dir_mode socket_owner socket_group socket_mode
    read -r dir_owner dir_group dir_mode < <(stat -c '%U %G %a' "$(dirname "$BOARD_SOCKET")")
    read -r socket_owner socket_group socket_mode < <(stat -c '%U %G %a' "$BOARD_SOCKET")
    [[ "$dir_owner $dir_group $dir_mode" == "boardsvc syrd-roles 750" ]] || \
        die "runtime directory is $dir_owner:$dir_group $dir_mode, expected boardsvc:syrd-roles 750"
    [[ "$socket_owner $socket_group $socket_mode" == "boardsvc syrd-roles 660" ]] || \
        die "socket is $socket_owner:$socket_group $socket_mode, expected boardsvc:syrd-roles 660"
}

verify_write_token_boundary() {
    local expected="$1"
    local client_config status
    client_config="$(mktemp "$work_root/client-config.XXXXXX")"
    curl -fsS "$BOARD_URL/api/client-config" >"$client_config"
    python3 - "$client_config" "$expected" <<'PY'
import json
import sys

payload = json.load(open(sys.argv[1], encoding="utf-8"))
if payload.get("build_id") != sys.argv[2]:
    raise SystemExit("client-config build_id mismatch")
if not isinstance(payload.get("write_token"), str) or not payload["write_token"]:
    raise SystemExit("client-config has no write token")
PY
    status="$(curl -sS -o /dev/null -w '%{http_code}' \
        -H 'Content-Type: application/json' \
        --data '{"role":"director"}' \
        "$BOARD_URL/api/register-caller")"
    [[ "$status" == "403" ]] || \
        die "HTTP write without the token returned $status, expected 403"
}

verify_legacy_runtime_api() {
    curl -fsS "$BOARD_URL/api/runtime-assignments" \
        | python3 -c 'import json,sys; p=json.load(sys.stdin); assert p.get("authority_mode") == "legacy_uid", p'
}

verify_director_reassign_capability() {
    curl -fsS "$BOARD_URL/api/workflow" | python3 -c '
import json, sys
p = json.load(sys.stdin)
roles = {role.get("name"): role for role in p.get("roles", [])}
director = roles.get("director", {})
assert director.get("active") is True, director
assert "reassign" in director.get("capabilities", []), director
'
}

verify_active_work_api() {
    curl -fsS "$BOARD_URL/api/board" | python3 -c '
import json, sys
p = json.load(sys.stdin)
stages = {stage.get("name"): stage for stage in p.get("workflow", {}).get("stages", [])}
highlighted = []
for ticket in p.get("tickets", []):
    assert isinstance(ticket.get("active_work_highlight"), bool), ticket.get("id")
    assert isinstance(ticket.get("active_work_owner_role"), str), ticket.get("id")
    assert isinstance(ticket.get("active_work_notified_at"), str), ticket.get("id")
    if not ticket["active_work_highlight"]:
        continue
    assert ticket["active_work_owner_role"], ticket
    assert not any(not blocker.get("resolved") for blocker in ticket.get("blockers", [])), ticket
    assert not ticket.get("awaiting_role"), ticket
    assert not ticket.get("manually_controlled"), ticket
    assert not ticket.get("queued_for_assignee"), ticket
    assert not ticket.get("queued_behind_ticket"), ticket
    stage = stages.get(ticket.get("state"), {})
    assert stage and not stage.get("terminal") and stage.get("kind") != "draft", ticket
    highlighted.append(ticket)
owners = [ticket["active_work_owner_role"] for ticket in highlighted]
assert highlighted, "no active work is highlighted on a board with current work"
assert len(owners) == len(set(owners)), owners
unsent = sum(not ticket["active_work_notified_at"] for ticket in highlighted)
print(f"SYRD-85 activation: active-work highlights={len(highlighted)} unsent={unsent}")
'
}

verify_flat_cards_in_browser() {
    sudo -u "$LEGACY_APP_ACCOUNT" -H \
        env PLAYWRIGHT_BROWSERS_PATH="/home/$LEGACY_APP_ACCOUNT/.cache/ms-playwright" \
        /usr/bin/python3 - "$BOARD_URL" <<'PY'
import sys
import time

from playwright.sync_api import sync_playwright

url = sys.argv[1]
last_error = "not attempted"
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    try:
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        for _ in range(4):
            try:
                page.goto(url, wait_until="domcontentloaded")
                page.locator(".column").first.wait_for(timeout=5000)
                for selector in ("#showDeferredInput", "#showDoneInput", "#showCancelledInput"):
                    toggle = page.locator(selector)
                    if toggle.count() and not toggle.is_checked():
                        toggle.check()
                page.wait_for_timeout(100)
                payload = page.evaluate("async () => (await fetch('/api/board')).json()")
                rendered = page.evaluate("""
() => Array.from(document.querySelectorAll('.column')).flatMap((column) => {
  const label = column.querySelector('.column-title').textContent.trim();
  const body = column.querySelector('.column-body');
  return Array.from(body.children)
    .filter((child) => child.classList.contains('card'))
    .map((card) => ({id: card.querySelector('.card-id').textContent.trim(), label,
                     active: card.classList.contains('card-active-work')}));
})
""")
                labels = {column["key"]: column["label"] for column in payload["columns"]}
                placements = {}
                for card in rendered:
                    placements.setdefault(card["id"], []).append(card)
                for ticket in payload["tickets"]:
                    expected = labels[ticket["state"]]
                    actual = placements.get(ticket["id"], [])
                    assert len(actual) == 1 and actual[0]["label"] == expected, (ticket, actual)
                    assert actual[0]["active"] == bool(ticket["active_work_highlight"]), (ticket, actual)
                assert len(rendered) == len(payload["tickets"]), (len(rendered), len(payload["tickets"]))
                assert page.locator(".column .child-ticket-item").count() == 0
                tickets = {ticket["id"]: ticket for ticket in payload["tickets"]}
                linked = next(
                    ticket for ticket in payload["tickets"]
                    if ticket.get("parent_id") and ticket["parent_id"] in tickets
                )
                page.evaluate("""(ticketId) => {
  const id = Array.from(document.querySelectorAll('.card-id'))
    .find((node) => node.textContent.trim() === ticketId);
  id.closest('.card').click();
}""", linked["id"])
                page.locator(".detail-modal").wait_for(timeout=5000)
                assert page.get_by_role("button", name=linked["parent_id"], exact=True).count() >= 1
                print(f"SYRD-85 activation: flat-card browser check passed for {len(rendered)} tickets")
                break
            except Exception as exc:  # Board activity can race one DOM/API snapshot; retry cold.
                last_error = repr(exc)
                time.sleep(0.2)
                page.reload(wait_until="domcontentloaded")
        else:
            raise SystemExit(f"flat-card browser verification failed: {last_error}")
    finally:
        browser.close()
PY
}

verify_target_health() {
    verify_release_pointer "$EXPECTED_TARGET"
    verify_http_and_build "$EXPECTED_TARGET"
    verify_unit_and_legacy_authority
    verify_canary_unit
    verify_socket_contract
    verify_write_token_boundary "$EXPECTED_TARGET"
    verify_legacy_runtime_api
    verify_director_reassign_capability
    verify_active_work_api
    verify_flat_cards_in_browser
}

verify_previous_health() {
    verify_release_pointer "$EXPECTED_PREVIOUS"
    verify_http_and_build "$EXPECTED_PREVIOUS"
    verify_unit_and_legacy_authority
    verify_canary_unit
    verify_socket_contract
    verify_write_token_boundary "$EXPECTED_PREVIOUS"
}

prepare_source_checkout() {
    work_root="$(mktemp -d /tmp/syrd-85-activation.XXXXXX)"
    source_dir="$work_root/source"
    git -c "safe.directory=$CACHE" clone --no-checkout --quiet "$CACHE" "$source_dir"
    git -C "$source_dir" checkout --detach --quiet "$EXPECTED_TARGET"
    [[ "$(git -C "$source_dir" rev-parse HEAD)" == "$EXPECTED_TARGET" ]] || \
        die "temporary checkout commit mismatch"
    [[ "$(git -C "$source_dir" rev-parse HEAD^{tree})" == "$EXPECTED_TREE" ]] || \
        die "temporary checkout tree mismatch"
    bash -n "$source_dir/scripts/ticket-board-service.sh"
    [[ -x "$source_dir/scripts/ticket-board-migrate" ]] || die "candidate migration runner is not executable"
    [[ -f "$source_dir/scripts/ticket_board/migrations/pgu925_syrd69_project_process_authority.sql" ]] || \
        die "candidate lacks the audited process-authority compatibility migration"
    [[ -f "$source_dir/scripts/ticket_board/migrations/pgu926_syrd77_director_reassign.sql" ]] || \
        die "candidate lacks the audited Director reassign migration"
}

standard_deploy() {
    local deploy_ref="$1"
    local skip_migrations="$2"
    env -u TICKET_BOARD_PROCESS_AUTHORITY \
        HOME="/home/$PROJECT_OWNER" \
        TICKET_BOARD_OWNER_HOME="/home/$PROJECT_OWNER" \
        TICKET_BOARD_PROJECT="$PROJECT" \
        TICKET_BOARD_COMMIT_GIT_DIR="$CACHE" \
        TICKET_BOARD_PROVISIONED_SYSTEM_UNIT="$UNIT" \
        TICKET_BOARD_SYSTEM_UNIT_PATH="$UNIT" \
        TICKET_BOARD_SERVICE_SCOPE=system \
        TICKET_BOARD_PYTHON=/usr/bin/python3 \
        TICKET_BOARD_DATABASE_URL="$SERVICE_DATABASE_URL" \
        TICKET_BOARD_ADMIN_DATABASE_URL="$ADMIN_DATABASE_URL" \
        SOURCE_REPO="$source_dir" \
        BOARD_ROOT="$BOARD_ROOT" \
        BOARD_HOST=127.0.0.1 \
        BOARD_PORT=23326 \
        BOARD_UNIX_SOCKET="$BOARD_SOCKET" \
        DEPLOY_REF="$deploy_ref" \
        TICKET_BOARD_SKIP_MIGRATIONS="$skip_migrations" \
        "$source_dir/scripts/ticket-board-service.sh" deploy-restart
}

rollback_previous_release() {
    printf '%s\n' \
        'SYRD-85 activation: activation failed; audited SQL migrations are additive and remain applied.' >&2
    printf 'SYRD-85 activation: restoring previous release %s through the standard health gate\n' \
        "$EXPECTED_PREVIOUS" >&2
    if ! standard_deploy "$EXPECTED_PREVIOUS" 1; then
        return 1
    fi
    if ! verify_previous_health; then
        return 1
    fi
    printf 'SYRD-85 activation: rollback healthy; build=%s; additive migrations remain applied\n' \
        "$EXPECTED_PREVIOUS" >&2
}

on_exit() {
    local status="$1"
    trap - EXIT
    if (( status != 0 && mutation_started == 1 && deployment_complete == 0 )); then
        local rollback_status=0
        (rollback_previous_release) || rollback_status=$?
        if (( rollback_status != 0 )); then
            printf 'SYRD-85 activation: CRITICAL: rollback health proof failed with status %s\n' \
                "$rollback_status" >&2
            status=1
        fi
    fi
    if [[ -n "$work_root" && -d "$work_root" ]]; then
        rm -rf -- "$work_root"
    fi
    exit "$status"
}

main() {
    (($# == 0)) || die "this artifact accepts no arguments"
    verify_artifact_trust
    require_commands
    unset GIT_DIR GIT_WORK_TREE
    verify_exact_source
    verify_unit_and_legacy_authority
    verify_canary_unit
    verify_database_admin
    verify_browser_dependencies

    local release build
    release="$(current_release)"
    build="$(board_build_id)"
    if [[ "$release" == "$BOARD_ROOT/releases/$EXPECTED_TARGET" && "$build" == "$EXPECTED_TARGET" ]]; then
        work_root="$(mktemp -d /tmp/syrd-85-verify.XXXXXX)"
        verify_target_health
        deployment_complete=1
        printf 'SYRD-85 activation: already active and healthy at %s\n' "$EXPECTED_TARGET"
        return
    fi
    [[ "$release" == "$BOARD_ROOT/releases/$EXPECTED_PREVIOUS" ]] || \
        die "current release is $release, expected previous or target release"
    [[ "$build" == "$EXPECTED_PREVIOUS" ]] || \
        die "live build is $build, expected previous or target build"
    verify_release_pointer "$EXPECTED_PREVIOUS"
    verify_http_and_build "$EXPECTED_PREVIOUS"
    verify_socket_contract

    prepare_source_checkout
    mutation_started=1
    printf 'SYRD-85 activation: applying additive migrations, canarying, and activating %s\n' \
        "$EXPECTED_TARGET"
    standard_deploy "$EXPECTED_TARGET" 0
    verify_target_health
    deployment_complete=1
    printf 'SYRD-85 activation: complete; build=%s authority=legacy_uid unit_hash=%s\n' \
        "$EXPECTED_TARGET" "$EXPECTED_UNIT_HASH"
}

trap 'on_exit $?' EXIT
main "$@"
