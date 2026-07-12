#!/usr/bin/env bash
set -euo pipefail

GUI_USER="${PGU_WAYLAND_GUI_USER:-eric}"
AGENT_USER="${PGU_WAYLAND_AGENT_USER:-agent}"
WAYLAND_NAME="${PGU_WAYLAND_DISPLAY:-wayland-0}"
SERVICE_NAME="${PGU_WAYLAND_SERVICE_NAME:-pgu-agent-wayland-clipboard.service}"

usage() {
    cat <<'EOF'
Usage:
  pgu-wayland-clipboard-access.sh apply-acl [--wait]
  pgu-wayland-clipboard-access.sh install-eric-user-unit
  pgu-wayland-clipboard-access.sh apply-agent-tmux-env
  pgu-wayland-clipboard-access.sh print-agent-env
  pgu-wayland-clipboard-access.sh verify

Restores pgu agent-pane image paste on Eric's Wayland session by granting the
agent user access to Eric's Wayland socket and exporting the matching pane env.

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

wait_for_socket() {
    local socket_path
    socket_path="$(wayland_socket)"
    for _ in $(seq 1 120); do
        [[ -S "$socket_path" ]] && return 0
        sleep 0.5
    done
    echo "error: timed out waiting for $socket_path" >&2
    return 1
}

apply_acl() {
    local wait_flag="${1:-}"
    local runtime_dir socket_path
    require_setfacl
    if [[ "$wait_flag" == "--wait" ]]; then
        wait_for_socket
    fi
    runtime_dir="$(gui_runtime_dir)"
    socket_path="$(wayland_socket)"
    [[ -d "$runtime_dir" ]] || {
        echo "error: $runtime_dir does not exist; is $GUI_USER logged into Plasma?" >&2
        exit 1
    }
    [[ -S "$socket_path" ]] || {
        echo "error: $socket_path does not exist; is the Wayland session up?" >&2
        exit 1
    }
    setfacl -m "u:${AGENT_USER}:x" "$runtime_dir"
    setfacl -m "u:${AGENT_USER}:rw" "$socket_path"
    echo "Granted ${AGENT_USER} access to $socket_path"
}

install_eric_user_unit() {
    local install_path unit_dir unit_path
    if [[ "$(id -un)" != "$GUI_USER" ]]; then
        echo "error: install-eric-user-unit must run as $GUI_USER" >&2
        exit 1
    fi
    install_path="${HOME}/.local/bin/pgu-wayland-clipboard-access"
    mkdir -p "$(dirname "$install_path")"
    install -m 0755 "$(realpath "$0")" "$install_path"
    unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    unit_path="$unit_dir/$SERVICE_NAME"
    mkdir -p "$unit_dir"
    cat >"$unit_path" <<EOF
[Unit]
Description=Grant ${AGENT_USER} access to ${GUI_USER}'s Wayland clipboard socket for pgu panes
After=graphical-session.target plasma-kwin_wayland.service
PartOf=graphical-session.target

[Service]
Type=oneshot
Environment=PGU_WAYLAND_GUI_USER=${GUI_USER}
Environment=PGU_WAYLAND_AGENT_USER=${AGENT_USER}
Environment=PGU_WAYLAND_DISPLAY=${WAYLAND_NAME}
ExecStart=${install_path} apply-acl --wait
RemainAfterExit=yes

[Install]
WantedBy=graphical-session.target default.target
EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$SERVICE_NAME"
    echo "Installed $install_path and started $unit_path"
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
    apply-acl)
        apply_acl "${2:-}"
        ;;
    install-eric-user-unit)
        install_eric_user_unit
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
