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

# Everything before the mutation marker must be a read, a refusal, or a dry run.
before="${main_body%%mutation_started=1*}"
[[ "$before" != "$main_body" ]] || fail "main() never sets mutation_started"
# -w throughout: run_upgrade is a substring of dry_run_upgrade, and matching it
# loosely would report the preflight as a mutation.
for forbidden in checkpoint_roles install_shared_release run_upgrade recover_previous_state; do
    if grep -qw "$forbidden" <<<"$before"; then
        fail "$forbidden runs before mutation_started=1"
    fi
done
for required in verify_artifact_trust require_commands refuse_role_pane verify_tenant_ownership \
                verify_partial_state verify_exact_source prepare_source_checkout dry_run_upgrade; do
    grep -qw "$required" <<<"$before" || fail "$required does not run before the first mutation"
done

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

echo "SYRD-87 recovery artifact contract regression passed"
