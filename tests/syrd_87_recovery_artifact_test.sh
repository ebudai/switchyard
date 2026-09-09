#!/usr/bin/env bash
set -euo pipefail

# SYRD-87. The recovery artifact cannot be executed to prove anything about it:
# running it is the cutover. What can be proved without running it is the shape
# of its mutation surface and the behaviour of the checks it will make, so both
# are pinned here against the shipped bytes.
#
# The definitions are sourced from the artifact rather than copied, and its
# trailing trap and main invocation are excluded, so this regression can never
# start a cutover.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT="$REPO_ROOT/deploy/SYRD-87-recover-syrd-runtime.sh"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

source <(sed '/^trap /,$d' "$ARTIFACT")

test_root="$(mktemp -d /tmp/syrd-87-artifact-test.XXXXXX)"
trap 'rm -rf -- "$test_root"' EXIT

code_of() {
    # The body of one function, comments stripped: prose about what a phase does
    # not do is not that phase doing it.
    #
    # Heredoc-aware, because several of these functions embed Python whose dict
    # literals close on a column-0 `}`. Stopping at the first of those silently
    # truncated the body and made every assertion below it vacuous -- a test
    # that reads half a function reports on half a function (SYRD-89).
    awk -v f="^$1\\\\(\\\\) \\\\{" '
        $0 ~ f { p = 1 }
        !p { next }
        { print }
        !h && match($0, /<<'"'"'[A-Za-z_][A-Za-z0-9_]*'"'"'/) {
            h = substr($0, RSTART + 3, RLENGTH - 4)
            next
        }
        h && $0 == h { h = ""; next }
        !h && /^}$/ { exit }
    ' "$ARTIFACT" | grep -v '^[[:space:]]*#'
}

# --- the pins are the ones this ticket authorises ----------------------------

[[ "$EXPECTED_TARGET" == "9a4d0a648c839fa5f8945342edefcd49ab553407" ]] || \
    fail "artifact targets $EXPECTED_TARGET, not the integrated main this ticket pins"
[[ "$EXPECTED_TREE" == "e5a685c51e4da4dd816d651a69d6467c9ba823a4" ]] || \
    fail "artifact pins tree $EXPECTED_TREE"
[[ "$PROJECT" == "syrd" ]] || fail "artifact is not scoped to syrd"

# --- the mutation surface ----------------------------------------------------

main_body="$(code_of main)"
[[ -n "$main_body" ]] || fail "could not read main() from the artifact"

# Everything before the mutation marker must be a read or a refusal. The dry run
# is deliberately not among them: the migration contract refuses while legacy
# panes are live, in dry run exactly as in earnest, so checkpointing has to come
# first and the dry run is the gate in front of what follows it (SYRD-89).
before="${main_body%%mutation_started=1*}"
[[ "$before" != "$main_body" ]] || fail "main() never sets mutation_started"
# -w throughout: run_upgrade is a substring of dry_run_upgrade, and matching it
# loosely would report the preflight as a mutation.
for forbidden in checkpoint_roles install_shared_release run_upgrade dry_run_upgrade recover_previous_state; do
    if grep -qw "$forbidden" <<<"$before"; then
        fail "$forbidden runs before mutation_started=1"
    fi
done
for required in verify_artifact_trust require_commands refuse_role_pane verify_tenant_ownership \
                verify_partial_state verify_exact_source prepare_source_checkout; do
    grep -qw "$required" <<<"$before" || fail "$required does not run before the first mutation"
done

# Ordering inside the mutating phase: checkpoints, then the dry run that is only
# meaningful once they are taken, then the parts the dry run gates.
after="${main_body#*mutation_started=1}"
order=""
for phase in checkpoint_roles dry_run_upgrade install_shared_release run_upgrade verify_privileged_phase_state; do
    grep -qw "$phase" <<<"$after" || fail "$phase does not run after the first mutation"
    order+="$(grep -nw "$phase" <<<"$after" | head -1 | cut -d: -f1) $phase"$'\n'
done
sorted_order="$(sort -n <<<"$order" | awk 'NF')"
[[ "$(awk '{print $2}' <<<"$sorted_order" | tr '\n' ' ')" == \
   "checkpoint_roles dry_run_upgrade install_shared_release run_upgrade verify_privileged_phase_state " ]] || \
    fail "the mutating phases are out of order:"$'\n'"$sorted_order"

# The already-recovered retry changes nothing at all.
retry_branch="$(awk '/if \[\[ "\$\(shared_release\)" == "\$SHARED_ROOT\/releases\/\$EXPECTED_TARGET" \]\]/,/^    fi$/' "$ARTIFACT" | grep -v '^[[:space:]]*#')"
[[ -n "$retry_branch" ]] || fail "could not find the already-recovered branch"
grep -q 'verify_privileged_phase_state' <<<"$retry_branch" || \
    fail "the already-recovered branch must still prove the state it is responsible for"
for forbidden in checkpoint_roles install_shared_release run_upgrade prepare_source_checkout mutation_started; do
    if grep -qw "$forbidden" <<<"$retry_branch"; then
        fail "the already-recovered retry must not reach $forbidden"
    fi
done

# --- the dry run must exercise the candidate, not the release it replaces -----

dry_run_body="$(code_of dry_run_upgrade)"
grep -q 'source_dir/scripts/switchyard' <<<"$dry_run_body" || \
    fail "the dry run must use the candidate entry point"
