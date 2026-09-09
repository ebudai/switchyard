#!/usr/bin/env bash
set -Eeuo pipefail

# SYRD-87 operator artifact. Audit these bytes, install this file as
# /etc/switchyard/provision/syrd/SYRD-87-recover-syrd-runtime.sh with root:root
# ownership and mode 0555, record its SHA-256, and run it once as root.
#
# It moves the syrd tenant off the abandoned per-role-account cutover and onto
# the integrated SYRD-69 one-project-account runtime, and installs the SYRD-66
# launcher, Konsole presentation, role-control and notification artifacts. It
# does that through the supported commands only -- install-switchyard, then
# `switchyard upgrade`, both pinned to one exact source checkout -- because the
# five previous rollouts failed in the seams around those commands rather than
# inside them. It changes nothing that belongs to pgu, mefp or otto, creates and
# deletes no accounts, and leaves the director-owned phase to the director.
#
# Every role session, including the director's, is stopped at a resumable
# checkpoint before the cutover and resumed from repatriated state afterwards.
# The repatriation in the release refuses to run while any role pane is live, so
# stopping them is not optional and is done here rather than left to the
# operator to remember.

readonly PROJECT="syrd"
readonly PROJECT_OWNER="switchyard-agent"
readonly EXPECTED_ARTIFACT_PATH="/etc/switchyard/provision/syrd/SYRD-87-recover-syrd-runtime.sh"
readonly EXPECTED_TARGET="9a4d0a648c839fa5f8945342edefcd49ab553407"
readonly EXPECTED_TREE="e5a685c51e4da4dd816d651a69d6467c9ba823a4"
readonly EXPECTED_SHARED_PREVIOUS="7a0d44d72fddef60a6c0cb3940a1f4853b30fe44"
readonly EXPECTED_BOARD_PREVIOUS="0eace2e8cb40c0adf04a3cfe4c61cf7e8e593b59"
readonly PUBLIC_REMOTE="https://github.com/ebudai/switchyard.git"
readonly PUBLIC_REF="refs/heads/main"
readonly CACHE="/home/switchyard-agent/syrd-source-cache.git"
readonly CACHE_REF="refs/remotes/origin/main"
readonly SHARED_ROOT="/opt/switchyard"
readonly SWITCHYARD_BIN="/usr/local/bin/switchyard"
readonly BOARD_ROOT="/home/switchyard-agent/syrd-ticketboard-live"
readonly BOARD_URL="http://127.0.0.1:23326"
readonly BOARD_SOCKET="/run/syrd-ticket-board/ticket-board.sock"
readonly SERVICE="syrd-ticket-board.service"
readonly TENANT_PROVISION="/home/switchyard-agent/Projects/switchyard/.switchyard/provision"
readonly TENANT_CONFIG="$TENANT_PROVISION/syrd.json"
readonly UPGRADE_JOURNAL="$TENANT_PROVISION/syrd-upgrade.json"
readonly DESKTOP_POLICY="$TENANT_PROVISION/desktop-policy.json"
readonly TENANT_TOOLING="/usr/local/lib/switchyard/syrd"
readonly TENANT_RELEASE_MARKER="$TENANT_TOOLING/.switchyard-release.json"
readonly ROLES=("director" "main" "app" "ops" "audit" "inspector")
# Every other tenant on this host. Their state is fingerprinted before the
# cutover and compared after it, because "syrd only" is a claim this artifact
# has to be able to prove rather than assert.
readonly FOREIGN_UNITS=(
    "/etc/systemd/system/pgu-ticket-board.service"
    "/etc/systemd/system/pgu-ticket-board-canary.service"
    "/etc/systemd/system/mefp-ticket-board.service"
    "/etc/systemd/system/otto-ticket-board.service"
)

work_root=""
source_dir=""
foreign_before=""
roles_live_at_entry=""
mutation_started=0
roles_stopped=0
cutover_complete=0

die() {
    printf 'SYRD-87 recovery: ERROR: %s\n' "$*" >&2
    exit 1
}

note() {
    printf 'SYRD-87 recovery: %s\n' "$*" >&2
}

