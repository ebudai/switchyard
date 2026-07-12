#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCRIPT="$REPO_ROOT/scripts/pgu-wayland-clipboard-access.sh"
RUNBOOK="$REPO_ROOT/docs/pgu-wayland-clipboard-access.md"

[[ -x "$SCRIPT" ]] || {
    echo "FAIL: clipboard access helper must exist and be executable" >&2
    exit 1
}
bash -n "$SCRIPT"

grep -q 'setfacl -m "u:${AGENT_USER}:x" "$runtime_dir"' "$SCRIPT" || {
    echo "FAIL: helper must grant agent traverse ACL on Eric runtime dir" >&2
    exit 1
}
grep -q 'setfacl -m "u:${AGENT_USER}:rw" "$socket_path"' "$SCRIPT" || {
    echo "FAIL: helper must grant agent rw ACL on Wayland socket" >&2
    exit 1
}
grep -q 'After=graphical-session.target plasma-kwin_wayland.service' "$SCRIPT" || {
    echo "FAIL: user unit must run after Eric graphical Wayland session starts" >&2
    exit 1
}
grep -q 'install -m 0755 "$(realpath "$0")" "$install_path"' "$SCRIPT" || {
    echo "FAIL: installer must copy helper into Eric home for reboot persistence" >&2
    exit 1
}
grep -q 'ExecStart=${install_path} apply-acl --wait' "$SCRIPT" || {
    echo "FAIL: user unit must wait for the recreated Wayland socket" >&2
    exit 1
}
grep -q 'WantedBy=graphical-session.target default.target' "$SCRIPT" || {
    echo "FAIL: user unit must be enabled for future logins" >&2
    exit 1
}
grep -q 'tmux setenv -g XDG_RUNTIME_DIR "$(gui_runtime_dir)"' "$SCRIPT" || {
    echo "FAIL: helper must set pgu tmux XDG_RUNTIME_DIR to Eric runtime" >&2
    exit 1
}
grep -q 'tmux setenv -g WAYLAND_DISPLAY "$WAYLAND_NAME"' "$SCRIPT" || {
    echo "FAIL: helper must set pgu tmux WAYLAND_DISPLAY to wayland-0" >&2
    exit 1
}
grep -q 'XDG_RUNTIME_DIR="$runtime_dir" WAYLAND_DISPLAY="$WAYLAND_NAME" wl-paste --list-types' "$SCRIPT" || {
    echo "FAIL: helper must verify the real wl-paste path" >&2
    exit 1
}

grep -q 'scripts/pgu-wayland-clipboard-access.sh install-eric-user-unit' "$RUNBOOK" || {
    echo "FAIL: runbook lacks Eric persistent install command" >&2
    exit 1
}
grep -q '~/.local/bin/pgu-wayland-clipboard-access' "$RUNBOOK" || {
    echo "FAIL: runbook must document the durable installed helper path" >&2
    exit 1
}
grep -q 'scripts/pgu-wayland-clipboard-access.sh apply-agent-tmux-env' "$RUNBOOK" || {
    echo "FAIL: runbook lacks agent tmux env command" >&2
    exit 1
}
grep -q 'XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 wl-paste --list-types' "$RUNBOOK" || {
    echo "FAIL: runbook lacks clipboard verification command" >&2
    exit 1
}
grep -q 'Reboot verification is the acceptance test' "$RUNBOOK" || {
    echo "FAIL: runbook must document reboot persistence verification" >&2
    exit 1
}

echo "pgu_wayland_clipboard_access_test: ok"