if grep -q 'SWITCHYARD_BIN' <<<"$dry_run_body"; then
    fail "the dry run must not use the installed trampoline it is about to replace"
fi
grep -q -- '--dry-run' <<<"$dry_run_body" || fail "the dry run must pass --dry-run"

# Every upgrade invocation carries all three pins; the 249d1f1a rollout failed
# because a handoff lost them.
pinned_body="$(code_of switchyard_pinned)"
for flag in --source-repo --commit-git-dir --deploy-ref; do
    grep -q -- "$flag" <<<"$pinned_body" || fail "pinned invocations must carry $flag"
done
# The invocation form is `upgrade "$PROJECT"`; the director instruction printed
# for the operator is prose and deliberately not one of these.
while read -r line; do
    [[ -z "$line" ]] && continue
    grep -q 'switchyard_pinned' <<<"$line" || \
        fail "an upgrade is invoked without the pinned wrapper: $line"
done < <(grep -n 'upgrade "\$PROJECT"' "$ARTIFACT" | grep -v '^[0-9]*:[[:space:]]*#' || true)

# --- recovery is honest ------------------------------------------------------

recovery_body="$(code_of recover_previous_state)"
grep -q "ln -sfn" <<<"$recovery_body" || fail "recovery must restore the previous shared release"
grep -q 'verify_foreign_tenants_unchanged' <<<"$recovery_body" || \
    fail "recovery must still prove no other tenant moved"
# It must not silently claim to have restarted the roles it stopped.
if grep -qE '"\$SWITCHYARD_BIN" "\$PROJECT"|switchyard_pinned .* "\$PROJECT"$' <<<"$recovery_body"; then
    fail "recovery must not relaunch the desktop presentation from a root trap handler"
fi
grep -q 'not restarted' <<<"$recovery_body" || \
    fail "recovery must say plainly that the roles are left stopped"

# --- the checks themselves ---------------------------------------------------

stub_bin="$test_root/bin"
mkdir -p "$stub_bin"
cat >"$stub_bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
url="${!#}"
case "$url" in
    */api/board) cat "$SYRD87_BOARD_BODY" ;;
    */api/runtime-assignments) cat "$SYRD87_RUNTIME_BODY" ;;
    *) echo "stub curl: unexpected URL: $url" >&2; exit 22 ;;
esac
EOF
chmod +x "$stub_bin/curl"
export PATH="$stub_bin:$PATH"
export SYRD87_BOARD_BODY="$test_root/board.json"
export SYRD87_RUNTIME_BODY="$test_root/runtime.json"

expect_fail() {
    local what="$1"
    shift
    if "$@" 2>/dev/null; then
        fail "$what should have been rejected"
    fi
}

echo "{\"build_id\": \"$EXPECTED_TARGET\"}" >"$SYRD87_BOARD_BODY"
cat >"$SYRD87_RUNTIME_BODY" <<EOF
{"project": "syrd", "authority_mode": "process", "assignments": {
  "director": {"target": "syrd-director:0.0"}, "main": {"target": "syrd-main:0.0"},
  "app": {"target": "syrd-app:0.0"}, "ops": {"target": "syrd-ops:0.0"},
  "audit": {"target": "syrd-audit:0.0"}, "inspector": {"target": "syrd-inspector:0.0"}}}
EOF
verify_board_process_authority || fail "the completed cutover shape should have passed"

# The interim state SYRD-85 deliberately left behind is not a completed cutover.
echo '{"project": "syrd", "authority_mode": "legacy_uid", "assignments": {}}' >"$SYRD87_RUNTIME_BODY"
expect_fail "legacy_uid after the cutover" verify_board_process_authority

cat >"$SYRD87_RUNTIME_BODY" <<'EOF'
{"project": "syrd", "authority_mode": "process", "assignments": {"director": {"target": "syrd-director:0.0"}}}
EOF
expect_fail "process authority with roles missing from the table" verify_board_process_authority

echo "{\"build_id\": \"$EXPECTED_BOARD_PREVIOUS\"}" >"$SYRD87_BOARD_BODY"
expect_fail "the previous board build" verify_board_process_authority

# The atomic flip that decides whether the roles share the project account.
config="$test_root/syrd.json"
write_config() {
    python3 - "$config" "$1" "$2" <<'PY'
import json
import sys

path, isolation, bound = sys.argv[1], sys.argv[2] == "true", sys.argv[3]
roles = []
for name in ("director", "main", "app", "ops", "audit", "inspector"):
    role = {"role": name}
    if bound == name:
        role["run_as_user"] = f"syrd-{name}"
    roles.append(role)
json.dump({"run_as_user": "switchyard-agent", "role_state_isolation": isolation, "roles": roles}, open(path, "w"))
PY
}
write_config true ""
verify_project_account_runtime "$config" >/dev/null || fail "a repatriated config should have passed"
write_config false ""
expect_fail "a config that never flipped role_state_isolation" verify_project_account_runtime "$config"
write_config true "app"
expect_fail "a role still bound to a dedicated account" verify_project_account_runtime "$config"

# --- the refusal the first operator run actually met (SYRD-89) ---------------
#
# A stand-in for the project-account migration that behaves the way the shipped
# one does: it probes for live legacy panes and refuses before it looks at the
# dry-run flag, so a dry run taken while the roles are up reports only that the
# roles are up. Everything here is hermetic -- no real tmux, sudo, account or
# board -- and the artifact's own functions are the ones under test.

