#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMPFILES_SOURCE="$REPO_ROOT/deploy/tmpfiles/pgu-ticket-board.conf"
SERVICE_SOURCE="$REPO_ROOT/deploy/systemd/pgu-ticket-board.service.boardsvc"
RUNTIME="/run/user/$(id -u)"

unit_frames_path="$(sed -n 's/.* --frames \([^ ]*\) .*/\1/p' "$SERVICE_SOURCE")"
tmpfiles_frames_path="$(awk '$1 == "d" { print $2 }' "$TMPFILES_SOURCE")"
[[ "$unit_frames_path" == "/tmp/pgu-frames" ]] || {
    echo "FAIL: boardsvc unit does not pass the expected shared frame directory" >&2
    exit 1
}
[[ "$tmpfiles_frames_path" == "$unit_frames_path" ]] || {
    echo "FAIL: tmpfiles path does not match the boardsvc --frames path" >&2
    echo "unit: $unit_frames_path" >&2
    echo "tmpfiles: $tmpfiles_frames_path" >&2
    exit 1
}
grep -q "^ReadWritePaths=.* $unit_frames_path\\($\\| \\)" "$SERVICE_SOURCE" || {
    echo "FAIL: boardsvc unit ReadWritePaths does not include the frame directory" >&2
    exit 1
}

if [[ ! -S "$RUNTIME/bus" ]]; then
    echo "ticket_board_boardsvc_tmpfiles_namespace_test: skipped, user systemd bus unavailable"
    exit 0
fi
if ! command -v systemd-tmpfiles >/dev/null 2>&1; then
    echo "ticket_board_boardsvc_tmpfiles_namespace_test: skipped, systemd-tmpfiles unavailable"
    exit 0
fi

export XDG_RUNTIME_DIR="$RUNTIME"
export DBUS_SESSION_BUS_ADDRESS="unix:path=$RUNTIME/bus"

TEST_ROOT="$(mktemp -d /tmp/pgu-560-namespace.XXXXXX)"
UNIT_NAME="pgu-560-namespace-${TEST_ROOT##*.}.service"
UNIT_PATH="$TEST_ROOT/$UNIT_NAME"
TMPFILES_TEST="$TEST_ROOT/tmpfiles.conf"
ASSETS_DIR="$TEST_ROOT/assets"
FRAMES_DIR="$TEST_ROOT/frames"
USER_NAME="$(id -un)"
GROUP_NAME="$(id -gn)"
USER_UNIT_LINK="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$UNIT_NAME"

cleanup() {
    systemctl --user reset-failed "$UNIT_NAME" >/dev/null 2>&1 || true
    rm -f "$USER_UNIT_LINK"
    systemctl --user daemon-reload >/dev/null 2>&1 || true
    rm -rf "$TEST_ROOT"
}
trap cleanup EXIT

mkdir -p "$ASSETS_DIR"

cat >"$UNIT_PATH" <<EOF
[Unit]
Description=PGU 280 namespace regression

[Service]
Type=oneshot
ExecStartPre=/bin/mkdir -p $FRAMES_DIR
ExecStart=/usr/bin/test -d $FRAMES_DIR
ProtectSystem=full
ProtectHome=read-only
ReadWritePaths=$ASSETS_DIR $FRAMES_DIR
EOF

systemctl --user link "$UNIT_PATH" >/dev/null

if systemctl --user start "$UNIT_NAME" >/dev/null 2>&1; then
    echo "FAIL: sandboxed unit unexpectedly started with missing ReadWritePaths target" >&2
    exit 1
fi
if [[ -d "$FRAMES_DIR" ]]; then
    echo "FAIL: ExecStartPre created the missing frame directory despite namespace setup failure" >&2
    exit 1
fi
pre_status="$(systemctl --user show "$UNIT_NAME" -p ExecStartPre --value || true)"
if [[ "$pre_status" != *"status=226"* ]]; then
    echo "FAIL: missing ReadWritePaths target did not fail at ExecStartPre namespace setup" >&2
    echo "$pre_status" >&2
    exit 1
fi

sed \
    -e "s# /tmp/pgu-frames # $FRAMES_DIR #" \
    -e "s# root root # $USER_NAME $GROUP_NAME #" \
    "$TMPFILES_SOURCE" >"$TMPFILES_TEST"
systemd-tmpfiles --create "$TMPFILES_TEST"

[[ -d "$FRAMES_DIR" ]] || {
    echo "FAIL: tmpfiles did not create the frame directory" >&2
    exit 1
}
[[ "$(stat -c '%a' "$FRAMES_DIR")" == "1777" ]] || {
    echo "FAIL: tmpfiles did not apply shared frame directory permissions" >&2
    exit 1
}

systemctl --user reset-failed "$UNIT_NAME" >/dev/null 2>&1 || true
systemctl --user start "$UNIT_NAME" >/dev/null

echo "ticket_board_boardsvc_tmpfiles_namespace_test: ok"
