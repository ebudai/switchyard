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
active Wayland session is headless and is told so. Interactively, `switchyard new`
offers to install headless (the default, which `--yes` takes).

A host with several active desktops is asked which one, with the other questions
and with no default (SYRD-778). Choosing between somebody's desktops is not a
decision provisioning may make quietly, so Enter alone answers nothing. The flag
answers the question instead:

```sh
switchyard new --desktop-gui-user desktop-owner
```

A named account that is not signed in is refused. So is a run nobody can answer
(no terminal and no `--yes`), and a `--yes` run, because there is no default to
take.

## The agent account's browser

A project's provider CLIs run as its agent account, so a browser sign-in opens
whatever that account's link handlers name, not the desktop owner's choice.
Once the desktop owner has approved desktop access, `switchyard new` copies
that owner's default browser into the account it provisions, before the first
provider sign-in:

```
switchyard: browser: example-agent opens links with firefox.desktop, desktop-owner's default browser, set once now: a later change of desktop-owner's default is not copied. Change it deliberately with `sudo -u example-agent -H xdg-settings set default-web-browser NAME.desktop`.
```

- **What is copied.** The owner's own answer for `x-scheme-handler/https`,
  `x-scheme-handler/http` and `text/html`, asked as the owner and never written.
  All three must name the same application, installed where every account can
  see it (`/usr/share/applications` and the like). It is set as the agent
  account itself, with `xdg-settings`: the same three handlers, plus KDE's
  `BrowserApplication` where KDE's config tool is installed.
- **Once, at creation.** Upgrade, launch and resume never copy a browser, and a
  later change of the owner's default is not followed. An existing project is
  never changed by installing or upgrading Switchyard.
- **Firefox when nobody chose.** When the account is new and no owner's browser
  is copied, it is given `firefox.desktop` the same way, as the account, with
  its own settings saved first and put back if any step is refused. That
  covers:
  - a headless project, or one without an approved desktop owner;
  - an owner whose handlers disagree, or name an application only that owner
    can see;
  - a refused write.

  Without this, an account that names no browser of its own falls back to the
  system's desktop-entry cache. That cache lists applications alphabetically,
  so on a host with Chrome installed, Chrome comes first:

  ```
  switchyard: browser: example-agent opens links with firefox.desktop, the Firefox default, because nobody chose a browser for it. Change it deliberately with `sudo -u example-agent -H xdg-settings set default-web-browser NAME.desktop`.
  ```

  If Firefox is not installed where the account can open it, nothing is
  written, and the line names the browser the account will open instead.
- **Left alone, and said so:**
  - an existing account reused by `switchyard new` that already names its own
    browser;
  - an existing account that names none, when there is no owner's browser to
    copy. The Firefox default is for new accounts only.

  In each case provisioning goes on.

To change an existing agent account's browser, deliberately, run as root:

```sh
sudo -u example-agent -H xdg-settings set default-web-browser firefox.desktop
# and on KDE, its own setting too:
sudo -u example-agent -H env XDG_CURRENT_DESKTOP=KDE KDE_SESSION_VERSION=6 xdg-settings set default-web-browser firefox.desktop
```

## Signing in during `switchyard new`

The agent account's own browser is not used for the provider sign-in. A browser
the agent opens cannot come to the front of the owner's desktop: KWin raises a
window only for a client the owner has just used, so it opens behind the
terminal and closes with the sign-in step. That is what happened on test11.

So each sign-in step runs with the agent's browser launch turned off.
`BROWSER=true` covers claude, codex and copilot, and a no-op `xdg-open` placed
first on the step's `PATH` covers agy, which calls `xdg-open` directly. The
no-op lives in a directory created for that run by the account running
Switchyard, root for a tenant, and removed when the run ends. Switchyard then
prints the URL the CLI shows, as one line you click in your own terminal:

```
switchyard: To sign in example-agent to claude, open this link in your own browser (click it, or Ctrl+click), then paste back here any code the page shows:
https://claude.com/cai/oauth/authorize?code=true&client_id=…
```

