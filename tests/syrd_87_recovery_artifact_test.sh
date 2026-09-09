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
    awk -v f="^$1\\\\(\\\\) \\\\{" '$0 ~ f {p=1} p {print} p && /^}$/ {exit}' "$ARTIFACT" \
        | grep -v '^[[:space:]]*#'
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
for phase in checkpoint_roles dry_run_upgrade install_shared_release run_upgrade verify_target_state; do
    grep -qw "$phase" <<<"$after" || fail "$phase does not run after the first mutation"
    order+="$(grep -nw "$phase" <<<"$after" | head -1 | cut -d: -f1) $phase"$'\n'
done
sorted_order="$(sort -n <<<"$order" | awk 'NF')"
[[ "$(awk '{print $2}' <<<"$sorted_order" | tr '\n' ' ')" == \
   "checkpoint_roles dry_run_upgrade install_shared_release run_upgrade verify_target_state " ]] || \
    fail "the mutating phases are out of order:"$'\n'"$sorted_order"

# The already-recovered retry changes nothing at all.
retry_branch="$(awk '/if \[\[ "\$\(shared_release\)" == "\$SHARED_ROOT\/releases\/\$EXPECTED_TARGET" \]\]/,/^    fi$/' "$ARTIFACT" | grep -v '^[[:space:]]*#')"
[[ -n "$retry_branch" ]] || fail "could not find the already-recovered branch"
grep -q 'verify_target_state' <<<"$retry_branch" || \
    fail "the already-recovered branch must still prove the target state"
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
grep -q 'were not restarted' <<<"$recovery_body" || \
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
while (( index < ${#args[@]} )); do
    case "${args[index]}" in
        -u) index=$((index + 2)) ;;
        -H) index=$((index + 1)) ;;
        *) break ;;
    esac
done
exec "${args[@]:index}"
EOF
chmod +x "$stub_bin/sudo"

cat >"$stub_bin/tmux" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "has-session" ]]; then
    session="${3:-}"
    grep -qx "${session#syrd-}" "$SYRD89_LIVE_ROLES" 2>/dev/null && exit 0
    exit 1
fi
exit 0
EOF
chmod +x "$stub_bin/tmux"

cat >"$stub_bin/id" <<'EOF'
#!/usr/bin/env bash
# Every syrd-<role> account exists in this fixture; -u answers as root.
[[ "${1:-}" == "-u" ]] && { echo 0; exit 0; }
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

echo "SYRD-87 recovery artifact contract regression passed"