migration_root="$test_root/migration"
mkdir -p "$migration_root/scripts"
live_roles="$test_root/live-roles"
printf '%s\n' director main app ops audit >"$live_roles"

cat >"$migration_root/scripts/switchyard" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
command="${1:-}"
case "$command" in
    stop)
        : >"$SYRD89_LIVE_ROLES"
        echo "switchyard: stopped every role at a resumable checkpoint"
        ;;
    upgrade)
        # The shipped contract: the live-pane probe comes first and does not
        # care whether --dry-run was passed.
        if [[ -s "$SYRD89_LIVE_ROLES" ]]; then
            echo "switchyard: refusing porter's project-account migration:"
            while read -r role; do
                [[ -n "$role" ]] || continue
                echo "  $role: syrd-$role is still live as syrd-$role; stop it at a resumable checkpoint before repatriation"
            done <"$SYRD89_LIVE_ROLES"
            echo "switchyard: no account, worktree, installed unit, or release was changed; resume after every named role is safely checkpointed"
            exit 1
        fi
        echo "switchyard: would repatriate syrd's resumable role state and remove dedicated-account bindings"
        ;;
    *)
        echo "fake switchyard: unexpected command: $command" >&2
        exit 64
        ;;
esac
EOF
chmod +x "$migration_root/scripts/switchyard"

cat >"$stub_bin/sudo" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
# `sudo -u <account> [-H] <command...>` -- run the command, drop the identity.
args=("$@")
index=0
account=""
while (( index < ${#args[@]} )); do
    case "${args[index]}" in
        -u) account="${args[index + 1]}"; index=$((index + 2)) ;;
        -H) index=$((index + 1)) ;;
        *) break ;;
    esac
done
# Which account is asked is the whole question when discovery must follow the
# configuration rather than guess a name (SYRD-89 audit), so carry it through.
if [[ -n "$account" && "$account" == "$(id -un)" ]]; then
    echo "sudo: user $account is not allowed to execute as $account" >&2
    exit 1
fi
SYRD89_ASKED_AS="$account" exec "${args[@]:index}"
EOF
chmod +x "$stub_bin/sudo"

cat >"$stub_bin/tmux" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "has-session" ]]; then
    session="${3:-}"
    # Exact-name selection: the artifact addresses sessions as =<name> so a
    # prefix cannot match a longer session belonging to another role.
    session="${session#=}"
    grep -qx "${session#syrd-}" "$SYRD89_LIVE_ROLES" 2>/dev/null || exit 1
    # A session lives in one account's server. Asking the wrong account finds
    # nothing -- which is what happens on a host whose roles moved to the
    # project account while a check still guesses syrd-<role>.
    if [[ -n "${SYRD89_SESSION_ACCOUNT:-}" ]]; then
        asked="${SYRD89_ASKED_AS:-$(id -un)}"
        [[ "$asked" == "$SYRD89_SESSION_ACCOUNT" ]] || exit 1
    fi
    exit 0
fi
exit 0
EOF
chmod +x "$stub_bin/tmux"

cat >"$stub_bin/id" <<'EOF'
#!/usr/bin/env bash
# -u answers as root and -un names root, so account_tmux takes its sudo path
# exactly as it would on a host. Which accounts exist is a fixture variable:
# a tenant can have retired role accounts removed while the project account
# remains, and a check that guesses the retired name then skips the role
# entirely instead of reporting it (SYRD-89 audit).
[[ "${1:-}" == "-u" ]] && { echo 0; exit 0; }
[[ "${1:-}" == "-un" ]] && { echo root; exit 0; }
if [[ -n "${SYRD89_EXISTING_ACCOUNTS:-}" ]]; then
    grep -qx "${1:-}" <<<"$SYRD89_EXISTING_ACCOUNTS" || exit 1
fi
exit 0
EOF
chmod +x "$stub_bin/id"

export SYRD89_LIVE_ROLES="$live_roles"
source_dir="$migration_root"

# 1. The observed failure, reproduced: a dry run taken while the roles are live
#    refuses and reports the roles, and nothing about the upgrade is learned.
refusal="$( (dry_run_upgrade) 2>&1 || true )"
grep -q 'still live as' <<<"$refusal" || \
    fail "the live-role dry run should have reproduced the migration refusal, got: $refusal"
grep -q 'no release, unit or identity was changed' <<<"$refusal" || \
    fail "the refusal must say plainly that nothing was changed, got: $refusal"
[[ -s "$live_roles" ]] || fail "a refused dry run must not have stopped anything"

# 2. The correction: checkpoint first, and the same dry run then means something.
(checkpoint_roles) >/dev/null 2>&1 || fail "checkpoint_roles should have stopped the fixture roles"
[[ ! -s "$live_roles" ]] || fail "checkpoint_roles left roles live"
progressed="$( (dry_run_upgrade) 2>&1 )" || \
    fail "the dry run should pass once the roles are checkpointed, got: $progressed"
grep -q 'would repatriate' <<<"$progressed" || \
    fail "the checkpointed dry run should reach the repatriation plan, got: $progressed"

