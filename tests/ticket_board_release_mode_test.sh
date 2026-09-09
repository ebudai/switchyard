#!/usr/bin/env bash
set -euo pipefail

# SYRD-89 R3. A published board release root must be traversable by the board
# service whatever umask the deploy inherited.
#
# The incident: deploy_export_release created the release root with `mkdir -p`
# under the caller's umask and published it with `mv`, which preserves that
# mode. Run under umask 077 the root was 0700, and the canary -- which runs as
# the board service account -- failed with EACCES opening the release's own
# ticket-board.py. The inherited ACL already named the service account
# `user:<svc>:r-x`, but a named entry is capped by the access mask and POSIX
# derives the mask from the group bits, so at 0700 the entry read
# `#effective:---`: present, and worth nothing.
#
# This drives the real function, under umask 077, and then asks the kernel the
# question that actually matters by reading the released file as a different
# uid. A numeric mode is a claim about access; this is the access.

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_SH="$REPO_ROOT/scripts/ticket-board-service.sh"

fail() { echo "FAIL: $*" >&2; exit 1; }

# Acting as a second uid needs a uid to act as. Outside a user namespace there
# is none available unprivileged, so the test re-executes itself inside one
# rather than asserting something weaker (SYRD-89 R3 audit).
if [[ "${1:-}" != "--in-namespace" ]]; then
    if ! command -v unshare >/dev/null 2>&1; then
        echo "SKIPPED ticket_board_release_mode_test: unshare is unavailable"
        exit 0
    fi
    if ! unshare --user --map-auto --map-root-user true 2>/dev/null; then
        echo "SKIPPED ticket_board_release_mode_test: unprivileged user namespaces are unavailable"
        exit 0
    fi
    exec unshare --user --map-auto --map-root-user "$BASH" "${BASH_SOURCE[0]}" --in-namespace
fi

command -v setpriv >/dev/null 2>&1 || { echo "SKIPPED ticket_board_release_mode_test: setpriv is unavailable"; exit 0; }
command -v setfacl >/dev/null 2>&1 || { echo "SKIPPED ticket_board_release_mode_test: setfacl is unavailable"; exit 0; }

work="$(mktemp -d)"
trap 'chmod -R u+rwX "$work" 2>/dev/null || true; rm -rf -- "$work"' EXIT

# A uid that is not this one: the board service is its own account and is not in
# the release owner's group, which is why group bits alone never granted it
# anything.
SERVICE_UID=1234
as_service() { setpriv --reuid="$SERVICE_UID" --regid="$SERVICE_UID" --clear-groups "$@"; }

# The source tree the export copies, carrying its own release marker so the
# export takes the `release` path and needs no git.
SHA="1111111111111111111111111111111111111111"
src="$work/source"
mkdir -p "$src/scripts"
printf '%s\n' 'print("board")' >"$src/scripts/ticket-board.py"
chmod 0755 "$src/scripts/ticket-board.py"
chmod 0755 "$src/scripts"
printf '{"commit": "%s"}\n' "$SHA" >"$src/.switchyard-release.json"

# The releases directory, with the production shape: a default ACL naming the
# service account, so every new release root inherits `user:<svc>:r-x`.
releases="$work/releases"
mkdir -p "$releases"
chmod 0755 "$releases"
setfacl -m "u:$SERVICE_UID:r-x" -m "d:u:$SERVICE_UID:r-x" "$releases" \
    || fail "could not put the production ACL shape on the fixture releases directory"

# Everything above the releases directory must be traversable, exactly as the
# real chain is: the point of this test is the release root, not its parents.
chmod 0755 "$work"

# The script declares these readonly, so they are supplied the way a deploy
# supplies them -- through the environment, before it is sourced.
export SOURCE_REPO="$src"
export BOARD_ROOT="$work"
export DEPLOY_REF="$SHA"
export TICKET_BOARD_PROJECT="fixture"
export TICKET_BOARD_OWNER_HOME="$work"
# The fixture's board service identity. Named numerically because a user
# namespace has no passwd entry for it, which the resolver must handle anyway.
export BOARD_CANARY_USER="$SERVICE_UID"
source "$SERVICE_SH"

