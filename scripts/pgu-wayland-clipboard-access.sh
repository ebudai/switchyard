#!/usr/bin/env bash
set -euo pipefail

GUI_USER="${PGU_WAYLAND_GUI_USER:-eric}"
AGENT_USER="${PGU_WAYLAND_AGENT_USER:-agent}"
WAYLAND_NAME="${PGU_WAYLAND_DISPLAY:-wayland-0}"
SERVICE_NAME="${PGU_WAYLAND_SERVICE_NAME:-pgu-agent-wayland-clipboard.service}"

usage() {
    cat <<'EOF'
Usage:
  pgu-wayland-clipboard-access.sh retire
  pgu-wayland-clipboard-access.sh apply-agent-tmux-env
  pgu-wayland-clipboard-access.sh print-agent-env
  pgu-wayland-clipboard-access.sh verify

Retired (SYRD-687). This helper granted the agent user named ACL entries on
Eric's XDG runtime directory, whose group bits then read 0710; Qt, KDE and
the XDG spec require exactly 0700. `apply-acl` and `install-eric-user-unit`
now refuse. `retire`, run as Eric, disables the login unit they installed and
takes the agent's two entries off again. Desktop access for a Switchyard
tenant is its desktop policy: switchyard upgrade <project> --desktop-policy FILE.

Environment overrides:
  PGU_WAYLAND_GUI_USER=eric
  PGU_WAYLAND_AGENT_USER=agent
  PGU_WAYLAND_DISPLAY=wayland-0
EOF
}

uid_for_user() {
    id -u "$1"
}

gui_uid() {
    uid_for_user "$GUI_USER"
}

agent_uid() {
    uid_for_user "$AGENT_USER"
}

gui_runtime_dir() {
    printf '/run/user/%s\n' "$(gui_uid)"
}

wayland_socket() {
    printf '%s/%s\n' "$(gui_runtime_dir)" "$WAYLAND_NAME"
}

require_setfacl() {
    command -v setfacl >/dev/null 2>&1 || {
        echo "error: setfacl is required" >&2
        exit 1
    }
}

refuse_runtime_acl() {
    echo "error: $1 is retired: a named ACL on $(gui_runtime_dir) makes it 0710, and Qt, KDE and the XDG spec require 0700 (SYRD-687)." >&2
    echo "error: give the tenant a Switchyard desktop policy instead: switchyard upgrade <project> --desktop-policy FILE" >&2
    exit 1
}

# Take one entry off without widening any other: --no-mask keeps the mask as
# it is, and with no named entry left the mask goes, so the group bits are the
# group entry's again.
drop_agent_entry() {
    local path="$1"
    [[ -e "$path" ]] || return 0
    getfacl -cpn "$path" 2>/dev/null | grep -q "^user:$(agent_uid):" || return 0
    setfacl --no-mask -x "u:${AGENT_USER}" "$path"
    if ! getfacl -cpn "$path" | grep -Eq '^(user|group):[^:]+:'; then
        setfacl -x m:: "$path" 2>/dev/null || true
    fi
    echo "Removed ${AGENT_USER}'s entry from $path"
}

retire_access() {
    local unit_path
    require_setfacl
    if [[ "$(id -un)" != "$GUI_USER" ]]; then
        echo "error: retire must run as $GUI_USER" >&2
        exit 1
    fi
    unit_path="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/$SERVICE_NAME"
    if [[ -e "$unit_path" ]]; then
        systemctl --user disable --now "$SERVICE_NAME" || true
        rm -f "$unit_path" "${HOME}/.local/bin/pgu-wayland-clipboard-access"
        systemctl --user daemon-reload
        echo "Disabled and removed $unit_path"
    fi
    drop_agent_entry "$(wayland_socket)"
    drop_agent_entry "$(gui_runtime_dir)"
    stat -c '%a %n' "$(gui_runtime_dir)"
}

print_agent_env() {
    cat <<EOF
export XDG_RUNTIME_DIR=$(gui_runtime_dir)
export WAYLAND_DISPLAY=${WAYLAND_NAME}
EOF
}

apply_agent_tmux_env() {
    if [[ "$(id -un)" != "$AGENT_USER" ]]; then
        echo "error: apply-agent-tmux-env must run as $AGENT_USER" >&2
        exit 1
    fi
    tmux setenv -g XDG_RUNTIME_DIR "$(gui_runtime_dir)"
    tmux setenv -g WAYLAND_DISPLAY "$WAYLAND_NAME"
    echo "Set tmux global env: XDG_RUNTIME_DIR=$(gui_runtime_dir) WAYLAND_DISPLAY=$WAYLAND_NAME"
}

verify_access() {
    local runtime_dir socket_path
    runtime_dir="$(gui_runtime_dir)"
    socket_path="$(wayland_socket)"
    [[ -x "$runtime_dir" ]] || {
        echo "FAIL: current user cannot traverse $runtime_dir" >&2
        exit 1
    }
    [[ -r "$socket_path" && -w "$socket_path" ]] || {
        echo "FAIL: current user cannot read/write $socket_path" >&2
        exit 1
    }
    if command -v wl-paste >/dev/null 2>&1; then
        XDG_RUNTIME_DIR="$runtime_dir" WAYLAND_DISPLAY="$WAYLAND_NAME" wl-paste --list-types >/dev/null
    fi
    echo "Wayland clipboard access verified for $(id -un)"
}

case "${1:-}" in
    apply-acl|install-eric-user-unit)
        refuse_runtime_acl "$1"
        ;;
    retire)
        retire_access
        ;;
    apply-agent-tmux-env)
        apply_agent_tmux_env
        ;;
    print-agent-env)
        print_agent_env
        ;;
    verify)
        verify_access
        ;;
    -h|--help|help|"")
        usage
        ;;
    *)
        usage >&2
        exit 2
        ;;
esac
