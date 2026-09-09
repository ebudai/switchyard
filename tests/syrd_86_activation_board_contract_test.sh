#!/usr/bin/env bash
set -euo pipefail

# SYRD-86. The SYRD-85 artifact activated the target board correctly and then
# failed its own post-check, because it read the workflow roles from the wrong
# level of the API response. Two live contracts go unproven by every other
# check in that artifact, so they are pinned here against the shipped bytes:
#
#   /api/workflow            returns {revision, document}; roles are nested.
#   /api/runtime-assignments under legacy UID authority reports an EMPTY
#                            assignment map, which is correct, not a fault.
#
# The verifiers are sourced from the artifact itself rather than copied, so this
# regression fails if the shipped code drifts from what it proves.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT="$REPO_ROOT/deploy/SYRD-85-activate-audited-board.sh"

fail() {
    echo "FAIL: $*" >&2
    exit 1
}

# Definitions only. The trailing trap and main invocation are excluded so this
# regression can never start an activation.
source <(sed '/^trap /,$d' "$ARTIFACT")

test_root="$(mktemp -d /tmp/syrd-86-board-contract-test.XXXXXX)"
trap 'rm -rf -- "$test_root"' EXIT

# The board URL is readonly in the artifact, so responses are served by a stub
# curl that answers by API path. It also proves which endpoint each verifier
# asks for: an unexpected path is an error, not an empty body.
stub_bin="$test_root/bin"
mkdir -p "$stub_bin"
cat >"$stub_bin/curl" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

url="${!#}"
case "$url" in
    */api/workflow) cat "$SYRD86_WORKFLOW_BODY" ;;
    */api/runtime-assignments) cat "$SYRD86_RUNTIME_BODY" ;;
    *)
        echo "stub curl: unexpected URL: $url" >&2
        exit 22
        ;;
esac
EOF
chmod +x "$stub_bin/curl"
export PATH="$stub_bin:$PATH"
export SYRD86_WORKFLOW_BODY="$test_root/workflow.json"
export SYRD86_RUNTIME_BODY="$test_root/runtime.json"

# Recorded from the live board this artifact verifies, trimmed to the fields
# the verifier reads.
workflow_document() {
    local active="$1" capabilities="$2"
    cat <<EOF
{"revision": 19, "document": {"schema": "switchyard.workflow.v1", "project": "syrd", "roles": [
  {"name": "director", "label": "Director", "kind": "system", "active": $active, "capabilities": $capabilities},
  {"name": "ops", "label": "Ops", "kind": "implementer", "active": true, "capabilities": ["add_comment"]}
]}}
EOF
}

readonly FULL_CAPABILITIES='["create_ticket", "add_comment", "edit_fields", "set_blockers", "merge", "reassign"]'
readonly NO_REASSIGN_CAPABILITIES='["create_ticket", "add_comment", "edit_fields", "set_blockers", "merge"]'

expect_pass() {
    local what="$1"
    shift
    "$@" || fail "$what should have passed"
}

expect_fail() {
    local what="$1"
    shift
    if "$@" 2>/dev/null; then
        fail "$what should have been rejected"
    fi
}

# --- the nested workflow document -------------------------------------------

workflow_document true "$FULL_CAPABILITIES" >"$SYRD86_WORKFLOW_BODY"
expect_pass "the live {revision, document} envelope" verify_director_reassign_capability

# The exact response the first operator run met. Roles are absent at the top
# level, so the original check read an empty role list, called that an inactive
# director, and failed a healthy board.
python3 - "$SYRD86_WORKFLOW_BODY" <<'PY'
import json
import sys

path = sys.argv[1]
payload = json.load(open(path))
assert "roles" not in payload, "the live envelope must not carry roles at the top level"
assert payload["document"]["roles"], "roles belong to the nested document"
PY

