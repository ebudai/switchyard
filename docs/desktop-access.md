# Screenshot clipboard access before project startup

Switchyard requires an explicit desktop policy before it starts a role. A
Wayland grant allows the tenant account to act as a client of the selected GUI
session, with the capabilities its compositor exposes. It is not restricted to
clipboard operations. Choose it only with consent for that tenant and GUI owner.

## Provisioning on a desktop

`switchyard new` generates the policy. It asks logind which account owns an
active Wayland session, and offers a choice:

```
switchyard: desktop-owner is signed into this host's desktop. Switchyard can
give example scoped access to that session, which is what lets a role paste a
screenshot.
  1) desktop: screenshots and clipboard through desktop-owner's session (recommended) [default]
  2) headless: no screenshots or clipboard
Desktop access [desktop]:
```

Choosing `desktop` records that owner's approval once, in root-owned
`/etc/switchyard/desktop-approval.json`, and writes a policy scoped to this
project and this tenant. Later projects on the same host read that record and
are provisioned without asking again. The generated policy is the same shape an
operator would have written and is checked by the same validator; nothing is
installed that a hand-written policy could not have said.

The approval is honoured only for the account logind reports as owning the
active desktop. A record naming any other account -- including a tenant --
matches nobody and grants nothing; provisioning asks the human instead. `--yes`
still never grants desktop access that no approval covers.

The GUI owner comes from logind, not from who ran the command. A host with no
active Wayland session is headless and is told so; a host with several active
desktops stops and names them, because choosing between somebody's desktops is
not a decision provisioning may make quietly:

```sh
switchyard new --desktop-gui-user desktop-owner
```

## Headless, and explicit policies

For a project without desktop clipboard access, on a host with or without a
compositor:

```sh
switchyard new --headless
```

`--desktop-policy` remains the advanced import path for an explicit policy or
for automation, with the same validation and the same tenant/project mismatch
refusal. `--desktop-policy headless` still means headless. Passing both
`--headless` and `--desktop-policy` is refused rather than given a precedence
rule. Existing unconfigured projects remain inspectable through
attach/status/upgrade; starting new role processes requires a policy. Legacy PGU
account names do not confer consent on a new tenant.

For approved Wayland access, provide a JSON file:

```json
{
  "mode": "wayland",
  "project": "example",
  "tenant_user": "example-worker",
  "gui_user": "desktop-owner",
  "wayland_display": "auto",
  "consent": {
    "approved": true,
    "by": "approving operator",
    "at": "2026-09-05T22:27:00Z",
    "reference": "the recorded approval for these accounts"
  }
}
```

`auto` accepts exactly one socket owned by the selected GUI user in the runtime
reported by logind, and is the recommended value. If several sockets exist, use
the selected socket basename such as `wayland-0`. An active Wayland session is
required.

A socket name is not stable across logins: after plasma-login-manager 6.8.0 the
greeter took `wayland-0` and the owner's KWin moved to `wayland-1` (SYRD-592).
So when a pinned name is absent from the owner's runtime, the GUI service relays
to the one Wayland socket that is there instead, with the same owner, session and
symlink checks, and logs that it did. It never chooses among several: with two
or more live sockets and the pinned name absent it relays to none and logs the
names. A pinned name that is present -- even beside another socket -- is always
the one used, and the socket used only in its absence is no longer relayed to
when the pinned name returns. The project and tenant
must match the target configuration. Missing, rejected or malformed consent
stops setup without a grant.

The presentation window follows the same rule for the display it opens on
(SYRD-621, `scripts/wayland_selection.py`). An explicit `HOST_WAYLAND_DISPLAY`,
then `TEAM_LAUNCHER_WAYLAND_DISPLAY`, comes first, and is held to the same proof
as every other choice -- a live socket the desktop account owns in its own
runtime directory, not a link -- or refused by name, never replaced. Otherwise a plain
`switchyard <project>` from the desktop account's own session uses that
session's `WAYLAND_DISPLAY` when it is a live socket the account owns in its
runtime directory, and else the account's only live Wayland socket; several are
refused by name, as is none. A launch that crosses from root never reads the
caller's `WAYLAND_DISPLAY`. When the terminal still exits at once, the report
names the display it was given and the sockets that are live.

```sh
switchyard new --desktop-policy /path/to/approved-policy.json
switchyard upgrade example --desktop-policy /path/to/approved-policy.json
switchyard example
```

Use the existing operator invocation for `new`/`upgrade`. Initial access setup
requires the GUI owner or root; ordinary tenant starts only validate already
approved access. No desktop setup command is required after starting the project.
The supported legacy `team-launcher ... new/upgrade --desktop-policy` accepts the
same input. Reprovisioning retains `desktop-policy.json` next to the generated
launcher config; upgrade preserves the embedded `desktop_access` policy. These
are scoped inputs, not a transferable blanket approval for new accounts.

