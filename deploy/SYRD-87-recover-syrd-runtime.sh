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
# The installed release this artifact puts in place, and the only tree the
# post-install upgrade may be sourced from. A release directory carries
# .switchyard-release.json; a plain git checkout does not, and that difference
# decides whether the roles end up with provenanced tooling (SYRD-89).
readonly TARGET_RELEASE_ROOT="/opt/switchyard/releases/9a4d0a648c839fa5f8945342edefcd49ab553407"
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
# Exactly the states this artifact has been reviewed against, and no other.
#
# There are two, because the first SYRD-89 operator run reached the supported
# upgrade and then failed in this artifact's own verification. Its recovery put
# the shared release symlink back, so the host is not where it started:
#
#   entry     the reviewed baseline. The identities transaction had rolled back,
#             no release phase had been recorded, and the staged tooling still
#             carried the previous release's marker.
#
#   resumed   what the failed run left. The upgrade succeeded through
#             identities and recorded release=ready, so the journal is ahead;
#             the shared symlink was restored to the previous release by
#             recovery; and the staged marker is gone, because the upgrade was
#             sourced from a markerless checkout and the staging contract
#             removes a marker a source cannot vouch for (SYRD-89).
#
# Both are safe to run from, and they are told apart rather than merged: a state
# that is neither is a host nobody reviewed, and the point of this check is to
# refuse it.
# Which reviewed state this host's journal is in, or a refusal. Separated from
# the checks around it so the contract regression can drive it over fixtures
# rather than re-implement it: a test that keeps its own copy of a classifier
# proves only that the copy agrees with itself (SYRD-89).
upgrade_journal_state() {
    python3 - "$1" <<'PY'
import json
import sys

journal = json.load(open(sys.argv[1]))
phases = {name: entry.get("state") for name, entry in journal.get("phases", {}).items()}
entry = {
    "artifacts": "done",
    "accounts": "done",
    "identities": "rolled back",
    "director": "pending",
}
resumed = {
    "artifacts": "done",
    "accounts": "done",
    "identities": "done",
    "release": "ready",
    "director": "pending",
}
if phases == entry:
    print("entry")
elif phases == resumed:
    print("resumed")
else:
    raise SystemExit(
        f"upgrade journal phases are {phases}; this artifact is reviewed against "
        f"{entry} (entry) and {resumed} (resumed after an interrupted run) only"
    )
PY
}