- **One unbroken line.** Your terminal's own link detection sees the whole URL,
  and the line is also an OSC 8 hyperlink for terminals that honour those.
  Konsole leaves OSC 8 off by default, under "Allow escape sequences for
  links". A URL a CLI draws itself, broken over several rows, opens only its
  first row when clicked.
- **Line by line, or below a full screen.** After the line that holds the URL
  for claude (`claude auth login`), codex and copilot. Below agy's full-screen
  sign-in, in rows kept from it.
- **Sign-in comes first.** Claude's own first run, with its theme and folder
  questions, comes after its sign-in. Claude Code 2.1.318 does not ask an
  account that already holds a credential to sign in again.
- **Paste back any code the page shows.** Claude and agy finish with a code to
  paste into the terminal. Codex and copilot return to a local address on this
  host, which any browser here reaches.

**Trust.** The sign-in page opens in **your own** browser and profile only
because you clicked the link, which is your own act. Nothing the agent account
runs touches your browser, your profile or your session bus. Only `https://`
URLs made of URL characters are printed, so the agent's output cannot put a
control sequence on that line.

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
So when a pinned name is absent from the owner's runtime, the GUI service grants
the one Wayland socket that is there instead, with the same owner, session and
symlink checks, and logs that it did. It never chooses among several: with two
or more live sockets and the pinned name absent it grants nothing and logs the
names. A pinned name that is present -- even beside another socket -- is always
the one used, and a socket granted only in its absence loses that grant when the
pinned name returns. The project and tenant
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
are refused. Setup verifies systemd activation before reporting success. An
installation failure rolls back its new ACLs and managed files. It puts the
previous install back and restarts it only when that install ran the same
helper. A helper from an earlier release may grant the runtime (as before
SYRD-791), so such a service stays stopped and the tenant has no desktop access
until the next upgrade (SYRD-796). First of all, before any check that could
refuse it, install stops every one of the owner's grant services still running a
helper from before SYRD-791, the tenant's own included, and removes every
tenant's entries from the runtime. So even a refused upgrade leaves no old helper
re-granting the runtime. Root's part of the install does the same first,
before its own checks (logind's runtime, the Wayland socket): through the
desktop owner's manager when it is up, and in every case by removing each such
unit's `default.target.wants` link, as the owner, so a manager that is down does
not start it at the next login (SYRD-798). A successful install starts the
tenant's new service itself; the others are replaced by their own upgrades.

The GUI-owned service starts with the user manager and checks every five seconds
for the approved session/socket. It reapplies after socket recreation or a later
login; no compositor-specific service name is hard-coded. A missing desktop
leaves the service pending and blocks clipboard-enabled role startup. GUI logout
is not required to test an upgrade and should not interrupt working sessions.

**The tenant never gets the compositor's own socket (SYRD-791).** A client of the
desktop owner's Wayland socket is offered every global the owner's own apps are
offered. On KWin 6.8.2, an account with only an ACL on that socket listed every
window with its title (`org_kde_plasma_window_management`) and typed into the
focused window (`org_kde_kwin_fake_input`), with no prompt. The service instead
holds a `wp_security_context_v1` listener per tenant: a socket the owner opens,
which KWin treats as a sandbox, the same mechanism Flatpak uses. It lives in
the desktop owner's grant directory (below) as
`switchyard-desktop-<gui-uid>-<tenant-uid>-<project>.wayland`, or at a shorter
name derived from that when the path would not fit a socket address. Its
sandbox engine is `org.switchyard.desktop-access`, its app id the tenant account
and its instance id the project. The listener lasts as long as the service, and
the service registers a new one whenever the compositor's socket is replaced. A
compositor that offers no `wp_security_context_manager_v1` gets no grant at all:
install refuses and says to choose headless, rather than falling back to the
compositor's socket.

What a tenant is offered, measured on KWin 6.8.2 in a disposable VM (63 globals
on the compositor socket, 56 through the security context):

| Global | Compositor socket | Security-context socket |
|---|---|---|
| `org_kde_plasma_window_management` (every window and title) | offered | withheld |
| `org_kde_kwin_fake_input` (typing into the focused window) | offered | withheld |
| `zkde_screencast_unstable_v1` (screen capture) | offered | withheld |
| `ext_data_control_manager_v1` (the clipboard without focus) | offered | withheld |
| `wp_security_context_manager_v1` | offered | withheld |
| `zwlr_layer_shell_v1` (overlays) | offered | **still offered** |
| `org_kde_plasma_shell` (panel and desktop roles) | offered | **still offered** |
| `kde_output_management_v2` (display configuration) | offered | **still offered** |
| `org_kde_kwin_keystate` (lock-key state) | offered | **still offered** |
| `xdg_wm_base`, `wl_seat`, `xdg_activation_v1`, `wl_data_device_manager` | offered | offered |

The last four Plasma rows are KWin's own policy for sandboxed clients. An agent
can still draw an overlay or change the display layout, but it can no longer read
another window, type into one, capture the screen through KWin, or read the
clipboard. An ordinary app run as the tenant opens its windows as before.

**The tenant never traverses the desktop owner's runtime (SYRD-791).** A user
runtime (`/run/user/<uid>`) is 0700 because the sockets in it are not protected
by their own modes. With only `--x` on it, an agent account reached the owner's
PipeWire and PulseAudio and recorded the microphone (measured in a disposable VM
with PipeWire 1.6.11, counts only):

| As the agent | No grant | `--x` on the owner's runtime |
|---|---|---|
| connect `pipewire-0`, `pipewire-0-manager` | refused | connects |
| PipeWire objects seen | — | all 54, the owner's microphone included |
| capture from the owner's microphone (`pw-record`) | — | a running stream linked to it |
| connect `pulse/native`; sources listed; `parecord` | refused | connects; 3 sources; linked to the microphone |
| connect `bus` (the session bus) | refused | connects, but registration is refused: dbus serves no other uid |

So the security-context sockets and the image-handoff directories live in the
owner's **grant directory**, `/run/switchyard-desktop/<gui-uid>/`, not in the
runtime. Root makes `/run/switchyard-desktop` (root's, 0755) and the owner's own
directory under it (0700) when it installs desktop access, and writes
`/etc/tmpfiles.d/switchyard-desktop*.conf` so both are made again at boot. No
account can claim the name first, because the parent is root's. Every reader
checks the parent is root's and the directory the owner's, neither writable by
anyone else. Root's pass at each install never changes the mode of an existing
grant directory with `chmod`: on a directory with ACL entries that would set the
mask to `---` and make every earlier tenant's grant ineffective. It sets the
owner, group and other classes with `setfacl`, which recomputes the mask from the
grants' entries, so N tenants installed in turn all keep their grants
(SYRD-796). With that layout the tenant matches the table's "no grant" column
for the runtime: no PipeWire, no PulseAudio, no bus, nothing else of the
owner's.

Only an execute ACL on the grant directory and a read/write ACL on the tenant's
security-context socket are applied. That socket is the owner's, mode 0600 apart
from that one entry. Nothing is granted on the runtime or the compositor's
sockets. Install removes the entries an earlier grant left there, for every
tenant this owner has installed, and each pass of the service removes its own
tenant's. Unrelated ACL entries are retained, except entries for uids no account
has: those are removed from the runtime, the grant directory, the compositor's
sockets and the security-context sockets, so a later account given the same uid
inherits nothing. Install likewise stops and removes the
service, policy, helper, receipt and socket of any of this owner's grants whose
tenant account no longer exists. If expanding the ACL mask would broaden another
entry's effective access, setup refuses it for operator review. There is no xhost
grant, Xauthority copy, default ACL, recursive permission change, or shared
private credential.

A protected GUI-owned readiness receipt in the grant directory records the
approved policy digest, the security-context socket and its identity, the compositor
socket it was registered on, and the context's metadata. Install waits, up to 20
seconds, for the service to write it, because only the service holds the context. The tenant cannot authorize itself by editing
its own config: startup verifies the matching receipt, socket ownership/identity,
access and a Unix-socket connection as the tenant. The probe reads no clipboard
content. Missing ACL tools, account/runtime, permission, session, socket or active
unit produces a pre-launch failure with its cause.

While it is waiting, the service logs why every five seconds (`journalctl --user
-u switchyard-desktop-<gui-uid>-<tenant-uid>-<project>.service`, as the GUI
owner). Where the tenant can already traverse the grant directory, it also marks
the receipt `pending` with that reason, and the launch error quotes it. Before
the tenant's first grant it cannot read the grant directory at all, and nothing
is granted just to report a reason, so the launch error names the unit and the
command to check it instead.

The receipt outlives a logout, as the grant directory does until reboot. When the
owner logs out or its compositor is gone, the service marks the receipt pending
with the reason (for example, that the owner is not logged in) and drops the
socket, and a launch quotes the reason.

A launch with no receipt at all, for example after desktop grants were withdrawn
by stopping the services, says plainly that the tenant has no desktop socket,
with the command that grants it again (`switchyard upgrade <project>` with the
approved policy) and how to check the service. A receipt from before SYRD-791,
which names the compositor's own socket, is refused the same way, whatever ACL
that socket still carries; the next pass of an upgraded service takes that grant
back.

For Wayland roles and their authentication commands, `WAYLAND_DISPLAY` is the
absolute path of the tenant's security-context socket in the grant directory. `XDG_RUNTIME_DIR` remains the tenant's logind runtime;
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
nobody else. It writes the image into `<grant directory>/switchyard-paste/<project>/`
(`/run/switchyard-desktop/<gui-uid>/…`, never the GUI runtime),
where the tenant may traverse the parent, list the project directory and read
each file, and write nothing. The project's listener tells the role where the
image is once its pane is idle. This GUI-owned service removes the image 15
minutes after the copy, and revoking the grant removes anything still there.
See [syrd-247-image-handoff.md](syrd-247-image-handoff.md).

## Existing projects, reapplication and rollback

`switchyard upgrade` installs the same policy/service and verifies readiness
without restarting any role. Existing process environments cannot be changed in
place. Preserve their work and use the existing per-role reload command at an
idle checkpoint; desktop-configured reloads reject busy panes even with force.
Attach/start preserves existing live sessions. If a compositor changes the socket
basename, new launches resolve the new receipt; existing processes need an idle
reload to inherit that name.

To revoke this project's desktop grant through the same supported lifecycle:

```sh
switchyard upgrade example --desktop-policy headless
```

This disables/removes only its managed GUI unit, policy/helper and receipt, and
restores its ACL entries. Other tenants and other projects sharing the same
approved account are retained. If another actor changed an ACL after setup,
rollback changes only the still-matching tenant entry; it never overwrites the
other actor's entries. A different Wayland policy similarly replaces the old
scoped grant through upgrade. An unavailable GUI user manager may need to be
restored before a previous grant can be revoked; setup must not claim revocation
while its persistent unit can still reapply the grant.

## Verification

`python3 tests/desktop_access_test.py` exercises real Unix sockets and filesystem
ACLs, including masks, recreation, rollback and multiple tenants. Its GUI account,
logind and systemd lifecycle are fixtures. The exported-release startup fixture
uses the actual new-project/configuration path and executes a harmless first role
process to verify its environment; account creation, board services and GUI
installation are fixtures there. Existing launcher tests explicitly choose
headless mode when desktop access is irrelevant.

Live acceptance is separate: after approved deployment, copy a known screenshot,
reload only an idle affected Codex role, and verify Ctrl+V creates an image
attachment without the X11 timeout. `wl-paste` alone does not satisfy acceptance.
Do not inspect unrelated clipboard data. Verify again after a naturally occurring
GUI login/socket recreation, without logging out working roles just for a test.