## Installation and access boundary

Before authentication or the first role process, setup discovers account IDs,
GUI runtime and active session/socket. The GUI owner installs a policy and a
project/tenant-specific systemd user unit:

- `~/.config/switchyard/desktop-access/switchyard-desktop-<gui-uid>-<tenant-uid>-<project>.json`
- `~/.config/systemd/user/switchyard-desktop-<gui-uid>-<tenant-uid>-<project>.service`
- `~/.local/lib/switchyard/desktop-access/switchyard-desktop-<gui-uid>-<tenant-uid>-<project>.py`

The helper is copied from the reviewed release, so a private source checkout or
temporary exported release is not a runtime dependency. Existing unmanaged units
are refused. Setup verifies systemd activation and the service's readiness
receipt before reporting success. An installation failure rolls back its
exposure entries and managed files.

### The exposure, not the runtime directory (SYRD-687)

The desktop owner's XDG runtime directory (`/run/user/<uid>`) takes no entry for
any tenant and stays exactly `0700`. Qt, KDE and the XDG spec require that:
before SYRD-687 the grant put a named `--x` entry there per tenant, the ACL's
mask showed in the group bits as `0710`, and KDE logged `QStandardPaths: wrong
permissions on runtime directory /run/user/1000, 0710 instead of 0700`.

Each grant is exposed instead through one directory of its own:

| path | owner | mode and ACL | the tenant may |
|---|---|---|---|
| `/var/lib/switchyard/desktop/` | root | `0755` | traverse |
| `<gui uid>-<tenant uid>-<project>/` | GUI owner | `0700` + `u:<tenant>:--x` | traverse |
| `.../wayland-0` | GUI owner | relay socket | connect, if its uid is the tenant's |
| `.../ready.json` | GUI owner | `0644` | read |
| `.../paste/` | GUI owner | `0700` + `u:<tenant>:r-x` | list, open hand-offs |

Root makes the directory once, at `new` or `upgrade`, and gives it to the
desktop owner. Every component from `/` down is opened by descriptor, without
following a link, and must be root's and writable by nobody else; the desktop
owner's own directory must be the owner's and writable by nobody else. A tenant
or the owner therefore cannot plant one or point one elsewhere. An upgrade run
as the desktop owner without root, before root has made it, is refused with that
reason.

The owner's service keeps a relay on `wayland-0`. It serves a connection only
when its peer (`SO_PEERCRED`) is the tenant's uid -- every other uid, the
owner's included, is closed unanswered and logged once -- and relays it, file
descriptors included, to the compositor socket the policy selects at that
moment. Only the Wayland protocol crosses: the owner's session bus and the rest
of its runtime directory stay out of reach. A compositor that recreates or
renames its socket at the next login is followed without the tenant's
`WAYLAND_DISPLAY` changing. Stopping the service closes the relay and every
connection through it.

The service starts with the user manager and selects the approved session's
socket every five seconds; no compositor-specific service name is hard-coded. A
missing desktop leaves it pending, and the receipt says why; clipboard-enabled
role startup is blocked meanwhile and quotes that reason. GUI logout is not
required to test an upgrade and should not interrupt working sessions. There is
no xhost grant, Xauthority copy, default ACL, recursive permission change, or
shared private credential.

The receipt records the approved policy digest and whether the relay is ready.
The tenant cannot authorize itself by editing its own config: startup verifies
the exposure's ownership, the matching receipt, the relay socket's owner, and a
Wayland round trip through it as the tenant (one `wl_display.sync`). The probe
reads no clipboard content. Missing ACL tools, account/runtime, permission,
session, socket or active unit produces a pre-launch failure with its cause.
While it is waiting, the service also logs why every five seconds (`journalctl
--user -u switchyard-desktop-<gui-uid>-<tenant-uid>-<project>.service`, as the
GUI owner).

For Wayland roles and their authentication commands, `WAYLAND_DISPLAY` is the
absolute path of the grant's relay socket. `XDG_RUNTIME_DIR` remains the tenant's logind runtime;
the bus address also names that tenant's runtime. `DISPLAY` and `XAUTHORITY` are
removed so inaccessible inherited X11 settings cannot win transport selection.
No tmux global environment is changed. Headless roles remove display/clipboard
variables without granting GUI access.

## Handing over one image

On a compositor with no data-control protocol, such as GNOME's Mutter, no
provider in a role pane can read the desktop clipboard even with this grant.
The desktop owner can still hand one image to a role:

