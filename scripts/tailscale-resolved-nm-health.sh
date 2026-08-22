#!/usr/bin/env bash
set -euo pipefail

readonly RESOLV_CONF="${TAILSCALE_RESOLV_CONF:-/etc/resolv.conf}"
readonly STUB_RESOLV_CONF="${TAILSCALE_STUB_RESOLV_CONF:-/run/systemd/resolve/stub-resolv.conf}"
readonly NM_CONFIG_FILE="${TAILSCALE_NM_CONFIG_FILE:-/etc/NetworkManager/NetworkManager.conf}"
readonly NM_CONFIG_DIR="${TAILSCALE_NM_CONFIG_DIR:-/etc/NetworkManager/conf.d}"
readonly NM_SYSTEMD_RESOLVED_CONFIG="${TAILSCALE_NM_SYSTEMD_RESOLVED_CONFIG:-/etc/NetworkManager/conf.d/90-pgu-systemd-resolved.conf}"
readonly SKIP_LIVE_PROBES="${TAILSCALE_SKIP_LIVE_PROBES:-0}"
readonly MAGICDNS_HOSTS="${TAILSCALE_MAGICDNS_HOSTS:-pixel-8 machine.tail0af98d.ts.net}"
readonly INTERNET_PROBE_HOSTS="${TAILSCALE_INTERNET_PROBE_HOSTS:-github.com cloudflare.com}"

usage() {
    cat <<EOF
Usage: scripts/tailscale-resolved-nm-health.sh <--check|--print-root-commands|--apply>

Checks or repairs the NetworkManager/systemd-resolved ownership contract that
Tailscale expects for MagicDNS on Linux.

Environment overrides for tests:
  TAILSCALE_RESOLV_CONF
  TAILSCALE_STUB_RESOLV_CONF
  TAILSCALE_NM_CONFIG_FILE
  TAILSCALE_NM_CONFIG_DIR
  TAILSCALE_NM_SYSTEMD_RESOLVED_CONFIG
  TAILSCALE_INTERNET_PROBE_HOSTS="github.com cloudflare.com"
  TAILSCALE_SKIP_LIVE_PROBES=1
EOF
}

ok() {
    printf 'OK: %s\n' "$*"
}

warn() {
    printf 'WARN: %s\n' "$*" >&2
}

fail_line() {
    printf 'FAIL: %s\n' "$*" >&2
}

shell_quote() {
    printf '%q' "$1"
}

getent_each_commands() {
    local host
    for host in $1; do
        printf 'getent hosts %s\n' "$(shell_quote "$host")"
    done
}

getent_any_command() {
    local host separator="" command=""
    for host in $1; do
        command="${command}${separator}getent hosts $(shell_quote "$host")"
        separator=" || "
    done
    if [[ -z "$command" ]]; then
        command=": # no ordinary DNS probe hosts configured"
    fi
    printf '%s\n' "$command"
}

resolv_conf_points_to_stub() {
    local target
    if [[ ! -L "$RESOLV_CONF" ]]; then
        fail_line "$RESOLV_CONF is not a symlink"
        return 1
    fi

    target="$(readlink "$RESOLV_CONF")"
    if [[ "$target" == "$STUB_RESOLV_CONF" ]]; then
        ok "$RESOLV_CONF points to $STUB_RESOLV_CONF"
        return 0
    fi

    if [[ -e "$RESOLV_CONF" && -e "$STUB_RESOLV_CONF" ]] &&
        [[ "$(readlink -f "$RESOLV_CONF")" == "$(readlink -f "$STUB_RESOLV_CONF")" ]]; then
        ok "$RESOLV_CONF resolves to $STUB_RESOLV_CONF"
        return 0
    fi

    fail_line "$RESOLV_CONF points to $target, expected $STUB_RESOLV_CONF"
    return 1
}

