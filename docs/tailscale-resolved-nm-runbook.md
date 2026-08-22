# Tailscale NetworkManager/systemd-resolved Health

PGU-583 fixes the host DNS ownership contract that Tailscale expects on Linux:
NetworkManager should delegate DNS to `systemd-resolved`, and
`/etc/resolv.conf` should point at resolved's stub resolver. MagicDNS currently
resolves on the PGU host, but Tailscale reports the NetworkManager/resolved
health warning because the configuration is fragile across NetworkManager
rewrites and reboots.

The Tailscale Linux DNS guidance says the NetworkManager + systemd-resolved
configuration should use `/run/systemd/resolve/stub-resolv.conf` as
`/etc/resolv.conf`; NetworkManager then defers resolver management to
systemd-resolved.

## Check

This command is read-only:

```bash
cd /home/agent/Projects/pgu
scripts/tailscale-resolved-nm-health.sh --check
```

It verifies:

- `/etc/resolv.conf` is a symlink to
  `/run/systemd/resolve/stub-resolv.conf`
- NetworkManager has `[main] dns=systemd-resolved`
- `systemd-resolved`, `NetworkManager`, and `tailscaled` are active
- MagicDNS resolves
- ordinary DNS has at least one passing control from
  `TAILSCALE_INTERNET_PROBE_HOSTS` (default: `github.com cloudflare.com`)
- `tailscale status` no longer reports the resolved/NetworkManager warning

## Apply

Review the command plan:

```bash
scripts/tailscale-resolved-nm-health.sh --print-root-commands
```

Then Eric can run the supervised root repair:

```bash
cd /home/agent/Projects/pgu
sudo scripts/tailscale-resolved-nm-health.sh --apply
```

Equivalent manual commands:

```bash
set -euo pipefail
systemctl is-active --quiet systemd-resolved
systemctl is-enabled --quiet systemd-resolved
test -e /run/systemd/resolve/stub-resolv.conf
sudo install -d -m 0755 /etc/NetworkManager/conf.d
printf '%s\n' '[main]' 'dns=systemd-resolved' | sudo tee /etc/NetworkManager/conf.d/90-pgu-systemd-resolved.conf >/dev/null
test ! -e /etc/resolv.conf || test -L /etc/resolv.conf || sudo cp -a /etc/resolv.conf "/etc/resolv.conf.pgu-pre-resolved-nm.$(date +%Y%m%d%H%M%S)"
sudo ln -sfn /run/systemd/resolve/stub-resolv.conf /etc/resolv.conf
sudo systemctl restart systemd-resolved
sudo systemctl restart NetworkManager
sudo systemctl restart tailscaled
```

## Verify After Apply

```bash
readlink -f /etc/resolv.conf
grep -R 'dns[[:space:]]*=[[:space:]]*systemd-resolved' /etc/NetworkManager/NetworkManager.conf /etc/NetworkManager/conf.d
getent hosts pixel-8
getent hosts machine.tail0af98d.ts.net
getent hosts github.com || getent hosts cloudflare.com
tailscale status
scripts/tailscale-resolved-nm-health.sh --check
```

Expected results:

- `readlink -f /etc/resolv.conf` prints
  `/run/systemd/resolve/stub-resolv.conf`
- both MagicDNS probes resolve
- at least one ordinary DNS control resolves. This intentionally does not use
  the RFC-2606 `example.*` names because the PGU host's current router returns
  NXDOMAIN for them while resolving normal internet names.
- `tailscale status` does not show the NetworkManager/systemd-resolved health
  warning

## Roll Back

If the repair causes a resolver regression, restore the previous
`/etc/resolv.conf` strategy and remove the PGU NetworkManager override. The
apply path creates a timestamped backup before replacing a regular
`/etc/resolv.conf`; use the newest matching backup if one exists:

```bash
latest_backup="$(ls -1t /etc/resolv.conf.pgu-pre-resolved-nm.* 2>/dev/null | head -1)"
test -z "$latest_backup" || { sudo rm -f /etc/resolv.conf; sudo cp -a "$latest_backup" /etc/resolv.conf; }
sudo rm -f /etc/NetworkManager/conf.d/90-pgu-systemd-resolved.conf
sudo systemctl restart systemd-resolved
sudo systemctl restart NetworkManager
sudo systemctl restart tailscaled
```

Then rerun the verification commands and capture `resolvectl status`,
`NetworkManager --print-config`, and `tailscale status` for diagnosis.