verify_partial_state() {
    local state marker_commit
    [[ "$(shared_release)" == "$SHARED_ROOT/releases/$EXPECTED_SHARED_PREVIOUS" ]] || \
        die "shared release is $(shared_release), expected $EXPECTED_SHARED_PREVIOUS"

    state="$(upgrade_journal_state "$UPGRADE_JOURNAL")" || \
        die "cannot classify the upgrade journal state"

    # The marker follows from the state, and is checked against it rather than
    # accepted either way: an absent marker in the entry state would mean
    # something else stripped it, and a present one in the resumed state would
    # mean the staging contract did not do what it says.
    if [[ "$state" == "entry" ]]; then
        [[ -f "$TENANT_RELEASE_MARKER" ]] || \
            die "tenant tooling marker $TENANT_RELEASE_MARKER is absent, but the journal says nothing has run"
        marker_commit="$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["commit"])
' "$TENANT_RELEASE_MARKER")"
        [[ "$marker_commit" == "$EXPECTED_SHARED_PREVIOUS" ]] || \
            die "tenant tooling marker is $marker_commit, expected $EXPECTED_SHARED_PREVIOUS"
    else
        [[ ! -e "$TENANT_RELEASE_MARKER" ]] || \
            die "tenant tooling marker $TENANT_RELEASE_MARKER exists, but the interrupted run should have left none"
    fi
    note "resuming from the $state state"

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
#
# The source is a parameter rather than a constant because the two upgrade
# invocations here are pinned to different trees on purpose, and using one for
# both is the SYRD-89 defect: the dry run has to exercise the candidate before
# it is installed, and the real upgrade has to be sourced from the installed
# release so the tooling it stages carries that release's marker. The git
# directory stays the cache either way -- a release directory is an export with
# no history of its own, which is exactly what --commit-git-dir is for.
switchyard_pinned() {
    local entry="$1"
    local source="$2"
    shift 2
    "$entry" "$@" \
        --source-repo "$source" \
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
    output="$(switchyard_pinned "$source_dir/scripts/switchyard" "$source_dir" upgrade "$PROJECT" \
        --dry-run --desktop-policy "$DESKTOP_POLICY" 2>&1)" || status=$?
    printf '%s\n' "$output" >&2
    (( status == 0 )) || \
        die "the supported upgrade refuses this host in dry-run; no release, unit or identity was changed"
    if grep -q 'refusing' <<<"$output"; then
        die "the upgrade dry-run reported a refusal; no release, unit or identity was changed"
    fi
}

# Which account a role's session belongs to, according to the configuration.
#
# Not the retired `<project>-<role>` account by assumption. The two reviewed
# entry states differ exactly here: before the cutover each role has its own
# account, and after it every role runs as the project account. Those accounts
# still exist, so `id` succeeds for both and the difference is invisible to any
# check that guesses. Asking the configuration is what makes discovery work in
# both, and getting it wrong meant six live sessions read as none -- so the
# artifact stopped them while reporting that it had stopped nothing, and a
# later failure would have stranded them with no checkpoint record at all
# (SYRD-89 audit).
# The config path is a parameter for the same reason verify_project_account_runtime
# takes one: the contract regression has to exercise both reviewed states, and
# it cannot do that against a readonly path pinned to this host.
role_session_account() {
    local role="$1" config="${2:-$TENANT_CONFIG}" configured
    configured="$(python3 -c '
import json, sys
config = json.load(open(sys.argv[1]))
for entry in config.get("roles", []):
    if entry.get("role") == sys.argv[2]:
        print((entry.get("run_as_user") or "").strip())
        break
' "$config" "$role" 2>/dev/null || true)"
    if [[ -n "$configured" ]]; then
        printf '%s\n' "$configured"
        return 0
    fi
    project_account "$config"
}

# tmux as one account. Without the sudo hop when that account is already this
# one: sudo to yourself is not a privilege change, needs a grant that a tenant
# has no reason to hold, and fails where it is absent -- which would make every
# session invisible to a check running as the project account itself.
account_tmux() {
    local account="$1"
    shift
    if [[ "$account" == "$(id -un)" ]]; then
        tmux "$@"
    else
        sudo -u "$account" -H tmux "$@"
    fi
}

# The tmux session name, which is unique per role whichever account holds it.
# The account is not: after the cutover all six answer to the same one.
live_role_sessions() {
    local config="${1:-$TENANT_CONFIG}" role account
    for role in "${ROLES[@]}"; do
        account="$(role_session_account "$role" "$config")"
        [[ -n "$account" ]] || continue
        id "$account" >/dev/null 2>&1 || continue
        if account_tmux "$account" has-session -t "=$PROJECT-$role" >/dev/null 2>&1; then
            printf '%s\n' "$PROJECT-$role"
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
        account="$(role_session_account "$role")"
        [[ -n "$account" ]] || continue
        id "$account" >/dev/null 2>&1 || continue
        grep -qxF "$PROJECT-$role" <<<"$roles_live_at_entry" || printf '  %s\n' "$PROJECT-$role"
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
    [[ -f "$TARGET_RELEASE_ROOT/.switchyard-release.json" ]] || \
        die "installed release $TARGET_RELEASE_ROOT carries no .switchyard-release.json"
    # The installed trampoline, because install_shared_release has already
    # repointed it at this same commit; the tenant must be upgraded by the
    # release it will keep running.
    #
    # And sourced from the installed release, not from the temporary checkout.
    # The upgrade restages the roles' tooling from whatever --source-repo names,
    # and that staging deliberately removes the staged marker when the source
    # carries none, so that a bundle can never be stamped with a release it did
    # not come from (SYRD-60). A git checkout carries none. Pinning the upgrade
    # to one therefore left the roles with unprovenanced tooling -- which the
    # staging contract accepts, because a markerless source and a markerless
    # bundle agree -- and left this artifact demanding a marker its own choice
    # of source had just removed (SYRD-89).
    switchyard_pinned "$SWITCHYARD_BIN" "$TARGET_RELEASE_ROOT" upgrade "$PROJECT" \
        --desktop-policy "$DESKTOP_POLICY"
}

# The pointers the privileged phases own. The board release is deliberately not
# among them: deploying it is the operator release phase, which `switchyard
# upgrade` reports and records as `ready` rather than performing. Asserting it
# here claimed a phase nobody had run (SYRD-89).
# The operator release phase, rendered by the release that will run it.
#
# `switchyard upgrade` prints this sequence and records the phase `ready`; it
# does not perform it. On an ordinary host that is right -- deploying is a
# separate, deliberate act. Here it is not optional and there is nobody else to
# do it: this artifact is the one root command SYRD-19 authorises, and stopping
# at `ready` leaves the tenant with its roles running as the project account
# while the board still authorises the retired per-role uids. Every role write
# is refused in that state, the director's included, so `finish-upgrade` cannot
# be performed and the tenant has no way forward at all (SYRD-89 review).
#
# The commands are asked of the installed release rather than written out here,
# for the same reason refresh_staged_role_tooling asks for its staging commands:
# what this runs and what the upgrade prints have to be the same sequence, and
# a copy would drift from it silently.
render_release_phase_commands() {
    python3 - "$TARGET_RELEASE_ROOT" "$TENANT_CONFIG" "$CACHE" "$EXPECTED_TARGET" "$PROJECT" <<'PY'
import json
import sys
from pathlib import Path

release_root, config_path, commit_git_dir, deploy_ref, project = sys.argv[1:6]
# The release root, not its scripts directory: team_launcher imports
# scripts.ticket_board, so it has to be importable as scripts.team_launcher.
# Putting scripts/ on the path instead made `import team_launcher` succeed only
# where the working directory happened to hold a scripts/ package of its own,
# and raise ModuleNotFoundError: No module named 'scripts' anywhere else
# (SYRD-89 audit).
sys.path.insert(0, release_root)
import scripts.team_launcher as tl

config = tl.load_project_config(project, Path(config_path))
status = tl.tenant_release_status(
    config,
    config_path=Path(config_path),
    source_repo=Path(release_root),
    commit_git_dir=commit_git_dir,
    deploy_ref=deploy_ref,
)
if status is None:
    raise SystemExit("the release status could not be read; this tenant has no board root")
if not status.target_sha:
    raise SystemExit(f"the deploy ref did not resolve: {status.resolve_error}")
if status.target_sha != deploy_ref:
    raise SystemExit(f"the deploy ref resolved to {status.target_sha}, not {deploy_ref}")
if status.provisioned_system_unit is None:
    raise SystemExit("the generated board, canary and listener units are incomplete")
steps = [
    ("stop the notification listener", tl.tenant_release_listener_command(status, project, "stop")),
    ("install the root-staged units and reload", tl.tenant_release_unit_install_command(status, project)),
    ("deploy the board release", tl.tenant_release_deploy_command(status, project)),
    ("start the notification listener", tl.tenant_release_listener_command(status, project, "start")),
]
for label, command in steps:
    if not command:
        raise SystemExit(f"the release renderer produced no command for: {label}")
    print(json.dumps({"label": label, "command": command}))
PY
}

# Record the release phase from what is now deployed, using the release's own
# reader and recorder rather than writing the journal by hand.
#
# The deploy moves the board; it does not touch the upgrade journal, which the
# upgrade wrote as `ready` before any of this ran. Leaving it there would make
# verify_upgrade_phase_journal abort on a run whose deploy had just succeeded --
# the artifact would undo a working tenant over its own bookkeeping (SYRD-89
# audit). record_release_phase_from_status reads the deployed release back and
# records `done` exactly when it matches, which is the same call `switchyard
# upgrade` makes and the only thing entitled to say the phase is finished.
record_release_phase() {
    note "recording the release phase from the deployed board"
    python3 - "$TARGET_RELEASE_ROOT" "$TENANT_CONFIG" "$CACHE" "$EXPECTED_TARGET" "$PROJECT" <<'PY'
import sys
from pathlib import Path

release_root, config_path, commit_git_dir, deploy_ref, project = sys.argv[1:6]
sys.path.insert(0, release_root)
import scripts.team_launcher as tl

config = tl.load_project_config(project, Path(config_path))
status = tl.tenant_release_status(
    config,
    config_path=Path(config_path),
    source_repo=Path(release_root),
    commit_git_dir=commit_git_dir,
    deploy_ref=deploy_ref,
)
deployed = tl.record_release_phase_from_status(
    config, config_path=Path(config_path), status=status
)
if not deployed:
    current = getattr(status, "current_sha", None) if status else None
    raise SystemExit(
        f"the board reports {current or 'no release'} after the deploy, not {deploy_ref}; "
        "the release phase is not done and will not be recorded as such"
    )
PY
}

# Run it. The order is the renderer's, not this artifact's: the listener comes
# down before the migrations the deploy runs and back up only after, because it
# reads the schema the release changes (SYRD-45).
run_release_phase() {
    note "performing the operator release phase: deploying board release $EXPECTED_TARGET"
    local rendered label command
    rendered="$(render_release_phase_commands)" || \
        die "could not render the release phase from $TARGET_RELEASE_ROOT"
    [[ -n "$rendered" ]] || die "the release phase rendered no commands"
    while IFS= read -r line; do
        [[ -n "$line" ]] || continue
        label="$(printf '%s' "$line" | python3 -c 'import json,sys; print(json.load(sys.stdin)["label"])')"
        command="$(printf '%s' "$line" | python3 -c 'import json,sys; print(json.load(sys.stdin)["command"])')"
        note "release phase: $label"
        # eval because these are rendered shell, quoted by the renderer that
        # produced them; running them any other way would be running something
        # other than what the upgrade prints.
        eval "$command" || die "release phase step failed: $label"
    done <<<"$rendered"
    record_release_phase
}

# Bring the roles back under the project account, opening no window.
#
# Not politeness: under process authority a role is authorised by the runtime it
# registers when its pane starts, so until the panes are back the board has no
# assignments and every write is refused -- the director's finish-upgrade
# included. Leaving the tenant there would be leaving it with no way forward
# (SYRD-89 review).
#
# The release's own pane entry point, which is what the identities phase uses to
# restart roles without opening a second window, and which routes each pane
# through ticket-board-register-runtime.
restart_project_account_roles() {
    note "restarting the $PROJECT roles under the project account so their runtimes register"
    local role account failures=0
    account="$(project_account)"
    [[ -n "$account" ]] || die "cannot resolve the project account"
    for role in "${ROLES[@]}"; do
        sudo -u "$account" -H env -u TICKET_BOARD_PANE_SESSION_ID -u TICKET_BOARD_PANE_TARGET \
            "$SHARED_ROOT/current/scripts/team-launcher" "$PROJECT" pane attach-or-start "$role" \
            --config "$TENANT_CONFIG" --skip-launcher-check --no-attach \
            || { note "role $role did not come back"; failures=$((failures + 1)); }
    done
    (( failures == 0 )) || die "$failures role session(s) did not restart; the board has no runtime to authorise them"
}

# A role is authorised only once the board can see its registered runtime. This
# is the check that says whether the director has a write path at all, which is
# the difference between a recovered tenant and a stranded one.
verify_director_write_path() {
    curl -fsS "$BOARD_URL/api/runtime-assignments" | python3 -c '
import json, sys

payload = json.load(sys.stdin)
mode = payload.get("authority_mode")
assignments = payload.get("assignments") or {}
if mode != "process":
    raise SystemExit(f"board authority mode is {mode}; no project-account role can write")
if "director" not in assignments:
    raise SystemExit(
        f"the board has no registered runtime for the director, so finish-upgrade "
        f"cannot be performed; registered: {sorted(assignments)}"
    )
'
}

verify_release_pointers() {
    [[ "$(shared_release)" == "$SHARED_ROOT/releases/$EXPECTED_TARGET" ]] || \
        die "shared release is $(shared_release), expected $EXPECTED_TARGET"
    [[ -f "$TENANT_RELEASE_MARKER" ]] || \
        die "tenant tooling marker $TENANT_RELEASE_MARKER is absent; the upgrade staged role tooling from a source that names no release"
    local marker_commit
    marker_commit="$(python3 -c '
import json, sys
print(json.load(open(sys.argv[1]))["commit"])
' "$TENANT_RELEASE_MARKER")"
    [[ "$marker_commit" == "$EXPECTED_TARGET" ]] || \
        die "tenant tooling marker is $marker_commit, expected $EXPECTED_TARGET"
}

# Where the supported upgrade actually leaves this tenant, proved rather than
# assumed. `identities` is done because the transaction ran; `release` is ready
# because deploying it is the operator's phase; `director` is pending because
# the board write is the director's own. Any other combination means the upgrade
# did something this artifact was not reviewed against (SYRD-89).
verify_upgrade_phase_journal() {
    python3 - "$UPGRADE_JOURNAL" <<'PY'
import json
import sys

journal = json.load(open(sys.argv[1]))
phases = {name: entry.get("state") for name, entry in journal.get("phases", {}).items()}
expected = {
    "artifacts": "done",
    "accounts": "done",
    "identities": "done",
    "release": "done",
    "director": "pending",
}
if phases != expected:
    raise SystemExit(
        f"upgrade journal phases are {phases}, expected {expected}; the release "
        "phase this artifact performs did not complete"
    )
PY
}

# The board is back on the release it was running before this artifact touched
# it. Used by recovery, which has to be able to say whether the rollback landed
# (SYRD-89).
verify_board_release_still_previous() {
    [[ "$(board_release)" == "$BOARD_ROOT/releases/$EXPECTED_BOARD_PREVIOUS" ]] || \
        die "board release is $(board_release), expected it to still be $EXPECTED_BOARD_PREVIOUS"
    [[ "$(board_build_id)" == "$EXPECTED_BOARD_PREVIOUS" ]] || \
        die "live board build is $(board_build_id), expected it to still be $EXPECTED_BOARD_PREVIOUS"
}

release_phase_deployed() {
    [[ "$(board_release)" == "$BOARD_ROOT/releases/$EXPECTED_TARGET" ]]
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
' "${1:-$TENANT_CONFIG}"
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
        account_tmux "$account" has-session -t "=$PROJECT-$role" >/dev/null 2>&1 || continue
        panes="$(account_tmux "$account" list-panes -t "=$PROJECT-$role" -F '#{pane_pid}')"
        while read -r pane_pid; do
            [[ -n "$pane_pid" ]] || continue
            pane_owner="$(stat -c '%U' "/proc/$pane_pid" 2>/dev/null || echo missing)"
            [[ "$pane_owner" != "root" ]] || \
                die "role $role has a root-owned pane process $pane_pid"
        done <<<"$panes"
    done
}

# Everything the privileged phases of this artifact are answerable for, and
# nothing that waits on a phase somebody else owns.
verify_privileged_phase_state() {
    verify_release_pointers
    verify_upgrade_phase_journal
    verify_project_account_runtime
    verify_socket_contract
    verify_display_bridge_installed
    verify_no_privileged_parent
    verify_foreign_tenants_unchanged
}

# Only meaningful once the operator has deployed the board release: process-bound
# authority is a property of the new board build, so asking a board still running
# the previous one can only ever report the previous answer.
verify_release_phase_state() {
    [[ "$(board_release)" == "$BOARD_ROOT/releases/$EXPECTED_TARGET" ]] || \
        die "board release is $(board_release), expected $EXPECTED_TARGET"
    verify_board_process_authority
}

# Two phases remain, not one. The release phase is the operator's in exactly the
# sense the director phase is the director's: `switchyard upgrade` prints its
# deployment sequence and records it `ready`, and performing it is a separate,
# deliberate act. This artifact reported only the director phase, which is how a
# run that had reached `release=ready` could read as finished (SYRD-89).
report_remaining_phases() {
    cat >&2 <<EOF
SYRD-87 recovery: complete. Shared release $EXPECTED_TARGET is installed, the
tenant's role tooling is staged from it and carries its marker, the board is
$EXPECTED_TARGET with process-bound authority, and every role session is back
under the project account with its runtime registered.

One phase remains. It is the director's own: an unprivileged board write that
only the director's session can make, and it is now possible because the board
authorises the project account the director is running as.

  Run as the director, unprivileged:
    switchyard finish-upgrade $PROJECT

Then confirm the six-pane presentation from the authorized desktop account:
    switchyard $PROJECT
EOF
}

# What is true now, said once, from one reading of the host.
report_state() {
    verify_release_phase_state
    verify_director_write_path
    report_remaining_phases
}

# Recovery is a real rollback here, unlike the board-only activation in SYRD-85:
# nothing in the mutating phase is one-way. The shared release is a symlink, the
# board release is a symlink the standard gate manages, and the roles resume from
# the resumable checkpoints taken before the cutover. What this must never do is
# claim more than it did.
# Whether anything can still write to this board, and if not, why.
#
# `systemctl is-active` says the board answers; it does not say anyone is
# allowed to talk to it. The two halves have to agree: a board on the previous
# release authorises per-role uids, so the configuration must still name role
# accounts that exist, and a board on the new one authorises registered
# runtimes, so a role pane must have registered. A configuration migrated to the
# project account under a board that still wants per-role uids authorises
# nobody at all -- which is the state the first operator run left, with six
# roles running and every write refused (SYRD-89 review).
#
# Prints the reason and returns non-zero; it does not decide what to do about
# it, because the answer differs between the success path and recovery.
board_write_path_problem() {
    local mode assignments account
    mode="$(curl -fsS "$BOARD_URL/api/runtime-assignments" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("authority_mode",""))' 2>/dev/null || true)"
    account="$(project_account 2>/dev/null || true)"
    case "$mode" in
        process)
            assignments="$(curl -fsS "$BOARD_URL/api/runtime-assignments" 2>/dev/null \
                | python3 -c 'import json,sys; print(" ".join(sorted((json.load(sys.stdin).get("assignments") or {}))))' 2>/dev/null || true)"
            [[ -n "$assignments" ]] && return 0
            printf 'the board authorises registered runtimes and none is registered\n'
            return 1
            ;;
        legacy_uid)
            # The old board authorises per-role Unix accounts. The configuration
            # has to still name them, and they have to exist.
            local missing=0 role configured
            for role in "${ROLES[@]}"; do
                configured="$(python3 -c '
