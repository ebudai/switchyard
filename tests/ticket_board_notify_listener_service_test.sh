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
printf '#!/usr/bin/env python3\nprint("listener")\n' >"$SOURCE_REPO/scripts/ticket-board-notify-listener"
chmod +x "$SOURCE_REPO/scripts/ticket-board-notify-listener"
git -C "$SOURCE_REPO" add scripts/ticket-board-notify-listener
git -C "$SOURCE_REPO" commit -m "seed" >/dev/null

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" deploy >/dev/null

[[ -L "$DEPLOY_ROOT/current" ]] || {
    echo "FAIL: current deploy link missing" >&2
    exit 1
}
[[ -x "$DEPLOY_ROOT/current/scripts/ticket-board-notify-listener" ]] || {
    echo "FAIL: exported listener script missing from current deploy" >&2
    exit 1
}
if [[ -d "$DEPLOY_ROOT/current/.git" ]]; then
    echo "FAIL: deploy root should be an export, not a git checkout" >&2
    exit 1
fi

BOARD_ROOT="$DEPLOY_ROOT" SOURCE_REPO="$SOURCE_REPO" DEPLOY_REF=HEAD \
    "$REPO_ROOT/scripts/ticket-board-notify-listener-service.sh" render-unit >"$UNIT_DIR/service.unit"

grep -q "WorkingDirectory=$DEPLOY_ROOT/current" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not point to deploy current link" >&2
    exit 1
}
grep -q "ExecStart=/usr/bin/python3 $DEPLOY_ROOT/current/scripts/ticket-board-notify-listener" "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit ExecStart did not point to deployed listener script" >&2
    exit 1
}
grep -q '^Environment=PGUSER=ticket_board_listener$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: unit did not select ticket_board_listener by default" >&2
    exit 1
}
grep -q '^Restart=always$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: listener unit should always restart after drops/crashes" >&2
    exit 1
}
grep -q '^RuntimeMaxSec=900$' "$UNIT_DIR/service.unit" || {
    echo "FAIL: listener unit should periodically recycle as a wedge backstop" >&2
    exit 1
}

echo "ticket_board_notify_listener_service_test: ok"