sha256_file() {
    sha256sum "$1" | awk '{print $1}'
}

shared_release() {
    readlink -f "$SHARED_ROOT/current"
}

board_release() {
    readlink -f "$BOARD_ROOT/current"
}

board_build_id() {
    curl -fsS "$BOARD_URL/api/board" \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("build_id", ""))'
}

verify_artifact_trust() {
    [[ "$(id -u)" == "0" ]] || die "run as root"
    local invoked resolved owner group mode component
    invoked="${BASH_SOURCE[0]}"
    resolved="$(readlink -f "$invoked")"
    [[ "$resolved" == "$EXPECTED_ARTIFACT_PATH" ]] || \
        die "run the staged copy at $EXPECTED_ARTIFACT_PATH, not $resolved"
    [[ ! -L "$EXPECTED_ARTIFACT_PATH" ]] || die "staged artifact is a symlink"
    [[ -f "$EXPECTED_ARTIFACT_PATH" ]] || die "staged artifact is not a regular file"
    read -r owner group mode < <(stat -c '%U %G %a' "$EXPECTED_ARTIFACT_PATH")
    [[ "$owner $group $mode" == "root root 555" ]] || \
        die "staged artifact is $owner:$group mode $mode, expected root:root mode 555"
    # A writable directory anywhere above the artifact is a writable artifact.
    component="$EXPECTED_ARTIFACT_PATH"
    while [[ "$component" != "/" ]]; do
        component="$(dirname "$component")"
        [[ ! -L "$component" ]] || die "path component is a symlink: $component"
        read -r owner group mode < <(stat -c '%U %G %a' "$component")
        [[ "$owner" == "root" ]] || die "path component $component is owned by $owner"
        (( (8#$mode & 8#022) == 0 )) || die "path component $component is group/world writable"
    done
}

require_commands() {
    local command_name
    for command_name in awk curl dirname git grep id ln mktemp mv python3 readlink rm sha256sum stat sudo systemctl tmux; do
        command -v "$command_name" >/dev/null 2>&1 || die "missing command: $command_name"
    done
    [[ -x "$SWITCHYARD_BIN" ]] || die "missing installed switchyard command: $SWITCHYARD_BIN"
}

# Running this from inside one of the panes it is about to stop would kill its
# own caller part-way through the cutover. The operator runs it from a desktop
# terminal or a root console, not from a role pane.
refuse_role_pane() {
    [[ -z "${TMUX:-}" ]] || die "do not run this from inside a tmux pane; use a plain root terminal"
    local caller role
    caller="${SUDO_USER:-$(id -un)}"
    for role in "${ROLES[@]}"; do
        [[ "$caller" != "$PROJECT-$role" ]] || \
            die "do not run this as $caller; this artifact stops that role's session"
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
    # Both rollback targets have to exist before anything is changed, because
    # after the cutover is the wrong time to discover one of them is missing.
    git -c "safe.directory=$CACHE" --git-dir="$CACHE" cat-file -e "$EXPECTED_SHARED_PREVIOUS^{commit}" || \
        die "trusted cache lacks the previous shared release $EXPECTED_SHARED_PREVIOUS"
    git -c "safe.directory=$CACHE" --git-dir="$CACHE" cat-file -e "$EXPECTED_BOARD_PREVIOUS^{commit}" || \
        die "trusted cache lacks the previous board release $EXPECTED_BOARD_PREVIOUS"
    [[ -d "$SHARED_ROOT/releases/$EXPECTED_SHARED_PREVIOUS" ]] || \
        die "previous shared release directory is missing"
    [[ -d "$BOARD_ROOT/releases/$EXPECTED_BOARD_PREVIOUS" ]] || \
        die "previous board release directory is missing"
}

verify_tenant_ownership() {
    local owner group mode path
    id "$PROJECT_OWNER" >/dev/null 2>&1 || die "missing project account $PROJECT_OWNER"
    for path in "$TENANT_CONFIG" "$UPGRADE_JOURNAL" "$DESKTOP_POLICY"; do
        [[ -f "$path" && ! -L "$path" ]] || die "tenant artifact is missing or a symlink: $path"
        read -r owner group mode < <(stat -c '%U %G %a' "$path")
        [[ "$owner" == "$PROJECT_OWNER" ]] || \
            die "$path is owned by $owner, expected $PROJECT_OWNER"
    done
    read -r owner group mode < <(stat -c '%U %G %a' "$DESKTOP_POLICY")
    (( (8#$mode & 8#007) == 0 )) || die "desktop policy $DESKTOP_POLICY is world readable"
}

# The abandoned cutover this recovery is cleaning up after. Any other shape is
# a host this artifact was not reviewed against.
verify_partial_state() {
    local marker_commit
    [[ "$(shared_release)" == "$SHARED_ROOT/releases/$EXPECTED_SHARED_PREVIOUS" ]] || \
        die "shared release is $(shared_release), expected $EXPECTED_SHARED_PREVIOUS"
    marker_commit="$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["commit"])
' "$TENANT_RELEASE_MARKER")"
    [[ "$marker_commit" == "$EXPECTED_SHARED_PREVIOUS" ]] || \
        die "tenant tooling marker is $marker_commit, expected $EXPECTED_SHARED_PREVIOUS"
    python3 - "$UPGRADE_JOURNAL" <<'PY'
import json
import sys

journal = json.load(open(sys.argv[1]))
phases = {name: entry.get("state") for name, entry in journal.get("phases", {}).items()}
expected = {
    "artifacts": "done",
    "accounts": "done",
    "identities": "rolled back",
    "director": "pending",
}
if phases != expected:
    raise SystemExit(
        f"upgrade journal phases are {phases}, expected {expected}; this artifact "
        "was reviewed against the rolled-back identities state only"
    )
PY
    [[ "$(board_release)" == "$BOARD_ROOT/releases/$EXPECTED_BOARD_PREVIOUS" ]] || \
        die "board release is $(board_release), expected $EXPECTED_BOARD_PREVIOUS"
    [[ "$(board_build_id)" == "$EXPECTED_BOARD_PREVIOUS" ]] || \
        die "live board build is $(board_build_id), expected $EXPECTED_BOARD_PREVIOUS"
    systemctl is-active --quiet "$SERVICE" || die "$SERVICE is not active"
}

# Fingerprint, not a pinned expectation: these belong to other tenants and are
# free to change on their own schedule. What must not change is that this run
# changed them.
foreign_fingerprint() {
    local unit
    for unit in "${FOREIGN_UNITS[@]}"; do
        if [[ -f "$unit" ]]; then
            printf '%s %s %s\n' "$unit" "$(sha256_file "$unit")" \
                "$(systemctl is-active "$(basename "$unit")" 2>/dev/null || true)"
        else
            printf '%s absent absent\n' "$unit"
        fi
    done
    local tenant
    for tenant in pgu mefp otto; do
        if [[ -e "/home/$PROJECT_OWNER/$tenant-ticketboard-live/current" ]]; then
            printf '%s %s\n' "$tenant" "$(readlink -f "/home/$PROJECT_OWNER/$tenant-ticketboard-live/current")"
        else
            printf '%s absent\n' "$tenant"
        fi
    done
}

verify_foreign_tenants_unchanged() {
    local after
    after="$(foreign_fingerprint)"
    if [[ "$after" != "$foreign_before" ]]; then
        printf 'SYRD-87 recovery: before:\n%s\nSYRD-87 recovery: after:\n%s\n' \
            "$foreign_before" "$after" >&2
        die "another tenant's units or release pointer changed; this rollout must touch only $PROJECT"
    fi
}

prepare_source_checkout() {
    work_root="$(mktemp -d /tmp/syrd-87-recovery.XXXXXX)"
    source_dir="$work_root/source"
    git -c "safe.directory=$CACHE" clone --no-checkout --quiet "$CACHE" "$source_dir"
    git -C "$source_dir" checkout --detach --quiet "$EXPECTED_TARGET"
    [[ "$(git -C "$source_dir" rev-parse HEAD)" == "$EXPECTED_TARGET" ]] || \
        die "temporary checkout commit mismatch"
    [[ "$(git -C "$source_dir" rev-parse HEAD^{tree})" == "$EXPECTED_TREE" ]] || \
        die "temporary checkout tree mismatch"
    bash -n "$source_dir/scripts/install-switchyard"
    bash -n "$source_dir/scripts/ticket-board-service.sh"
    [[ -x "$source_dir/scripts/switchyard" ]] || die "candidate switchyard entry point is not executable"
    [[ -f "$source_dir/scripts/switchyard-display-attach" ]] || \
        die "candidate lacks the SYRD-66 display bridge"
    [[ -f "$source_dir/scripts/presentation_controller.py" ]] || \
        die "candidate lacks the presentation controller"
}

# One pinned source for every phase. The 249d1f1a rollout failed because the
# accounts phase handed the upgrade back through sudo, which carries neither
# arguments nor environment, and the rerun then had no source to resolve.
switchyard_pinned() {
    local entry="$1"
    shift
    "$entry" "$@" \
        --source-repo "$source_dir" \
        --commit-git-dir "$CACHE" \
        --deploy-ref "$EXPECTED_TARGET"
}

# Deliberately the candidate's own entry point, not the installed trampoline.
# Until install_shared_release runs, $SWITCHYARD_BIN is still the abandoned
# release, whose upgrade predates the repatriation contract this preflight
# exists to exercise; dry-running that would prove nothing about what follows.
#
# This runs after the roles are checkpointed, not before. The project-account
# migration probes for live legacy panes and refuses before it looks at the
# dry-run flag at all, so a dry run taken while the roles are up reports only
# that the roles are up -- which is what happened on the first operator run
# (SYRD-89). Stopping them is the mutation this artifact must make anyway, and
# the dry run is the gate in front of the parts that follow it.
dry_run_upgrade() {
    local output status=0
    output="$(switchyard_pinned "$source_dir/scripts/switchyard" upgrade "$PROJECT" \
        --dry-run --desktop-policy "$DESKTOP_POLICY" 2>&1)" || status=$?
    printf '%s\n' "$output" >&2
    (( status == 0 )) || \
        die "the supported upgrade refuses this host in dry-run; no release, unit or identity was changed"
    if grep -q 'refusing' <<<"$output"; then
        die "the upgrade dry-run reported a refusal; no release, unit or identity was changed"
    fi
}

live_role_sessions() {
    local role account count=0
    for role in "${ROLES[@]}"; do
        account="$PROJECT-$role"
        id "$account" >/dev/null 2>&1 || continue
        if sudo -u "$account" -H tmux has-session -t "$PROJECT-$role" >/dev/null 2>&1; then
            printf '%s\n' "$account"
            count=$((count + 1))
        fi
    done
    return 0
}

# What this run actually checkpointed: live when it started, not live now. A
# role that was already down before the artifact ran was not stopped by it, and
# reporting it as checkpointed would credit the artifact with someone else's
# outage -- Main was already absent on the host this was written for (SYRD-89).
checkpointed_role_sessions() {
    local now account
    now="$(live_role_sessions)"
    while read -r account; do
        [[ -n "$account" ]] || continue
        grep -qxF "$account" <<<"$now" || printf '  %s\n' "$account"
    done <<<"$roles_live_at_entry"
    return 0
}

# Context, kept separate on purpose: these were down before this run touched
# anything, so they are the operator's to explain, not this artifact's.
absent_role_sessions_at_entry() {
    local role account
    for role in "${ROLES[@]}"; do
        account="$PROJECT-$role"
        id "$account" >/dev/null 2>&1 || continue
        grep -qxF "$account" <<<"$roles_live_at_entry" || printf '  %s\n' "$account"
    done
    return 0
}

checkpoint_roles() {
    note "stopping every $PROJECT role at a resumable checkpoint, including the director's session"
    local after status=0
    # The pre-stop set is captured before the first mutation and kept, so what
    # this run stopped stays a difference rather than a snapshot.
    # Run as root through the candidate entry point: the roles are still bound
    # to dedicated accounts, so each stop is wrapped in `sudo -u <account>` by
    # the release itself, and only root can reach all six.
    "$source_dir/scripts/switchyard" stop "$PROJECT" || status=$?
    after="$(live_role_sessions)"
    # What recovery needs to know is what is actually stopped, not what the stop
    # command returned. A stop that checkpoints some roles and then fails still
    # leaves sessions down, and taking the exit code as the signal made recovery
    # silent about exactly that case (SYRD-89).
    [[ "$roles_live_at_entry" == "$after" ]] || roles_stopped=1
    if (( status != 0 )); then
        if (( roles_stopped == 1 )); then
            die "the supported stop command failed (exit $status) after checkpointing some roles; no release or identity was changed"
        fi
        die "the supported stop command failed (exit $status) and checkpointed nothing; no release or identity was changed"
    fi
    [[ -z "$after" ]] || \
        die "these role sessions are still live after stop: $after"
}

install_shared_release() {
    note "installing the shared release $EXPECTED_TARGET before anything depends on it"
    env SWITCHYARD_SOURCE_REPO="$source_dir" \
        SWITCHYARD_SOURCE_REF="$EXPECTED_TARGET" \
        SWITCHYARD_SHARED_INSTALL_ROOT="$SHARED_ROOT" \
        bash "$source_dir/scripts/install-switchyard" --apply
    [[ "$(shared_release)" == "$SHARED_ROOT/releases/$EXPECTED_TARGET" ]] || \
        die "shared release is $(shared_release) after install, expected $EXPECTED_TARGET"
}

run_upgrade() {
    note "running the supported tenant upgrade onto the one-project-account runtime"
    # The installed trampoline now, because install_shared_release has already
    # repointed it at this same commit; the tenant must be upgraded by the
    # release it will keep running.
    switchyard_pinned "$SWITCHYARD_BIN" upgrade "$PROJECT" --desktop-policy "$DESKTOP_POLICY"
}

verify_release_pointers() {
    [[ "$(shared_release)" == "$SHARED_ROOT/releases/$EXPECTED_TARGET" ]] || \
        die "shared release is $(shared_release), expected $EXPECTED_TARGET"
    local marker_commit
    marker_commit="$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["commit"])
' "$TENANT_RELEASE_MARKER")"
    [[ "$marker_commit" == "$EXPECTED_TARGET" ]] || \
        die "tenant tooling marker is $marker_commit, expected $EXPECTED_TARGET"
    [[ "$(board_release)" == "$BOARD_ROOT/releases/$EXPECTED_TARGET" ]] || \
        die "board release is $(board_release), expected $EXPECTED_TARGET"
}

# The config path is a parameter so the contract regression can exercise this
# against fixtures; it defaults to the tenant's real configuration.
verify_project_account_runtime() {
    local config="${1:-$TENANT_CONFIG}"
    python3 - "$config" "${ROLES[@]}" <<'PY'
import json
import sys

config = json.load(open(sys.argv[1]))
roles = sys.argv[2:]
if config.get("role_state_isolation") is not True:
    raise SystemExit("tenant config did not flip role_state_isolation; the cutover did not complete")
owner = config.get("run_as_user")
if not owner:
    raise SystemExit("tenant config names no project account")
present = {role.get("role") for role in config.get("roles", [])}
missing = sorted(set(roles) - present)
if missing:
    raise SystemExit(f"tenant config lost roles: {missing}")
bound = sorted(
    role["role"] for role in config["roles"] if role.get("run_as_user") not in (None, "", owner)
)
if bound:
    raise SystemExit(f"roles are still bound to dedicated accounts: {bound}")
print(f"SYRD-87 recovery: every role runs as {owner} with role-private state")
PY
}

verify_board_process_authority() {
    curl -fsS "$BOARD_URL/api/board" | python3 -c '
import json, sys

payload = json.load(sys.stdin)
build = payload.get("build_id", "")
expected = sys.argv[1]
assert build == expected, f"live board build is {build}, expected {expected}"
' "$EXPECTED_TARGET"
    curl -fsS "$BOARD_URL/api/runtime-assignments" | python3 -c '
import json, sys

payload = json.load(sys.stdin)
# The point of the cutover: authority comes from the registered process, not
# from a per-role uid that no longer exists (SYRD-69).
assert payload.get("authority_mode") == "process", payload
assignments = payload.get("assignments")
assert isinstance(assignments, dict) and assignments, payload
missing = [role for role in sys.argv[1:] if role not in assignments]
assert not missing, f"roles with no runtime assignment: {missing}"
' "${ROLES[@]}"
}

verify_socket_contract() {
    [[ -S "$BOARD_SOCKET" ]] || die "board socket is missing: $BOARD_SOCKET"
    local owner group mode
    read -r owner group mode < <(stat -c '%U %G %a' "$BOARD_SOCKET")
    [[ "$mode" == "660" ]] || die "board socket is mode $mode, expected 660"
}

verify_display_bridge_installed() {
    local bridge="$TENANT_TOOLING/switchyard-display-attach"
    local owner group mode
    [[ -f "$bridge" && ! -L "$bridge" ]] || die "SYRD-66 display bridge is missing: $bridge"
    read -r owner group mode < <(stat -c '%U %G %a' "$bridge")
    [[ "$owner $group" == "root root" ]] || \
        die "display bridge is $owner:$group, expected root:root"
    (( (8#$mode & 8#022) == 0 )) || die "display bridge is group/world writable"
}

project_account() {
    python3 -c '
import json, sys
config = json.load(open(sys.argv[1]))
print(config.get("run_as_user") or "")
' "$TENANT_CONFIG"
}

verify_no_privileged_parent() {
    # Nothing this artifact installs may leave a root-owned process attached to
    # a role session. A pane whose owner is root is the SYRD-66 defect, and a
    # `die` inside a pipeline would only exit the pipeline's own subshell, so
    # the pane list is collected first and checked in this shell.
    local role account panes pane_pid pane_owner
    account="$(project_account)"
    [[ -n "$account" ]] || die "cannot resolve the project account"
    for role in "${ROLES[@]}"; do
        sudo -u "$account" -H tmux has-session -t "$PROJECT-$role" >/dev/null 2>&1 || continue
        panes="$(sudo -u "$account" -H tmux list-panes -t "$PROJECT-$role" -F '#{pane_pid}')"
        while read -r pane_pid; do
            [[ -n "$pane_pid" ]] || continue
            pane_owner="$(stat -c '%U' "/proc/$pane_pid" 2>/dev/null || echo missing)"
            [[ "$pane_owner" != "root" ]] || \
                die "role $role has a root-owned pane process $pane_pid"
        done <<<"$panes"
    done
}

verify_target_state() {
    verify_release_pointers
    verify_project_account_runtime
    verify_board_process_authority
    verify_socket_contract
    verify_display_bridge_installed
    verify_no_privileged_parent
    verify_foreign_tenants_unchanged
}

report_director_phase() {
    cat >&2 <<EOF
SYRD-87 recovery: the privileged phases are complete. The remaining phase is the
director's own, and is deliberately not attempted here: it is an unprivileged
board write that only the director's session can make.

  Run as the director, unprivileged:
    switchyard finish-upgrade $PROJECT

Then confirm the six-pane presentation from the authorized desktop account:
    switchyard $PROJECT
EOF
}

# Recovery is a real rollback here, unlike the board-only activation in SYRD-85:
# nothing in the mutating phase is one-way. The shared release is a symlink, the
# board release is a symlink the standard gate manages, and the roles resume from
# the resumable checkpoints taken before the cutover. What this must never do is
# claim more than it did.
recover_previous_state() {
    # The dry run and the checkpoints both sit inside the recovered region now,
    # so this runs in two quite different situations: one where only the roles
    # were stopped, and one where the shared release moved too. Saying which is
    # part of being accurate about what was restored (SYRD-89).
    if [[ "$(shared_release)" == "$SHARED_ROOT/releases/$EXPECTED_SHARED_PREVIOUS" ]]; then
        note "recovery: the shared release never moved; it is still $EXPECTED_SHARED_PREVIOUS"
    else
        note "recovery: restoring the previous shared release $EXPECTED_SHARED_PREVIOUS"
        [[ -d "$SHARED_ROOT/releases/$EXPECTED_SHARED_PREVIOUS" ]] || {
            note "recovery: previous shared release directory is gone; cannot restore it"
            return 1
        }
        ln -sfn "$SHARED_ROOT/releases/$EXPECTED_SHARED_PREVIOUS" "$SHARED_ROOT/current.recovery"
        mv -Tf "$SHARED_ROOT/current.recovery" "$SHARED_ROOT/current"
    fi
    [[ "$(shared_release)" == "$SHARED_ROOT/releases/$EXPECTED_SHARED_PREVIOUS" ]] || return 1
    systemctl is-active --quiet "$SERVICE" || return 1
    verify_foreign_tenants_unchanged
    note "recovery: shared release restored to $EXPECTED_SHARED_PREVIOUS; board build is $(board_build_id); $SERVICE is active"
    local still_live checkpointed already_absent
    still_live="$(live_role_sessions)"
    checkpointed="$(checkpointed_role_sessions)"
    already_absent="$(absent_role_sessions_at_entry)"
    if (( roles_stopped == 1 )) || [[ -n "$checkpointed" ]]; then
        # Deliberately not restarted from here. The roles are stopped at
        # resumable checkpoints and their state is intact; bringing the six-pane
        # presentation back up is a desktop-session operation, and a root trap
        # handler is the worst possible place to attempt it. Say so plainly, and
        # name which sessions are actually down, so a partial checkpoint is not
        # reported as if it were all or nothing (SYRD-89).
        cat >&2 <<EOF
SYRD-87 recovery: these $PROJECT roles were checkpointed by this run and were
not restarted:
${checkpointed:-  (none)}
Still live:
${still_live:-  (none)}
Already absent before this run started, and not this artifact's doing:
${already_absent:-  (none)}
Their resumable state is intact and no account, worktree or session store was
deleted. Bring them back from the authorized desktop account:

    switchyard $PROJECT

EOF
    fi
}

on_exit() {
    local status="$1"
    trap - EXIT
    if (( status != 0 && mutation_started == 1 && cutover_complete == 0 )); then
        local recovery_status=0
        (recover_previous_state) || recovery_status=$?
        if (( recovery_status != 0 )); then
            cat >&2 <<EOF
SYRD-87 recovery: CRITICAL: the cutover failed and recovery did not complete.
Do not rerun this artifact. Report, with this output:
  shared release : $(shared_release)
  board release  : $(board_release)
  board build    : $(board_build_id 2>/dev/null || echo unreachable)
  service        : $(systemctl is-active "$SERVICE" 2>/dev/null || echo unknown)
  roles stopped  : $roles_stopped
No account was created or deleted and no other tenant was touched.
EOF
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
    refuse_role_pane
    unset GIT_DIR GIT_WORK_TREE
    verify_tenant_ownership
    foreign_before="$(foreign_fingerprint)"

    # Already done: prove it and change nothing. A retry has to be safe, because
    # the reason to rerun this is usually that nobody is sure what state the host
    # is in (SYRD-86).
    if [[ "$(shared_release)" == "$SHARED_ROOT/releases/$EXPECTED_TARGET" ]]; then
        verify_target_state
        cutover_complete=1
        note "already recovered onto $EXPECTED_TARGET; nothing was changed"
        report_director_phase
        return
    fi

    verify_partial_state
    verify_exact_source
    prepare_source_checkout

    # The pre-stop set, captured while nothing has changed yet, so recovery can
    # report what this run stopped rather than what happens to be down.
    roles_live_at_entry="$(live_role_sessions)"

    # Checkpointing the roles is the first mutation and it has to come first:
    # the migration contract refuses while any legacy pane is live, in dry run
    # exactly as in earnest, so there is no meaningful dry run to take before
    # this point. Everything above changed nothing; everything below is covered
    # by the recovery path (SYRD-89).
    mutation_started=1
    checkpoint_roles
    dry_run_upgrade
    install_shared_release
    run_upgrade
    verify_target_state
    cutover_complete=1
    note "complete; shared release $EXPECTED_TARGET, board build $(board_build_id), project-account runtime active"
    report_director_phase
}

trap 'on_exit $?' EXIT
main "$@"
