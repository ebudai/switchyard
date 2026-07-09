#!/usr/bin/env bash
set -euo pipefail

ensure_agent_desktop_env() {
    local agent_runtime wayland_path

    agent_runtime="/run/user/$(id -u)"
    if [[ -d "$agent_runtime" ]]; then
        export XDG_RUNTIME_DIR="$agent_runtime"
        if [[ -z "${DBUS_SESSION_BUS_ADDRESS:-}" && -S "$agent_runtime/bus" ]]; then
            export DBUS_SESSION_BUS_ADDRESS="unix:path=$agent_runtime/bus"
        fi
    fi

    if [[ -n "${WAYLAND_DISPLAY:-}" ]]; then
        if [[ "$WAYLAND_DISPLAY" != /* ]]; then
            wayland_path="/run/user/1000/$WAYLAND_DISPLAY"
            if [[ -S "$wayland_path" ]]; then
                export WAYLAND_DISPLAY="$wayland_path"
            fi
        fi
        return 0
    fi

    wayland_path="/run/user/1000/wayland-0"
    if [[ -S "$wayland_path" ]]; then
        export WAYLAND_DISPLAY="$wayland_path"
    fi
}