nm_config_files() {
    [[ -f "$NM_CONFIG_FILE" ]] && printf '%s\n' "$NM_CONFIG_FILE"
    if [[ -d "$NM_CONFIG_DIR" ]]; then
        find "$NM_CONFIG_DIR" -maxdepth 1 -type f -name '*.conf' -print | sort
    fi
}

main_dns_setting_from_file() {
    local file="$1"
    awk '
        {
            sub(/[;#].*/, "", $0)
            gsub(/^[ \t]+|[ \t]+$/, "", $0)
            if ($0 ~ /^\[.*\]$/) {
                in_main = ($0 == "[main]")
                next
            }
            if (in_main && $0 ~ /^dns[ \t]*=/) {
                value = $0
                sub(/^dns[ \t]*=/, "", value)
                gsub(/^[ \t]+|[ \t]+$/, "", value)
                print value
            }
        }
    ' "$file"
}

networkmanager_uses_systemd_resolved() {
    local file setting=""
    while IFS= read -r file; do
        [[ -n "$file" ]] || continue
        while IFS= read -r found; do
            setting="$found"
        done < <(main_dns_setting_from_file "$file")
    done < <(nm_config_files)

    if [[ "$setting" == "systemd-resolved" ]]; then
        ok "NetworkManager [main] dns=systemd-resolved"
        return 0
    fi

    if [[ -z "$setting" ]]; then
        fail_line "NetworkManager has no [main] dns=systemd-resolved setting"
    else
        fail_line "NetworkManager effective [main] dns=$setting, expected systemd-resolved"
    fi
    return 1
}

probe_getent() {
    local host="$1"
    if getent hosts "$host" >/dev/null 2>&1; then
        ok "DNS resolves $host"
    else
        warn "DNS probe failed for $host"
    fi
}

probe_internet_dns() {
    local host failed_hosts=() total=0 success=0
    for host in $INTERNET_PROBE_HOSTS; do
        total=$((total + 1))
        if getent hosts "$host" >/dev/null 2>&1; then
            ok "ordinary DNS resolves $host"
            success=$((success + 1))
        else
            failed_hosts+=("$host")
        fi
    done

    [[ "$total" -gt 0 ]] || return 0
    if [[ "$success" -eq 0 ]]; then
        warn "ordinary DNS probes failed for all controls: ${failed_hosts[*]}"
    elif [[ "${#failed_hosts[@]}" -gt 0 ]]; then
        ok "ordinary DNS has a passing control; inconclusive failed controls: ${failed_hosts[*]}"
    fi
}

run_live_probes() {
    [[ "$SKIP_LIVE_PROBES" == "1" ]] && return 0

    if command -v systemctl >/dev/null 2>&1; then
        systemctl is-active --quiet systemd-resolved && ok "systemd-resolved is active" || warn "systemd-resolved is not active"
        systemctl is-active --quiet NetworkManager && ok "NetworkManager is active" || warn "NetworkManager is not active"
        systemctl is-active --quiet tailscaled && ok "tailscaled is active" || warn "tailscaled is not active"
    fi

    if command -v getent >/dev/null 2>&1; then
        local host
        for host in $MAGICDNS_HOSTS; do
            probe_getent "$host"
        done
        probe_internet_dns
    fi

    if command -v tailscale >/dev/null 2>&1; then
        if tailscale status 2>&1 | grep -qi 'resolved.*NetworkManager'; then
            warn "tailscale status still reports a resolved/NetworkManager health warning"
        else
            ok "tailscale status has no resolved/NetworkManager health warning"
        fi
    fi
}

resolved_stub_ready() {
    if ! command -v systemctl >/dev/null 2>&1; then
        fail_line "systemctl is required to verify systemd-resolved before changing $RESOLV_CONF"
        return 1
    fi
    if ! systemctl is-active --quiet systemd-resolved; then
        fail_line "systemd-resolved is not active; refusing to repoint $RESOLV_CONF"
        return 1
    fi
    if ! systemctl is-enabled --quiet systemd-resolved; then
        fail_line "systemd-resolved is not enabled; refusing to repoint $RESOLV_CONF"
        return 1
    fi
    if [[ ! -e "$STUB_RESOLV_CONF" ]]; then
        fail_line "$STUB_RESOLV_CONF does not exist; refusing to repoint $RESOLV_CONF"
        return 1
    fi
    ok "systemd-resolved is enabled, active, and $STUB_RESOLV_CONF exists"
}

check_health() {
    local status=0
    resolv_conf_points_to_stub || status=1
    networkmanager_uses_systemd_resolved || status=1
    run_live_probes
    return "$status"
}

print_root_commands() {
    local nm_dir nm_config resolv_conf stub_conf magic_probe_commands internet_probe_command
    nm_dir="$(dirname "$NM_SYSTEMD_RESOLVED_CONFIG")"
    nm_config="$(shell_quote "$NM_SYSTEMD_RESOLVED_CONFIG")"
    resolv_conf="$(shell_quote "$RESOLV_CONF")"
    stub_conf="$(shell_quote "$STUB_RESOLV_CONF")"
    magic_probe_commands="$(getent_each_commands "$MAGICDNS_HOSTS")"
    internet_probe_command="$(getent_any_command "$INTERNET_PROBE_HOSTS")"

    cat <<EOF
# Review, then run as root from the PGU repo checkout.
set -euo pipefail
systemctl is-active --quiet systemd-resolved
systemctl is-enabled --quiet systemd-resolved
test -e $stub_conf
sudo install -d -m 0755 $(shell_quote "$nm_dir")
printf '%s\n' '[main]' 'dns=systemd-resolved' | sudo tee $nm_config >/dev/null
test ! -e $resolv_conf || test -L $resolv_conf || sudo cp -a $resolv_conf "${resolv_conf}.pgu-pre-resolved-nm.\$(date +%Y%m%d%H%M%S)"
sudo ln -sfn $stub_conf $resolv_conf
sudo systemctl restart systemd-resolved
sudo systemctl restart NetworkManager
sudo systemctl restart tailscaled

# Verify ownership and DNS behavior.
readlink -f $resolv_conf
grep -R 'dns[[:space:]]*=[[:space:]]*systemd-resolved' /etc/NetworkManager/NetworkManager.conf /etc/NetworkManager/conf.d
$magic_probe_commands
$internet_probe_command
tailscale status
scripts/tailscale-resolved-nm-health.sh --check
EOF
}

require_root() {
    if [[ "$(id -u)" != "0" ]]; then
        printf 'ERROR: --apply must be run as root.\n' >&2
        exit 1
    fi
}

apply_fix() {
    require_root

    local tmp_config
    resolved_stub_ready
    install -d -m 0755 "$(dirname "$NM_SYSTEMD_RESOLVED_CONFIG")"
    tmp_config="$(mktemp)"
    printf '%s\n' '[main]' 'dns=systemd-resolved' >"$tmp_config"
    install -m 0644 "$tmp_config" "$NM_SYSTEMD_RESOLVED_CONFIG"
    rm -f "$tmp_config"

    if [[ -e "$RESOLV_CONF" && ! -L "$RESOLV_CONF" ]]; then
        cp -a "$RESOLV_CONF" "$RESOLV_CONF.pgu-pre-resolved-nm.$(date +%Y%m%d%H%M%S)"
    fi
    ln -sfn "$STUB_RESOLV_CONF" "$RESOLV_CONF"
    systemctl restart systemd-resolved
    systemctl restart NetworkManager
    systemctl restart tailscaled
    check_health
}

main() {
    case "${1:-}" in
        --check)
            check_health
            ;;
        --print-root-commands)
            print_root_commands
            ;;
        --apply)
            apply_fix
            ;;
        -h|--help)
            usage
            ;;
        *)
            usage >&2
            exit 1
            ;;
    esac
}

main "$@"
