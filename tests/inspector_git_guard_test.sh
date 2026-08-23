#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER="$REPO_ROOT/scripts/install-inspector-git-guard.sh"

tmpdir="$(mktemp -d)"
cleanup() {
    rm -rf "$tmpdir"
}
trap cleanup EXIT

home_dir="$tmpdir/home"
hooks_dir="$tmpdir/hooks"
repo="$tmpdir/repo"
remote="$tmpdir/remote.git"
global_config="$tmpdir/gitconfig"
mkdir -p "$home_dir"

HOME="$home_dir" \
GIT_CONFIG_GLOBAL="$global_config" \
GLOBAL_GIT_HOOKS_DIR="$hooks_dir" \
INSPECTOR_GIT_GUARD_REPO_ROOT="$REPO_ROOT" \
FILE_SIZE_LINE_LIMIT=5 \
    "$INSTALLER" >/dev/null

[[ "$(GIT_CONFIG_GLOBAL="$global_config" git config --global --get core.hooksPath)" == "$hooks_dir" ]] || {
    echo "FAIL: installer did not set global core.hooksPath" >&2
    exit 1
}
[[ -x "$hooks_dir/pre-commit" && -x "$hooks_dir/pre-push" ]] || {
    echo "FAIL: global pre-commit/pre-push hooks were not installed" >&2
    exit 1
}

git init --bare "$remote" >/dev/null
git init "$repo" >/dev/null
git -C "$repo" config user.name Test
git -C "$repo" config user.email test@example.com
git -C "$repo" remote add origin "$remote"
printf 'seed\n' >"$repo/README.md"
git -C "$repo" add README.md
HOME="$home_dir" GIT_CONFIG_GLOBAL="$global_config" TICKET_BOARD_PANE_TARGET=pgu-ops:0.0 \
    git -C "$repo" commit -m "seed" >/dev/null

printf 'inspector edit\n' >"$repo/inspector.txt"
git -C "$repo" add inspector.txt
if HOME="$home_dir" GIT_CONFIG_GLOBAL="$global_config" TICKET_BOARD_PANE_TARGET=pgu-inspector:0.0 \
    git -C "$repo" commit -m "inspector commit" >"$tmpdir/inspector-commit.out" 2>"$tmpdir/inspector-commit.err"; then
    echo "FAIL: inspector pre-commit was allowed" >&2
    exit 1
fi
grep -q 'Inspector role cannot commit or push code' "$tmpdir/inspector-commit.err" || {
    echo "FAIL: inspector commit rejection message was unclear" >&2
    cat "$tmpdir/inspector-commit.err" >&2
    exit 1
}

HOME="$home_dir" GIT_CONFIG_GLOBAL="$global_config" TICKET_BOARD_PANE_TARGET=pgu-ops:0.0 \
    git -C "$repo" commit -m "normal commit" >/dev/null

if HOME="$home_dir" GIT_CONFIG_GLOBAL="$global_config" TICKET_BOARD_PANE_TARGET=pgu-inspector:0.0 \
    git -C "$repo" push origin HEAD:refs/heads/inspector-test >"$tmpdir/inspector-push.out" 2>"$tmpdir/inspector-push.err"; then
    echo "FAIL: inspector pre-push was allowed" >&2
    exit 1
fi
grep -q 'Inspector role cannot commit or push code' "$tmpdir/inspector-push.err" || {
    echo "FAIL: inspector push rejection message was unclear" >&2
    cat "$tmpdir/inspector-push.err" >&2
    exit 1
}

HOME="$home_dir" GIT_CONFIG_GLOBAL="$global_config" TICKET_BOARD_PANE_TARGET=pgu-ops:0.0 \
    git -C "$repo" push origin HEAD:refs/heads/normal-test >/dev/null

printf 'next\n' >"$repo/next.txt"
git -C "$repo" add next.txt
HOME="$home_dir" GIT_CONFIG_GLOBAL="$global_config" TICKET_BOARD_PANE_TARGET=pgu-ops:0.0 \
    git -C "$repo" commit -m "next" >/dev/null
if HOME="$home_dir" GIT_CONFIG_GLOBAL="$global_config" TICKET_BOARD_PANE_TARGET=pgu-ops:0.0 \
    git -C "$repo" push origin HEAD:refs/heads/main >"$tmpdir/main-push.out" 2>"$tmpdir/main-push.err"; then
    echo "FAIL: normal pane should still be blocked from direct main push" >&2
    exit 1
fi
grep -q 'Direct pushes to origin/main are blocked' "$tmpdir/main-push.err" || {
    echo "FAIL: main guard was not preserved in global pre-push" >&2
    cat "$tmpdir/main-push.err" >&2
    exit 1
}

echo "inspector_git_guard_test: ok"