# 3. The production refusal this reordering must not weaken is still in the
#    release, unchanged: this ticket corrects the artifact, never the contract.
grep -q 'stop it at a resumable checkpoint before repatriation' \
    "$REPO_ROOT/scripts/team_launcher.py" || \
    fail "the production live-session refusal is missing from the release"
python3 - "$REPO_ROOT/scripts/team_launcher.py" <<'PY'
import re
import sys

source = open(sys.argv[1], encoding="utf-8").read()
body = source.split("def repatriate_role_runtime_state", 1)[1].split("\ndef ", 1)[0]
probe = body.index("stop it at a resumable checkpoint before repatriation")
refusal = body.index("if problems:")
honours_dry_run = body.index("if dry_run:")
if not probe < refusal < honours_dry_run:
    raise SystemExit(
        "the release no longer refuses live panes before it honours --dry-run; "
        "the artifact ordering this test pins was chosen for that contract"
    )
PY

# --- discovery must follow the configuration, not the account name -----------
#
# The two reviewed entry states differ exactly here. Before the cutover each role
# has its own account; after it, every role runs as the project account -- and
# those retired accounts still exist, so `id` succeeds for both and any check
# that guesses the account name sees nothing. That is today's exact state: six
# live sessions under switchyard-agent, which the previous revision reported as
# none live and none stopped (SYRD-89 audit).

discovery_root="$test_root/discovery"
mkdir -p "$discovery_root"

write_role_config() {
    # $1 destination, $2 project account, $3 per-role account or "" for none
    python3 - "$1" "$2" "${3:-}" <<'PYD'
import json
import sys

path, project_account, role_account = sys.argv[1], sys.argv[2], sys.argv[3]
roles = []
for role in ("director", "main", "app", "ops", "audit", "inspector"):
    entry = {"role": role, "tmux_session": f"syrd-{role}", "target": f"syrd-{role}:0.0"}
    if role_account:
        entry["run_as_user"] = f"syrd-{role}"
    roles.append(entry)
json.dump({"project": "syrd", "run_as_user": project_account, "roles": roles}, open(path, "w"))
PYD
}


# The resumed state: every role under the project account.
write_role_config "$discovery_root/project.json" switchyard-agent ""
project_cfg="$discovery_root/project.json"
printf '%s
' director main app ops audit inspector >"$live_roles"
export SYRD89_SESSION_ACCOUNT=switchyard-agent
found="$(live_role_sessions "$project_cfg")"
[[ "$(grep -c . <<<"$found")" == "6" ]] ||     fail "project-account sessions must all be discovered, got:"$'
'"$found"
grep -qx 'syrd-director' <<<"$found" ||     fail "the director's project-account session must be discovered, got:"$'
'"$found"
[[ "$(role_session_account director "$project_cfg")" == "switchyard-agent" ]] ||     fail "the resumed state resolves every role to the project account"

# And what recovery would report from that state: all six checkpointed.
roles_live_at_entry="$found"
: >"$live_roles"
[[ "$(checkpointed_role_sessions | grep -c .)" == "6" ]] ||     fail "stopping six project-account sessions must be reported as six checkpoints"

# What recovery reports when only some are still up: the rest by session name.
roles_live_at_entry="$(printf '%s\n' syrd-director syrd-main)"
absent_now="$(absent_role_sessions_at_entry)"
[[ "$(grep -c . <<<"$absent_now")" == "4" ]] || \
    fail "the four roles absent at entry must be named, got:"$'\n'"$absent_now"
grep -q 'syrd-ops' <<<"$absent_now" || \
    fail "absent roles must be reported by session name under the project account"
roles_live_at_entry=""

# A tenant whose retired role accounts were removed still has six roles, and
# every one of them must be reported absent rather than silently skipped by a
# check looking for an account that is gone.
export SYRD89_EXISTING_ACCOUNTS="switchyard-agent"
roles_live_at_entry=""
absent_pruned="$(absent_role_sessions_at_entry)"
[[ "$(grep -c . <<<"$absent_pruned")" == "6" ]] || \
    fail "all six roles must be reported absent when the retired accounts are gone, got:"$'\n'"$absent_pruned"
unset SYRD89_EXISTING_ACCOUNTS

# Discovery must work when the caller already is the account that owns the
# sessions: sudo to yourself needs a grant a tenant has no reason to hold, and
# is refused where it is absent -- observed on this host.
self="$(id -un)"
write_role_config "$discovery_root/self.json" "$self" ""
export SYRD89_SESSION_ACCOUNT="$self"
printf '%s\n' director main app ops audit inspector >"$live_roles"
[[ "$(live_role_sessions "$discovery_root/self.json" | grep -c .)" == "6" ]] || \
    fail "sessions owned by the calling account must be discovered without a sudo hop"
export SYRD89_SESSION_ACCOUNT=switchyard-agent

# The entry state: each role under its own account, discovered the same way.
write_role_config "$discovery_root/perrole.json" switchyard-agent per-role
perrole_cfg="$discovery_root/perrole.json"
printf '%s
' director main app ops audit inspector >"$live_roles"
# The entry state: each session lives in its own role account's server.
unset SYRD89_SESSION_ACCOUNT
[[ "$(live_role_sessions "$perrole_cfg" | grep -c .)" == "6" ]] ||     fail "per-role-account sessions must still be discovered"
[[ "$(role_session_account director "$perrole_cfg")" == "syrd-director" ]] ||     fail "the entry state resolves each role to its own account"