# A flat document is not a shape this API produces. Tolerating it would let the
# check pass against a board whose configuration was never read.
cat >"$SYRD86_WORKFLOW_BODY" <<EOF
{"roles": [{"name": "director", "label": "Director", "kind": "system", "active": true, "capabilities": $FULL_CAPABILITIES}]}
EOF
expect_fail "a flat response with no document envelope" verify_director_reassign_capability

# The envelope is not enough on its own: the capability itself still has to be
# there, and the director still has to be active.
workflow_document true "$NO_REASSIGN_CAPABILITIES" >"$SYRD86_WORKFLOW_BODY"
expect_fail "a director without the reassign capability" verify_director_reassign_capability

workflow_document false "$FULL_CAPABILITIES" >"$SYRD86_WORKFLOW_BODY"
expect_fail "an inactive director" verify_director_reassign_capability

echo '{"revision": 19, "document": {"roles": [{"name": "ops", "active": true, "capabilities": []}]}}' \
    >"$SYRD86_WORKFLOW_BODY"
expect_fail "a document with no director role at all" verify_director_reassign_capability

# --- the legacy_uid empty-assignment contract --------------------------------

# What the live board reports during this interim activation. No process
# registers itself under legacy UID authority, so the empty map is the contract.
echo '{"project": "syrd", "authority_mode": "legacy_uid", "assignments": {}}' >"$SYRD86_RUNTIME_BODY"
expect_pass "legacy_uid with an empty assignment map" verify_legacy_runtime_api

# A populated map means process authority reached the board ahead of the
# project-account cutover this activation deliberately defers.
cat >"$SYRD86_RUNTIME_BODY" <<'EOF'
{"project": "syrd", "authority_mode": "legacy_uid",
 "assignments": {"director": {"target": "syrd-director:0.0", "pid": 4242}}}
EOF
expect_fail "legacy_uid carrying a live process assignment" verify_legacy_runtime_api

echo '{"project": "syrd", "authority_mode": "process", "assignments": {}}' >"$SYRD86_RUNTIME_BODY"
expect_fail "process authority mode during the legacy interim activation" verify_legacy_runtime_api

echo '{"project": "syrd", "authority_mode": "legacy_uid"}' >"$SYRD86_RUNTIME_BODY"
expect_fail "a response with no assignment map" verify_legacy_runtime_api

# --- retry and post-migration recovery, read off the shipped bytes ------------

retry_branch="$(awk '/if \[\[ "\$release" == "\$BOARD_ROOT\/releases\/\$EXPECTED_TARGET"/,/^    fi$/' "$ARTIFACT")"
[[ -n "$retry_branch" ]] || fail "could not find the already-active branch in the artifact"
# Commentary about what the branch does not do is not the branch doing it.
retry_code="$(grep -v '^[[:space:]]*#' <<<"$retry_branch")"
grep -q 'verify_target_health' <<<"$retry_code" || \
    fail "the already-active branch must still prove target health"
for forbidden in standard_deploy prepare_source_checkout mutation_started mktemp; do
    if grep -q "$forbidden" <<<"$retry_code"; then
        fail "the already-active retry must not reach $forbidden"
    fi
done

# Recovery after the mutating phase is forward. Redeploying the previous release
# once the additive migrations are applied would run code that grants an
# unregistered process director authority.
if grep -q 'standard_deploy "\$EXPECTED_PREVIOUS"' "$ARTIFACT"; then
    fail "the artifact must not redeploy the previous release after migrations"
fi
grep -q 'recover_forward_after_migrations' "$ARTIFACT" || \
    fail "the artifact must define forward-safe post-migration recovery"
if ! declare -F recover_forward_after_migrations >/dev/null; then
    fail "forward-safe recovery is not a callable function"
fi
if declare -F rollback_previous_release >/dev/null; then
    fail "the backward rollback path must be gone, not merely unused"
fi
if declare -F verify_previous_health >/dev/null; then
    fail "the previous-release health proof must be gone with the rollback it served"
fi

echo "SYRD-86 activation board-contract regression passed"
