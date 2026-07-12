# PGU Wayland Clipboard Access

PGU Claude/Codex panes run as `agent`, but Eric's KDE Plasma Wayland session
runs as `eric`. Image paste in the panes uses `wl-paste`, so the pane process
must be able to connect to Eric's Wayland socket:

- `XDG_RUNTIME_DIR=/run/user/1000`
- `WAYLAND_DISPLAY=wayland-0`
- ACL traverse access for `agent` on `/run/user/1000`
- ACL read/write access for `agent` on `/run/user/1000/wayland-0`

`/run/user/1000` is recreated on every Eric login, so the ACL must be applied
by a login-time hook, not only once by hand.

## Install Persistent ACL Hook

Run this as Eric from a desktop session:

```bash
cd /home/agent/Projects/pgu
scripts/pgu-wayland-clipboard-access.sh install-eric-user-unit
systemctl --user status pgu-agent-wayland-clipboard.service
```

That installs `~/.config/systemd/user/pgu-agent-wayland-clipboard.service`.
The installer also copies the helper to
`~/.local/bin/pgu-wayland-clipboard-access`, so the unit does not depend on a
repo checkout path. The unit waits for `/run/user/1000/wayland-0` and then
runs:

```bash
setfacl -m u:agent:x /run/user/1000
setfacl -m u:agent:rw /run/user/1000/wayland-0
```

Manual one-shot repair, also as Eric:

```bash
cd /home/agent/Projects/pgu
scripts/pgu-wayland-clipboard-access.sh apply-acl --wait
```

## Set Pane Environment

Run this as `agent` in the tmux server that owns the pgu panes:

```bash
cd /home/agent/Projects/pgu
scripts/pgu-wayland-clipboard-access.sh apply-agent-tmux-env
tmux show-environment -g XDG_RUNTIME_DIR
tmux show-environment -g WAYLAND_DISPLAY
```

For already-running Claude/Codex panes, restart the CLI process in each pane so
it inherits the updated tmux environment. New panes inherit it automatically.

Equivalent manual commands:

```bash
tmux setenv -g XDG_RUNTIME_DIR /run/user/1000
tmux setenv -g WAYLAND_DISPLAY wayland-0
```

## Verify

From the `agent` user:

```bash
XDG_RUNTIME_DIR=/run/user/1000 WAYLAND_DISPLAY=wayland-0 wl-paste --list-types
```

Then Eric should copy an image with Plasma/Spectacle and paste into a pgu pane
with Ctrl+V. The previous error, `clipboard unavailable` or `No image found in
clipboard`, should be gone.

Reboot verification is the acceptance test:

```bash
systemctl --user status pgu-agent-wayland-clipboard.service
getfacl /run/user/1000 /run/user/1000/wayland-0 | sed -n '1,80p'
```

Expected ACL entries after Eric logs in:

```text
user:agent:--x
user:agent:rw-
```

The first entry is on `/run/user/1000`; the second is on
`/run/user/1000/wayland-0`.