roles_live_at_entry=""

unset SYRD89_SESSION_ACCOUNT

# --- a stop that checkpoints some roles and then fails (SYRD-89) -------------
#
# The exit code is not the signal. A stop that gets part-way through leaves
# sessions down that recovery has to account for, and reading roles_stopped off
# the return value made recovery silent about exactly that case.

cat >"$migration_root/scripts/switchyard" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
    stop)
        # Checkpoints the first two roles, then fails the way a partial stop does.
        remaining="$(tail -n +3 "$SYRD89_LIVE_ROLES")"
        printf '%s\n' "$remaining" | grep -v '^$' >"$SYRD89_LIVE_ROLES" || :
        echo "switchyard: stopped director, main" >&2
        echo "switchyard: could not reach app's session" >&2
        exit 1
        ;;
    upgrade) echo "switchyard: would repatriate"; ;;
    *) exit 64 ;;
esac
EOF
chmod +x "$migration_root/scripts/switchyard"
printf '%s\n' director main app ops audit >"$live_roles"
roles_stopped=0

partial="$( ( trap 'echo "ROLES_STOPPED=$roles_stopped"' EXIT; checkpoint_roles ) 2>&1 || true )"
grep -q 'ROLES_STOPPED=1' <<<"$partial" || \
    fail "a partial stop must be recorded as a checkpoint, got: $partial"
grep -q 'after checkpointing some roles' <<<"$partial" || \
    fail "a partial stop must say so rather than claiming nothing was checkpointed, got: $partial"
grep -qx 'app' "$live_roles" || fail "the fixture should still have app live after a partial stop"

# A stop that fails having checkpointed nothing is a different report, and must
# not claim a checkpoint that never happened.
printf '%s\n' director main app ops audit >"$live_roles"
cat >"$migration_root/scripts/switchyard" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
case "${1:-}" in
    stop) echo "switchyard: could not reach any session" >&2; exit 1 ;;
    upgrade) echo "switchyard: would repatriate"; ;;
    *) exit 64 ;;
esac
EOF
chmod +x "$migration_root/scripts/switchyard"
roles_stopped=0
roles_live_at_entry="$(live_role_sessions)"
none="$( ( trap 'echo "ROLES_STOPPED=$roles_stopped"' EXIT; checkpoint_roles ) 2>&1 || true )"
grep -q 'ROLES_STOPPED=0' <<<"$none" || \
    fail "a stop that checkpointed nothing must not be recorded as a checkpoint, got: $none"
grep -q 'checkpointed nothing' <<<"$none" || \
    fail "a stop that checkpointed nothing must say so, got: $none"

# Recovery names the sessions this run actually stopped, so a partial checkpoint
# is not reported as if it were all or nothing.
printf '%s\n' director main app ops audit >"$live_roles"
roles_live_at_entry="$(live_role_sessions)"
printf '%s\n' app ops audit >"$live_roles"
down="$(checkpointed_role_sessions)"
grep -q 'syrd-director' <<<"$down" || fail "checkpointed_role_sessions must name a stopped role"
grep -q 'syrd-main' <<<"$down" || fail "checkpointed_role_sessions must name every stopped role"
if grep -q 'syrd-app' <<<"$down"; then
    fail "checkpointed_role_sessions must not name a role that is still live"
fi

# The host this artifact was written for: five roles live at entry, Main already
# absent. Crediting this run with stopping Main would claim a resumable
# checkpoint it never took, so the pre-stop set has to survive (SYRD-89).
printf '%s\n' director app ops audit inspector >"$live_roles"
roles_live_at_entry="$(live_role_sessions)"
if grep -q 'syrd-main' <<<"$roles_live_at_entry"; then
    fail "the fixture should have Main absent at entry"
fi
printf '%s\n' app ops audit inspector >"$live_roles"
down="$(checkpointed_role_sessions)"
grep -q 'syrd-director' <<<"$down" || \
    fail "the role this run stopped must be reported as checkpointed, got: [$down]"
if grep -q 'syrd-main' <<<"$down"; then
    fail "a role absent before this run started must not be reported as checkpointed, got: [$down]"
fi
absent="$(absent_role_sessions_at_entry)"
grep -q 'syrd-main' <<<"$absent" || \
    fail "a role absent at entry must be reported separately as such, got: [$absent]"
if grep -q 'syrd-director' <<<"$absent"; then
    fail "a role live at entry must not be reported as already absent, got: [$absent]"
fi

recovery_body="$(code_of recover_previous_state)"
grep -q 'checkpointed_role_sessions' <<<"$recovery_body" || \
    fail "recovery must report the sessions this run stopped, not just a flag"
grep -q 'absent_role_sessions_at_entry' <<<"$recovery_body" || \
    fail "recovery must separate roles that were already absent at entry"

# --- SYRD-89: the real upgrade is sourced from a tree that names a release ----
#
# The staging contract removes the staged marker when the source carries none,
# so that a bundle can never be stamped with a release it did not come from.
# A git checkout carries none. Pinning the post-install upgrade to the temporary
# checkout therefore left the roles' tooling unprovenanced and this artifact
# demanding a marker its own choice of source had removed.