[[ "$BOARD_RELEASES_DIR" == "$releases" ]] || \
    fail "the fixture releases directory is $BOARD_RELEASES_DIR, not $releases"

# --- the incident, under the umask that produced it --------------------------
umask 077
export_output="$(deploy_export_release)" || fail "deploy_export_release failed under umask 077"
release_dir="$(printf '%s' "$export_output" | cut -f2)"
[[ -d "$release_dir" ]] || fail "the export produced no release directory: $export_output"

# The inherited named entry must be present -- if it is not, this fixture is not
# reproducing production and everything below it would be vacuous.
getfacl -p --omit-header "$release_dir" 2>/dev/null | grep -q "user:$SERVICE_UID:r-x" \
    || fail "the fixture did not inherit the service ACL entry; the test would prove nothing"

# The question the canary asks: can the service account open the entry point?
entry="$release_dir/scripts/ticket-board.py"
as_service test -r "$entry" \
    || fail "the board service account cannot read $entry after an export under umask 077"
as_service head -c 1 "$entry" >/dev/null \
    || fail "the board service account cannot actually read the bytes of $entry"

# And the mask is why. Not asserted instead of the read above -- asserted as
# well, because it names the mechanism the fix relies on.
mask="$(getfacl -p --omit-header "$release_dir" 2>/dev/null | sed -n 's/^mask::\(.*\)$/\1/p')"
[[ "$mask" == "r-x" ]] || fail "the release root mask is '$mask', so the named service entry is not effective"
getfacl -p --omit-header "$release_dir" 2>/dev/null | grep -q '#effective:---' \
    && fail "an ACL entry on the release root is masked to nothing"

# The published mode is a recorded decision, so it is pinned as one. The value
# is read from the sourced script rather than matched in its text.
[[ "$BOARD_RELEASE_ROOT_MODE" == "0750" ]] || \
    fail "the published release root mode is $BOARD_RELEASE_ROOT_MODE; this ticket records 0750"

# 0750, not 0755: the decision this ticket records.
mode="$(stat -c '%a' "$release_dir")"
[[ "$mode" == "750" ]] || fail "the published release root is mode $mode, expected 750"
perms="$(stat -c '%A' "$release_dir")"
[[ "${perms:7:3}" == "---" ]] || fail "the release root grants 'other' $(printf '%s' "${perms:7:3}"), which nothing in this model needs"

# The modes inside are the release's own and must survive normalization.
[[ "$(stat -c '%a' "$entry")" == "755" ]] || \
    fail "the released entry point is mode $(stat -c '%a' "$entry"), expected the archive's 755"

# --- a reused release with an unsafe root ------------------------------------
# The contract: repaired after its provenance is verified, or refused before the
# canary. Never left for the canary to discover.
chmod 0700 "$release_dir"
as_service test -r "$entry" && fail "the fixture did not actually break traversal"
reused="$( ( deploy_export_release ) )" || fail "a reused release with an unsafe root was refused instead of repaired"
[[ "$(printf '%s' "$reused" | cut -f2)" == "$release_dir" ]] || fail "the reused export named a different directory"
as_service test -r "$entry" || fail "a reused release with an unsafe root was not repaired"

# A root that is already serviceable is left exactly as it is: tightening
# somebody else's release is not this function's business.
chmod 0755 "$release_dir"
deploy_export_release >/dev/null || fail "a serviceable 0755 release was refused"
[[ "$(stat -c '%a' "$release_dir")" == "755" ]] || \
    fail "an already-serviceable release root was rewritten to $(stat -c '%a' "$release_dir")"

# Provenance before permissions: a tree whose sha does not match must be refused
# rather than made reachable.
chmod 0700 "$release_dir"
printf '%s\n' "0000000000000000000000000000000000000000" >"$release_dir/.pgu-deploy-sha"
# In a subshell: `die` exits the shell it runs in, so calling it directly here
# would end the test rather than be caught by it.
if ( deploy_export_release ) >/dev/null 2>&1; then
    fail "a release whose recorded sha does not match was accepted"
