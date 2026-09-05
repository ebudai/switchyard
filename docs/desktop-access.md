# Screenshot clipboard access before project startup

Switchyard requires an explicit desktop policy before it starts a role. A
Wayland grant allows the tenant account to act as a client of the selected GUI
session, with the capabilities its compositor exposes. It is not restricted to
clipboard operations. Choose it only with consent for that tenant and GUI owner.

For a project without desktop clipboard access:

```sh
switchyard new --desktop-policy headless
```

An interactive `switchyard new` asks for a policy file or `headless` before
provisioning. `--yes` does not approve desktop access. Existing unconfigured
projects remain inspectable through attach/status/upgrade; starting new role
processes requires choosing a policy. Legacy PGU account names do not confer
consent on a new tenant.

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
reported by logind. If several sockets exist, use the selected socket basename
such as `wayland-0`. An active Wayland session is required. The project and tenant
must match the target configuration. Missing, rejected or malformed consent
stops setup without a grant.

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
installation failure rolls back its new ACLs and managed files.

The GUI-owned service starts with the user manager and checks every five seconds
for the approved session/socket. It reapplies after socket recreation or a later
login; no compositor-specific service name is hard-coded. A missing desktop
leaves the service pending and blocks clipboard-enabled role startup. GUI logout
is not required to test an upgrade and should not interrupt working sessions.

Only an execute ACL on the GUI runtime directory and a read/write ACL on the
selected socket are applied for the approved tenant. Unrelated ACL entries are
retained. If expanding the ACL mask would broaden another entry's effective
access, setup refuses it for operator review. There is no xhost grant, Xauthority
copy, default ACL, recursive permission change, or shared private credential.

A protected GUI-owned readiness receipt in the GUI runtime records the approved
policy digest and socket identity. The tenant cannot authorize itself by editing
its own config: startup verifies the matching receipt, socket ownership/identity,
access and a Unix-socket connection as the tenant. The probe reads no clipboard
content. Missing ACL tools, account/runtime, permission, session, socket or active
unit produces a pre-launch failure with its cause.

For Wayland roles and their authentication commands, `WAYLAND_DISPLAY` is the
absolute GUI socket path. `XDG_RUNTIME_DIR` remains the tenant's logind runtime;
the bus address also names that tenant's runtime. `DISPLAY` and `XAUTHORITY` are
removed so inaccessible inherited X11 settings cannot win transport selection.
No tmux global environment is changed. Headless roles remove display/clipboard
variables without granting GUI access.

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