run_upgrade_body="$(code_of run_upgrade)"
[[ -n "$run_upgrade_body" ]] || fail "could not read run_upgrade() from the artifact"
grep -q 'TARGET_RELEASE_ROOT' <<<"$run_upgrade_body" || \
    fail "the post-install upgrade must be sourced from the installed release, not the checkout"
if grep -qw 'source_dir' <<<"$run_upgrade_body"; then
    fail "the post-install upgrade must not be sourced from the temporary checkout"
fi
grep -q -- '-f "\$TARGET_RELEASE_ROOT/.switchyard-release.json"' <<<"$run_upgrade_body" || \
    fail "run_upgrade must test for the marker, not merely mention it in a message"

# The installed release is the one this artifact just put in place, named by the
# same commit it pins, so the two cannot drift apart.
[[ "$TARGET_RELEASE_ROOT" == "$SHARED_ROOT/releases/$EXPECTED_TARGET" ]] || \
    fail "TARGET_RELEASE_ROOT is $TARGET_RELEASE_ROOT, not the release this artifact installs"

# The dry run keeps the candidate checkout: it has to exercise the tree before
# it is installed, so the two invocations are pinned to different trees on
# purpose and each must keep its own.
dry_body="$(code_of dry_run_upgrade)"
grep -qw 'source_dir' <<<"$dry_body" || \
    fail "the dry run must still exercise the candidate checkout"
if grep -q 'TARGET_RELEASE_ROOT' <<<"$dry_body"; then
    fail "the dry run must not be sourced from a release that is not installed yet"
fi

# Both invocations pass their source explicitly, so neither can silently inherit
# the other's.
pinned_body="$(code_of switchyard_pinned)"
grep -q 'local source=' <<<"$pinned_body" || \
    fail "switchyard_pinned must take its source as a parameter"
if grep -qw 'source_dir' <<<"$pinned_body"; then
    fail "switchyard_pinned must not close over the temporary checkout"
fi

# --- SYRD-89: the artifact claims only the phases it performs ----------------
#
# `switchyard upgrade` reports the release deployment sequence and records the
# phase `ready`; it does not deploy. Verifying a deployed board after it is
# claiming a phase nobody ran.

privileged_body="$(code_of verify_privileged_phase_state)"
[[ -n "$privileged_body" ]] || fail "could not read verify_privileged_phase_state()"
for owed in verify_board_process_authority verify_release_phase_state; do
    if grep -qw "$owed" <<<"$privileged_body"; then
        fail "$owed depends on the operator release phase and must not gate this artifact"
    fi
done
grep -qw 'verify_upgrade_phase_journal' <<<"$privileged_body" || \
    fail "the privileged phase must prove the journal state it actually leaves"

pointers_body="$(code_of verify_release_pointers)"
if grep -qw 'board_release' <<<"$pointers_body"; then
    fail "verify_release_pointers must not assert a board release this artifact does not deploy"
fi
grep -q 'TENANT_RELEASE_MARKER' <<<"$pointers_body" || \
    fail "verify_release_pointers must still prove the staged tooling's provenance"

release_phase_body="$(code_of verify_release_phase_state)"
grep -qw 'verify_board_process_authority' <<<"$release_phase_body" || \
    fail "process-bound authority belongs to the release phase check"

# --- SYRD-89 review: one invocation must reach a usable tenant ---------------
#
# Stopping at release=ready leaves the roles running as the project account
# while the board still authorises the retired per-role uids, so every write is
# refused and the director cannot perform finish-upgrade. That is an outage, not
# a phase boundary, and this artifact is the one root command authorised to end
# it.

main_after="${main_body#*mutation_started=1}"
for required in run_release_phase restart_project_account_roles; do
    grep -qw "$required" <<<"$main_after" || \
        fail "$required must run: one invocation has to leave a tenant that can be written to"
done
release_order=""
for phase in run_upgrade run_release_phase restart_project_account_roles verify_privileged_phase_state; do
    release_order+="$(grep -nw "$phase" <<<"$main_after" | head -1 | cut -d: -f1) $phase"$'\n'
done
[[ "$(sort -n <<<"$release_order" | awk 'NF {print $2}' | tr '\n' ' ')" == \
   "run_upgrade run_release_phase restart_project_account_roles verify_privileged_phase_state " ]] || \
    fail "the release phase must follow the upgrade and precede the roles that register against it"

# The follow-up handed to a human must be executable, not a command that only
# reprints instructions. `switchyard upgrade` prints the sequence and records
# `ready`; naming it as the way to deploy is naming a dead end.
report_body="$(code_of report_remaining_phases)"
grep -q 'finish-upgrade' <<<"$report_body" || fail "the report must name the director phase"
if grep -q 'SWITCHYARD_BIN upgrade' <<<"$report_body"; then
    fail "the report must not send an operator back to a command that only prints the sequence"
fi

# The release phase is the release's own rendering, not a copy that can drift.
render_body="$(code_of render_release_phase_commands)"
[[ -n "$render_body" ]] || fail "could not read render_release_phase_commands()"
for renderer in tenant_release_listener_command tenant_release_unit_install_command tenant_release_deploy_command; do
    grep -q "$renderer" <<<"$render_body" || \
        fail "the release phase must use the release's own $renderer, not a hand-written copy"
done
grep -q 'TARGET_RELEASE_ROOT' <<<"$render_body" || \
    fail "the release phase must be rendered by the release being deployed"