fi
[[ "$(stat -c '%a' "$release_dir")" == "700" ]] || \
    fail "a release that failed its sha check was made reachable anyway"

# --- SYRD-89 R3 audit: the predicate must agree with the kernel ---------------
#
# Mode bits are not access. Three ways the old numeric test was wrong, each
# driven here against the uid that actually has to get in.

probe="$work/probe"
mkdir -p "$probe"
chmod 0755 "$probe"

agrees() {
    # $1 path, $2 expected ("granted"|"denied"); compares the predicate against
    # the kernel's own answer for the service uid.
    local path="$1" expected="$2" predicted kernel
    if path_grants_rx_to "$path" "$SERVICE_UID"; then predicted=granted; else predicted=denied; fi
    if as_service test -r "$path" && as_service test -x "$path"; then kernel=granted; else kernel=denied; fi
    [[ "$predicted" == "$expected" ]] || \
        fail "$path: predicate said $predicted, expected $expected"
    [[ "$kernel" == "$expected" ]] || \
        fail "$path: kernel said $kernel, expected $expected -- the fixture does not model what it claims"
    [[ "$predicted" == "$kernel" ]] || \
        fail "$path: predicate said $predicted but the kernel said $kernel"
}

# 0755: reachable by anyone, and the predicate must say so.
mkdir -p "$probe/world"; chmod 0755 "$probe/world"
agrees "$probe/world" granted

# 0750 with no named entry, and the service account not in the owning group.
# Every numeric test called this serviceable; the uid gets EACCES.
mkdir -p "$probe/plain750"; chmod 0750 "$probe/plain750"
agrees "$probe/plain750" denied

# 0750 with the named entry: the production shape. The mask carries the entry.
mkdir -p "$probe/named750"; setfacl -m "u:$SERVICE_UID:r-x" "$probe/named750"; chmod 0750 "$probe/named750"
agrees "$probe/named750" granted

# The same entry under 0700: present, and masked to nothing. This is the
# incident.
mkdir -p "$probe/named700"; setfacl -m "u:$SERVICE_UID:r-x" "$probe/named700"; chmod 0700 "$probe/named700"
agrees "$probe/named700" denied

# A symlink. `stat` reports it `lrwxrwxrwx`, whose `other` bits are r-x, so
# every mode-based test accepted one as a release root.
ln -sfn "$probe/world" "$probe/link"
path_grants_rx_to "$probe/link" "$SERVICE_UID" \
    && fail "a symlink was accepted as a serviceable release root"
release_root_is_serviceable "$probe/link" \
    && fail "release_root_is_serviceable accepted a symlink"

# And the guard runs before any early return, so a symlink can never reach the
# serviceability shortcut.
if ( ensure_release_root_serviceable "$probe/link" ) >/dev/null 2>&1; then
    fail "ensure_release_root_serviceable accepted a symlink"
fi
# Captured, not piped: under `set -o pipefail` the dying function's status
# fails the pipeline even when grep matched.
link_refusal="$( ( ensure_release_root_serviceable "$probe/link" ) 2>&1 || true )"
grep -q 'symlink' <<<"$link_refusal" \
    || fail "the symlink refusal must say what is wrong, got: $link_refusal"

# A reused root the mask cannot save: no named entry, wrong group. Raising the
# mask is not enough, so it must refuse with the command that fixes it rather
# than hand an unreadable tree to the canary.
refusal="$( ( ensure_release_root_serviceable "$probe/plain750" ) 2>&1 || true )"
grep -q 'setfacl -m' <<<"$refusal" \
    || fail "a root with no named entry must refuse with the exact setfacl command, got: $refusal"
as_service test -r "$probe/plain750" \
    && fail "the refusal path must not have granted access"

# And one the mask can save is repaired in place, not refused.
ensure_release_root_serviceable "$probe/named700" \
    || fail "a root whose named entry only needed the mask must be repaired"
as_service test -r "$probe/named700" \
    || fail "the repaired root is still not readable by the service account"
[[ "$(stat -c '%a' "$probe/named700")" == "750" ]] \
    || fail "the repair must raise the mask, not replace the mode"

echo "ticket board release mode contract regression passed"