import json, sys
config = json.load(open(sys.argv[1]))
for entry in config.get("roles", []):
    if entry.get("role") == sys.argv[2]:
        print(entry.get("run_as_user") or "")
        break
' "$TENANT_CONFIG" "$role" 2>/dev/null || true)"
                if [[ -z "$configured" || "$configured" == "$account" ]]; then
                    missing=$((missing + 1))
                elif ! id "$configured" >/dev/null 2>&1; then
                    missing=$((missing + 1))
                fi
            done
            (( missing == 0 )) && return 0
            printf 'the board authorises per-role Unix accounts but the configuration names %s for %d of %d roles\n' \
                "${account:-the project account}" "$missing" "${#ROLES[@]}"
            return 1
            ;;
        *)
            printf 'the board did not report an authority mode (it may be down or unreachable)\n'
            return 1
            ;;
    esac
}

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

    # The board is its own pointer and the release phase may have moved it. A
    # rollback that restores the shared release and leaves the board on the new
    # one has restored half a tenant.
    if release_phase_deployed; then
        note "recovery: the board moved to $EXPECTED_TARGET and this rollback cannot move it back"
        return 1
    fi
    note "recovery: shared release restored to $EXPECTED_SHARED_PREVIOUS; board build is $(board_build_id); $SERVICE is active"

    # An active service is not a working tenant. If the configuration and the
    # board no longer agree about how a role is authorised, nobody can write --
    # not the roles, and not the director -- and that has to be said as a
    # critical failure rather than folded into a recovery that reports success
    # (SYRD-89 review).
    local write_problem
    if ! write_problem="$(board_write_path_problem)"; then
        cat >&2 <<EOF