run_release_body="$(code_of run_release_phase)"
grep -qw 'render_release_phase_commands' <<<"$run_release_body" || \
    fail "run_release_phase must execute what the renderer produced"
grep -q 'die "release phase step failed' <<<"$run_release_body" || \
    fail "a failed release step must stop the run rather than continue past it"

# The listener comes down before the migrations and back up after them; that
# order is the renderer's and the artifact must not resequence it.
grep -q '"stop")' <<<"$render_body" || fail "the listener must be stopped for the migrations"
stop_at="$(grep -n 'listener_command(status, project, "stop")' <<<"$render_body" | head -1 | cut -d: -f1)"
deploy_at="$(grep -n 'tenant_release_deploy_command' <<<"$render_body" | head -1 | cut -d: -f1)"
start_at="$(grep -n 'listener_command(status, project, "start")' <<<"$render_body" | head -1 | cut -d: -f1)"
[[ -n "$stop_at" && -n "$deploy_at" && -n "$start_at" ]] || fail "could not read the release phase order"
(( stop_at < deploy_at && deploy_at < start_at )) || \
    fail "the listener must stop before the deploy and start after it"

# --- SYRD-89 review: every terminal state has a Director write path ----------

state_report="$(code_of report_state)"
grep -qw 'verify_director_write_path' <<<"$state_report" || \
    fail "a successful run must prove the director can actually write before reporting success"
grep -qw 'verify_release_phase_state' <<<"$state_report" || \
    fail "a successful run must prove the board actually moved"

write_path_body="$(code_of verify_director_write_path)"
grep -q 'authority_mode' <<<"$write_path_body" || \
    fail "the write path check must read the board's authority mode"
grep -q '"director" not in assignments' <<<"$write_path_body" || \
    fail "the write path check must test for the director in the assignments, not merely mention it"

# Recovery: a rolled-back tenant whose config and board disagree authorises
# nobody, and must be reported critical rather than recovered.
recovery_body="$(code_of recover_previous_state)"
grep -qw 'board_write_path_problem' <<<"$recovery_body" || \
    fail "recovery must decide whether anything can still write, not just whether the service is up"
grep -q 'CRITICAL' <<<"$recovery_body" || \
    fail "a recovered tenant with no write path must be reported critical"
grep -qw 'release_phase_deployed' <<<"$recovery_body" || \
    fail "recovery must notice a board it cannot move back"

# --- driven, not read ------------------------------------------------------
#
# The previous revision claimed these were driven and only grepped their source.
# That is how a release renderer that cannot import its own release, and a
# release phase that never records itself, both passed review: reading a
# function proves the letters in it, not that it runs (SYRD-89 audit).

# The renderer, imported the way the artifact will import it, from a directory
# that holds no scripts/ package of its own. The earlier version passed only
# because the working directory happened to supply one.
render_probe="$test_root/render"
mkdir -p "$render_probe"
if [[ -d "$TARGET_RELEASE_ROOT" ]]; then
    rendered_live="$(cd "$render_probe" && render_release_phase_commands 2>&1)" || \
        fail "render_release_phase_commands failed from a neutral directory:"$'\n'"$rendered_live"
    [[ "$rendered_live" != *"ModuleNotFoundError"* ]] || \
        fail "the renderer cannot import its own release:"$'\n'"$rendered_live"
    # Four steps, in the order the listener contract requires.
    labels="$(printf '%s\n' "$rendered_live" | python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if line:
        print(json.loads(line)["label"])
')" || fail "the renderer did not emit parseable steps:"$'\n'"$rendered_live"
    [[ "$(printf '%s\n' "$labels" | wc -l)" == "4" ]] || \
        fail "the release phase must be four steps, got:"$'\n'"$labels"
    [[ "$(printf '%s\n' "$labels" | head -1)" == *"stop the notification listener"* ]] || \
        fail "the listener must be stopped first, got:"$'\n'"$labels"
    [[ "$(printf '%s\n' "$labels" | tail -1)" == *"start the notification listener"* ]] || \
        fail "the listener must be started last, got:"$'\n'"$labels"
    printf '%s\n' "$labels" | sed -n '2p' | grep -q 'units' || \
        fail "the units must be installed before the deploy, got:"$'\n'"$labels"
    printf '%s\n' "$labels" | sed -n '3p' | grep -q 'deploy' || \
        fail "the deploy must follow the unit install, got:"$'\n'"$labels"
    # And the deploy must name the exact pinned release, from root's own mirror.
    deploy_cmd="$(printf '%s\n' "$rendered_live" | python3 -c '
import json, sys
for line in sys.stdin:
    line = line.strip()
    if line and "deploy" in json.loads(line)["label"]:
        print(json.loads(line)["command"])
')"
    grep -q "$EXPECTED_TARGET" <<<"$deploy_cmd" || \
        fail "the deploy step does not name the pinned release $EXPECTED_TARGET"
    grep -q '/etc/switchyard/provision/' <<<"$(printf '%s\n' "$rendered_live")" || \
        fail "the units must come from the root-owned mirror, not the tenant's directory"
else
    fail "the pinned release $TARGET_RELEASE_ROOT is not installed; the renderer cannot be driven"
fi

