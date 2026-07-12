#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPDIR_T="$(mktemp -d)"
trap 'rm -rf "$TMPDIR_T"' EXIT

SOURCE_REPO="$TMPDIR_T/source"
DEPLOY_ROOT="$TMPDIR_T/live"
UNIT_DIR="$TMPDIR_T/unit"
mkdir -p "$UNIT_DIR"

git init "$SOURCE_REPO" >/dev/null
git -C "$SOURCE_REPO" config user.name Test
git -C "$SOURCE_REPO" config user.email test@example.com
mkdir -p "$SOURCE_REPO/scripts"
printf '#!/usr/bin/env python3\nprint("board")\n' >"$SOURCE_REPO/scripts/ticket-board.py"
chmod +x "$SOURCE_REPO/scripts/ticket-board.py"
git -C "$SOURCE_REPO" add scripts/ticket-board.py
git -C "$SOURCE_REPO" commit -m "seed" >/dev/null

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-service.sh" deploy >/dev/null

[[ -L "$DEPLOY_ROOT/current" ]] || {
    echo "FAIL: current deploy link missing" >&2
    exit 1
}
[[ -x "$DEPLOY_ROOT/current/scripts/ticket-board.py" ]] || {
    echo "FAIL: exported board script missing from current deploy" >&2
    exit 1
}
if [[ -d "$DEPLOY_ROOT/current/.git" ]]; then
    echo "FAIL: deploy root should be an export, not a git checkout" >&2
    exit 1
fi

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-service.sh" render-unit >"$UNIT_DIR/service.unit"

grep -q "WorkingDirectory=$DEPLOY_ROOT/current" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not point to deploy current link" >&2
    exit 1
}
grep -q "ExecStart=/usr/bin/python3 $DEPLOY_ROOT/current/scripts/ticket-board.py" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit ExecStart did not point to deploy current script" >&2
    exit 1
}
grep -q -- "--unix-socket /tmp/pgu-ticket-board.sock" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not expose the local pane Unix socket" >&2
    exit 1
}
retired_backend_flag='--store''-backend'
if grep -q -- "$retired_backend_flag" "$UNIT_DIR/service.unit"; then
    echo "FAIL: rendered unit should not pass the removed backend selector option" >&2
    exit 1
fi
grep -q '^Environment=PGU_TICKET_BOARD_SOCKET=/tmp/pgu-ticket-board.sock$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not export PGU_TICKET_BOARD_SOCKET" >&2
    exit 1
}

echo "ticket_board_service_deploy_test: ok"