SYRD-87 recovery: CRITICAL: the release was rolled back but this tenant has no
legitimate write path: $write_problem.
No role can write to the board, the director included, so \`switchyard
finish-upgrade $PROJECT\` cannot be performed and this tenant cannot be moved
forward or back from a role session.

  shared release : $(shared_release)
  board release  : $(board_release)
  board build    : $(board_build_id 2>/dev/null || echo unreachable)
  project account: $(project_account 2>/dev/null || echo unknown)

An operator must restore a matching pair: either deploy board release
$EXPECTED_TARGET, which authorises the project account this configuration now
names, or restore a configuration whose roles name the per-role accounts the
running board authorises. Do not rerun this artifact until they match.
EOF
        return 1
    fi
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
    # Already done: prove all of it and change nothing. "Already recovered" now
    # means the board moved too, because the release phase is this artifact's to
    # perform; a host with the shared release in place and the board still on the
    # previous one is a half-finished run, not a completed one, and saying so is
    # the difference between a retry that resumes and one that reports success
    # over an outage (SYRD-89 review).
    if [[ "$(shared_release)" == "$SHARED_ROOT/releases/$EXPECTED_TARGET" ]]; then
        release_phase_deployed || \
            die "shared release is $EXPECTED_TARGET but the board is still $(board_build_id); this host is mid-recovery and this artifact cannot resume from there"
        verify_privileged_phase_state
        cutover_complete=1
        note "already recovered onto $EXPECTED_TARGET; nothing was changed"
        report_state
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
    # The upgrade records the release phase `ready` and prints its sequence; it
    # does not perform it, and nobody else is going to. Everything after this is
    # what makes the tenant usable rather than merely upgraded (SYRD-89 review).
    run_release_phase
    restart_project_account_roles
    verify_privileged_phase_state
    cutover_complete=1
    # Named for what it is. This run installs a shared release and stages the
    # tenant's tooling from it; it does not deploy the board, so reporting a
    # board build here would be reporting the one that was already running
    # (SYRD-89).
    note "complete; shared release $EXPECTED_TARGET, board $(board_build_id), roles registered under $(project_account)"
    report_state
}

trap 'on_exit $?' EXIT
main "$@"
