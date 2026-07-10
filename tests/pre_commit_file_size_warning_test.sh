#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$REPO_ROOT/scripts/install-update-hook.sh"

tmpdir="$(mktemp -d)"
cleanup() {
    rm -rf "$tmpdir"
}
trap cleanup EXIT

repo="$tmpdir/repo"
server_hooks="$tmpdir/server-hooks"

git init "$repo" >/dev/null
git -C "$repo" config user.name Test
git -C "$repo" config user.email test@example.com
printf 'seed\n' >"$repo/README.md"
git -C "$repo" add README.md
git -C "$repo" commit -m "seed" >/dev/null

PGU_UPDATE_HOOK_SERVER_HOOKS_DIR="$server_hooks" \
PGU_UPDATE_HOOK_LOCAL_REPO_ROOT="$repo" \
PGU_UPDATE_HOOK_REPO_ROOT="$REPO_ROOT" \
PGU_FILE_SIZE_LINE_LIMIT=5 \
"$INSTALLER" >/dev/null

pre_commit_hook="$(git -C "$repo" rev-parse --git-path hooks)/pre-commit"
warning_helper="$(git -C "$repo" rev-parse --git-path hooks)/pgu-warn-file-size-limit.py"
if [[ "$pre_commit_hook" != /* ]]; then
    pre_commit_hook="$repo/$pre_commit_hook"
fi
if [[ "$warning_helper" != /* ]]; then
    warning_helper="$repo/$warning_helper"
fi
[[ -x "$pre_commit_hook" ]] || {
    echo "FAIL: pre-commit hook was not installed" >&2
    exit 1
}
[[ -x "$warning_helper" ]] || {
    echo "FAIL: warning helper was not installed" >&2
    exit 1
}

mkdir -p "$repo/src" "$repo/third_party"
python3 - <<'PY' >"$repo/src/too_big.py"
for i in range(7):
    print(f"line_{i}")
PY
python3 - <<'PY' >"$repo/third_party/ignored.py"
for i in range(20):
    print(f"ignored_{i}")
PY
git -C "$repo" add src/too_big.py third_party/ignored.py

stdout_file="$tmpdir/commit.stdout"
stderr_file="$tmpdir/commit.stderr"
git -C "$repo" commit -m "add oversized file" >"$stdout_file" 2>"$stderr_file"

grep -q 'warning: src/too_big.py is 7 lines (soft limit 5) - consider splitting\.' "$stderr_file" || {
    echo "FAIL: expected warning for oversized staged file" >&2
    cat "$stderr_file" >&2
    exit 1
}
! grep -q 'third_party/ignored.py' "$stderr_file" || {
    echo "FAIL: ignored third_party file should not warn" >&2
    cat "$stderr_file" >&2
    exit 1
}

echo "pre_commit_file_size_warning_test: ok"
