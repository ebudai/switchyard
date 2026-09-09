#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARTIFACT="$REPO_ROOT/deploy/SYRD-85-activate-audited-board.sh"

# Exercise the artifact as namespace root while the pinned cache retains a
# different owner, matching the trust boundary that rejected the operator run.
if [[ "${SYRD85_USERNS_ROOT:-0}" != "1" ]]; then
    exec unshare --user --map-root-user \
        env SYRD85_USERNS_ROOT=1 bash "$0"
fi

[[ "$(id -u)" == "0" ]] || {
    echo "FAIL: cross-owner regression did not enter a root user namespace" >&2
    exit 1
}

# Load only the artifact definitions. The final trap/main invocation is
# intentionally excluded: this regression must never execute the activation.
source <(sed '/^trap /,$d' "$ARTIFACT")

[[ -d "$CACHE" ]] || {
    echo "FAIL: pinned source cache is unavailable: $CACHE" >&2
    exit 1
}
[[ "$(stat -c '%u' "$CACHE")" != "$(id -u)" ]] || {
    echo "FAIL: pinned cache is not cross-owner in the root regression" >&2
    exit 1
}

test_root="$(mktemp -d /tmp/syrd-85-safe-directory-test.XXXXXX)"
cleanup() {
    if [[ -n "${work_root:-}" && -d "$work_root" ]]; then
        rm -rf -- "$work_root"
    fi
    rm -rf -- "$test_root"
}
trap cleanup EXIT
git_bin="$test_root/bin"
trace="$test_root/cache-reads"
mkdir -p "$git_bin"
real_git="$(command -v git)"

cat >"$git_bin/git" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail

if [[ "${1:-}" == "ls-remote" ]]; then
    printf '%s\t%s\n' "$EXPECTED_TARGET" "$PUBLIC_REF"
    exit 0
fi

touches_cache=0
has_exact_allowance=0
for argument in "$@"; do
    case "$argument" in
        "$CACHE"|"--git-dir=$CACHE") touches_cache=1 ;;
        "safe.directory=$CACHE") has_exact_allowance=1 ;;
    esac
done

if (( touches_cache == 1 )); then
    if (( has_exact_allowance != 1 )); then
        echo "fatal: detected dubious ownership in repository at '$CACHE'" >&2
        exit 128
    fi
    printf '%q ' "$@" >>"$SYRD85_GIT_TRACE"
    printf '\n' >>"$SYRD85_GIT_TRACE"
fi

exec "$SYRD85_REAL_GIT" "$@"
EOF
chmod +x "$git_bin/git"

export PATH="$git_bin:/usr/bin:/bin"
export SYRD85_REAL_GIT="$real_git"
export SYRD85_GIT_TRACE="$trace"
export CACHE EXPECTED_TARGET PUBLIC_REF

verify_exact_source
prepare_source_checkout

[[ "$(wc -l <"$trace")" == "4" ]] || {
    echo "FAIL: expected four safe-directory-scoped reads of the pinned cache" >&2
    cat "$trace" >&2
    exit 1
}

echo "SYRD-85 cross-owner namespace-root cache regression passed"