# The recorder must exist and go through the release's own recorder, and
# run_release_phase must actually call it -- a deploy that leaves the journal at
# `ready` makes verify_upgrade_phase_journal abort over its own bookkeeping.
record_body="$(code_of record_release_phase)"
[[ -n "$record_body" ]] || fail "could not read record_release_phase()"
grep -q 'tl.record_release_phase_from_status(' <<<"$record_body" || \
    fail "the release phase must be recorded by calling the release's own recorder"
grep -q 'raise SystemExit' <<<"$record_body" || \
    fail "a board that did not actually move must not be recorded as done"

# No foreign tenant may change, on the success path as on the recovery path.
# It is the claim this rollout makes about every other tenant on the host.
grep -qw 'verify_foreign_tenants_unchanged' <<<"$privileged_body" || \
    fail "a successful run must prove it changed no other tenant"
grep -qw 'verify_foreign_tenants_unchanged' <<<"$recovery_body" || \
    fail "recovery must prove it changed no other tenant"
grep -qw 'record_release_phase' <<<"$run_release_body" || \
    fail "run_release_phase must record the phase it just performed"
# It must import the release the same correct way.
grep -q 'sys.path.insert(0, release_root)' <<<"$record_body" || \
    fail "the recorder must import the release root, not its scripts directory"
grep -q 'sys.path.insert(0, release_root)' <<<"$render_body" || \
    fail "the renderer must import the release root, not its scripts directory"

# The diagnosis, driven against the live host. Today's exact state -- a
# project-account configuration under a board that authorises per-role uids --
# must be reported as no write path.
if diagnosis="$(board_write_path_problem)"; then
    board_mode="$(curl -fsS "$BOARD_URL/api/runtime-assignments" 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin).get("authority_mode",""))' 2>/dev/null || true)"
    [[ "$board_mode" == "process" ]] || \
        fail "the diagnosis called a legacy_uid board with a project-account config healthy"
else
    [[ -n "$diagnosis" ]] || fail "the diagnosis must say why there is no write path"
fi

# The already-recovered retry must not call a half-finished host complete.
grep -qw 'release_phase_deployed' <<<"$retry_branch" || \
    fail "a host with the shared release in place and the board behind is mid-recovery, not recovered"

# The journal proof pins the exact partial state, including release=ready.
journal_body="$(code_of verify_upgrade_phase_journal)"
grep -q '"release": "done"' <<<"$journal_body" || \
    fail "the journal proof must expect release=done: this artifact performs that phase"
grep -q '"director": "pending"' <<<"$journal_body" || \
    fail "the journal proof must expect director=pending"

# --- SYRD-89: both remaining phases are reported, not just the director's -----

# --- SYRD-89: the partial state this artifact resumes from -------------------

partial_body="$(code_of verify_partial_state)"
grep -qw 'upgrade_journal_state' <<<"$partial_body" || \
    fail "verify_partial_state must classify the journal rather than assume a state"
# The marker is checked against the state, not accepted either way: absent in the
# entry state means something else stripped it, and present in the resumed state
# means the staging contract did not do what it says.
grep -q 'state" == "entry"' <<<"$partial_body" || \
    fail "the marker expectation must follow from which state was found"
grep -q 'TENANT_RELEASE_MARKER' <<<"$partial_body" || \
    fail "verify_partial_state must still say something about the staged marker"

# --- the classifier itself, run against fixtures -----------------------------
#
# Sourcing gives us the real function; these drive it over journals rather than
# trusting that its text looks right.

# The artifact's own classifier, driven over fixtures. It is sourced above, so
# this is the function the operator run will execute -- not a restatement of it.
write_journal() {
    python3 - "$1" "$2" <<'PYJ'
import json
import sys

phases = json.loads(sys.argv[2])
json.dump({"phases": {k: {"state": v} for k, v in phases.items()}}, open(sys.argv[1], "w"))
PYJ
}

classify() {
    upgrade_journal_state "$1" 2>/dev/null || echo refused
}

# The exact state the first operator run left, which is the one that matters.
write_journal "$test_root/resumed.json" \
    '{"artifacts":"done","accounts":"done","identities":"done","release":"ready","director":"pending"}'
[[ "$(classify "$test_root/resumed.json")" == "resumed" ]] || \
    fail "the state the interrupted run left must classify as resumed"

write_journal "$test_root/entry.json" \
    '{"artifacts":"done","accounts":"done","identities":"rolled back","director":"pending"}'
[[ "$(classify "$test_root/entry.json")" == "entry" ]] || \
    fail "the reviewed baseline must classify as entry"

# Anything else is a host nobody reviewed. A journal claiming the release phase
# is already done is the dangerous one: it would let this artifact skip straight
# to verifying a deploy that never happened.
write_journal "$test_root/mid.json" \
    '{"artifacts":"done","accounts":"done","identities":"done","release":"done","director":"pending"}'
[[ "$(classify "$test_root/mid.json")" == "refused" ]] || \
    fail "a journal claiming the release phase is done must be refused"
write_journal "$test_root/rolled.json" \
    '{"artifacts":"done","accounts":"done","identities":"done","release":"ready","director":"done"}'
[[ "$(classify "$test_root/rolled.json")" == "refused" ]] || \
    fail "a journal claiming the director phase is done must be refused"
write_journal "$test_root/partial.json" '{"artifacts":"done"}'
[[ "$(classify "$test_root/partial.json")" == "refused" ]] || \
    fail "an incomplete journal must be refused"

echo "SYRD-87 recovery artifact contract regression passed"