```sh
switchyard paste-image <project> <role>
```

It runs as the GUI owner named in this project's installed policy, and as
nobody else. It writes the image into the exposure's `paste/` directory, where
the tenant may list and read each file, and write nothing. A project not yet
upgraded onto its exposure is refused, with the upgrade that moves it. The project's listener tells the role where the
image is once its pane is idle. This GUI-owned service removes the image 15
minutes after the copy, and revoking the grant removes anything still there.
See [syrd-247-image-handoff.md](syrd-247-image-handoff.md).

## Existing projects, reapplication and rollback

`switchyard upgrade` installs the same policy/service and verifies readiness
without restarting any role. Existing process environments cannot be changed in
place. Preserve their work and use the existing per-role reload command at an
idle checkpoint; desktop-configured reloads reject busy panes even with force.
Attach/start preserves existing live sessions. If a compositor changes the socket
basename, the relay follows it; no process needs a reload for that. A role
started before its project's upgrade onto the exposure still has the legacy
socket path; reload it at an idle checkpoint to move it to the relay.

To revoke this project's desktop grant through the same supported lifecycle:

```sh
switchyard upgrade example --desktop-policy headless
```

This disables/removes only its managed GUI unit, policy/helper and receipt,
closes the relay and every connection through it, withdraws the tenant's entries
on the exposure and removes it, and retires any legacy entry the grant still
held. Other tenants and other projects sharing the same approved account are
retained. `switchyard teardown` withdraws the tenant's grant on any desktop the
same way, while the tenant account still exists. A different Wayland policy similarly replaces the old
scoped grant through upgrade. An unavailable GUI user manager may need to be
restored before a previous grant can be revoked; setup must not claim revocation
while its persistent unit can still reapply the grant.

## Hosts already at 0710: migration and repair

The legacy grant is installed state, and `switchyard upgrade <project>` moves
each project off it. The upgrade makes the exposure, replaces the owner's helper
and restarts its service, and has the tenant verify the relay. Only then does it
take the project's legacy entries off the owner's runtime directory: the
tenant's `--x` there and on the old `switchyard-paste` directory, and its `rw-`
on a Wayland socket there -- each only while it is still the value Switchyard
set -- or, where the legacy receipt recorded an entry from before the grant,
that entry is put back. The mask only narrows, and with no named entry left the
ACL is minimal again, so the directory reads `0700`. Every other entry stays as
it is. If the cutover cannot complete, the previous helper is put back and its
service restarted, the legacy entries untouched, and the new exposure withdrawn.

A tenant with another project still on the legacy helper keeps its shared entry
until that project is upgraded too, and the upgrade says so. The upgrade prints
how the runtime directory reads afterwards, and what still holds it off `0700`.

As the desktop owner, the repair report lists every Switchyard grant on that
desktop and what keeps the runtime directory off `0700`:

```sh
python3 /opt/switchyard/current/scripts/desktop_access.py repair
python3 /opt/switchyard/current/scripts/desktop_access.py repair --apply
```

`--apply` withdraws a grant whose project is no longer registered (a project torn
down before SYRD-687 left its grant and its owner's unit running), and retires
legacy entries an upgraded grant left behind. A grant still on the legacy helper
is named with the upgrade that moves it; an entry no Switchyard grant accounts
for is named and left. It exits 0 when the directory is `0700`. The legacy PGU
helper `scripts/pgu-wayland-clipboard-access.sh` no longer grants; its `retire`
command removes what it set.

## Verification

`python3 tests/desktop_wayland_exposure_test.py` runs the grant as real, separate
accounts in a user namespace and chroot, with a real KWin (`kwin_wayland
--virtual`) and a real clipboard: main's legacy grant and its `0710`, the
upgrade through `configure_project_desktop`, two tenants, the relay's peer
check, a socket rename, a new login, a failed cutover's rollback, revocation,
teardown, repair, and a Qt-style `0700` check. logind and systemd are stand-ins
that run each unit's real ExecStart. `python3 tests/desktop_access_test.py`
exercises the exported helper's install, relay, descriptor passing and failed
install with fixture accounts. The exported-release startup fixture
uses the actual new-project/configuration path and executes a harmless first role
process to verify its environment; account creation, board services and GUI
installation are fixtures there. Existing launcher tests explicitly choose
headless mode when desktop access is irrelevant.

Live acceptance is separate: after approved deployment, copy a known screenshot,
reload only an idle affected Codex role, and verify Ctrl+V creates an image
attachment without the X11 timeout. `wl-paste` alone does not satisfy acceptance.
Do not inspect unrelated clipboard data. Verify again after a naturally occurring
GUI login/socket recreation, without logging out working roles just for a test.
