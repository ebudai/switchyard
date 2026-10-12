# SYRD-247: handing a clipboard image to a role on GNOME

## What failed

On Zorin's GNOME desktop (run `test19`), pasting an image into a role pane
failed in both provider families:

- **Claude:** "No image found in clipboard".
- **Codex:** "clipboard unavailable: … X11 server connection timed out because
  it was unreachable".

Text paste, pane focus, resize and the viewer layout all worked.

## Why: no provider can read this clipboard

These findings come from the installed binaries in September, and today's
recheck of Codex 0.162.0 agrees:

- **Claude** reads images only by running `xclip` or `wl-paste`. It has no
  native fallback for images.
- **Codex** links arboard and wl-clipboard-rs. It asks the compositor for
  `ext-data-control` or `wlr-data-control`, and falls back to X11 when neither is
  offered.
- **Mutter**, GNOME's compositor, implements neither protocol, and its
  maintainers have declined to.
- **The approved desktop policy** removes `DISPLAY` and `XAUTHORITY` on purpose,
  so the X11 fallback is unreachable.

So both providers fail, and no provider setting changes that. KWin implements
`ext-data-control`, which is why Claude pastes images on this host while
SYRD-98's Codex fault is something else.

The obvious repairs are each a reusable clipboard read for every role sharing
the tenant's UID, which SYRD-98 forbids:

- exporting the owner's `DISPLAY` and `XAUTHORITY`;
- `xhost +SI:localuser:<tenant>`;
- a private Xvfb for the tenant;
- any tenant-side clipboard tool.

## The design: the owner hands over one image

```sh
switchyard paste-image <project> <role>     # run by the desktop owner
```

### Who may run it

Only the GUI owner named in a desktop policy that owner installed for this
project.
- **The proof is the owner's own policy copy:**
  `~/.config/switchyard/desktop-access/switchyard-desktop-<gui uid>-<tenant uid>-<project>.json`.
  It must be a regular file the caller owns that no one else can write, and it
  must validate as a Wayland policy naming the caller as `gui_user`.
- **The tenant in it must match its file name.**
- **A project that runs as the owner's own account is refused,** because there
  every role pane is the caller.
- **It never escalates.** It is in `SWITCHYARD_UNPRIVILEGED_COMMANDS`; escalated,
  it would read root's clipboard, which does not exist.
- **The tenant config is not read.** Tenant homes are `drwx--x---`, so the owner
  could not read it anyway.

### What crosses

One image type the clipboard offers, taken in this order:
1. `image/png`;
2. `image/jpeg`;
3. any of `image/gif`, `image/webp`, `image/bmp`.

The types are listed with whichever tool answers first: `wl-paste`, then
`xclip`. Under GNOME, the owner still has XWayland, so `xclip` is the one that
answers. Claude's own image path tries the same two tools in the same order.

The bytes are read in 64 KiB chunks. The reader is killed once they pass
10 MiB, so an oversized copy is refused while it is being read. Text, or an
empty clipboard, is a refusal that names what is there. Nothing is logged or
echoed.

### Where it lands

The image goes in `<gui runtime>/switchyard-paste/<project>/`:

| path | owner | mode and ACL | tenant may |
|---|---|---|---|
| `switchyard-paste/` | GUI owner | `0700` + `u:<tenant>:--x` | traverse |
| `switchyard-paste/<project>/` | GUI owner | `0700` + `u:<tenant>:r-x` | list, open |
| `paste-<32 hex>.<ext>` | GUI owner | `0600` + `u:<tenant>:r--` | read |
| `paste-<32 hex>.handoff.json` | GUI owner | `0600` + `u:<tenant>:r--` | read |

Every entry is made through `desktop_access.grant`, which refuses to broaden any
unrelated masked entry.

**Write order:** the image is written with `O_EXCL|O_NOFOLLOW`, fsynced and
granted. The manifest is then written under a temporary name, granted, and
renamed into place last. So the listener never sees a manifest whose image is
incomplete.

**Refusals:** `prepare_handoff_dir` refuses:
- a symlink, a non-directory, a foreign owner, or a group/other write bit
  anywhere it would write;
- a runtime where the approved grant's own traverse entry is not applied. In
  that case the tenant could reach nothing, so nothing is handed over.

**What the tenant can do there:** list the project's directory and read its
handoffs. It cannot list the runtime or the parent directory, create anything,
or delete anything. Another tenant, even one of the same owner's other
projects, reaches nothing.

**Same-UID roles:** every role of the tenant shares its UID, so any of them can
read an image handed to this project during its 15 minutes. That is the image
the owner chose to hand to this project. No role gains a way to read the
clipboard.

### How the role is told

The project's own notify listener runs as the tenant and delivers into panes
through `directorctl send`. The GUI owner cannot do that: the sudoers grant to
run tmux as a role belongs to the project owner and Director accounts.

**Finding 1, wiring.** `main()` wires the handoff from the tenant's own
configuration. It reads the registry entry, then its `config_path`, then that
file's `desktop_access`. The policy is accepted only when it is a valid Wayland
grant naming this account as tenant. A headless tenant gets nothing.

**What each loop pass delivers.** A handoff is delivered only when:
- its manifest and image are regular files owned by the GUI owner, opened
  without following links;
- the image's size and SHA-256 match the manifest;
- its schema and project are this one's;
- its image shares its own stem;
- it names a role in the listener's own role table, and its target comes from
  that table.

**Never from the manifest:** the pane target and the path to send.

**The message** is one direct line of at most 200 characters, the same for both
providers:

```
Image from <owner>'s clipboard: <absolute path> - open it with your image tool (Read or view_image); it is deleted at HH:MM.
```

If the path makes that line too long, a shorter form is used. Otherwise
`directorctl` would put the line into a file and send a pointer to it.

**Why not `@path` for Codex.** `directorctl` types with `send-keys -l` and then
presses Enter; there is no bracketed paste. In Codex, `@` opens the file-search
popup, and Enter can select an unrelated match. A plain path works instead:
Codex has a built-in `view_image` tool for local images, and Claude's Read
opens image files. Each provider opens the file as the tenant, when it acts on
the message.

**When it is sent.** A handoff is sent only while the pane's activity gate
reports it idle; otherwise it waits for a later pass. A failed send is retried
on the next pass. Delivered stems are recorded in
`$XDG_RUNTIME_DIR/switchyard-image-handoffs-<project>.json`, so a restarted
listener does not deliver twice.

**It cannot stop the loop.** Any exception in a handoff pass is logged and does
not stop the listener's notices.

### Retention and cleanup (finding 3)

**Delivery never deletes anything,** and the tenant could not delete anyway.

- **Lifetime:** the image exists from the copy until
  `HANDOFF_TTL_SECONDS` (15 minutes) after it.
- **Delivery window:** delivery happens only in the first
  `DELIVERY_WINDOW_SECONDS` (10 minutes). So a role that is told about an image
  can open it for at least `MIN_RETAINED_AFTER_DELIVERY` (5 minutes) afterwards.
  A handoff whose pane stays busy past the window is logged once and left to
  expire. The owner can simply run the command again.
- **Who removes it:** the GUI owner's existing access service
  (`switchyard-desktop-<gui>-<tenant>-<project>.service`, the `watch` loop that
  reapplies the grant every 5 s). It runs `sweep_handoffs` on every pass, so an
  expired image leaves within about 5 s, without any further paste.
- **Also removed:**
  - each `paste-image` sweeps before it writes;
  - revoking the grant (`rollback`) removes the project's directory;
  - the runtime directory itself goes when the owner's last session ends.
- **Before the upgrade:** until a tenant upgrade refreshes the owner's copy of
  the helper, the service does not sweep. Removal then falls to the next paste,
  to rollback, and to logout.

## What did not change

- text paste, pane focus, resize, scrollback, and the viewer layout;
- every role's environment, the Wayland socket grant and its readiness receipt;
- headless tenants;
- the listener's notices. Its change is one import, one line in the loop, and
  one in `main()`, which keeps `notify_listener.py` at its 1,250-line limit.

## Tests

**`tests/image_handoff_two_identity_test.py`** runs the whole crossing with three
real Unix identities in a user namespace: desktop owner (uid 1000), tenant (1019)
and another tenant (1020). It uses a tmpfs over `/run/user` with real ACLs, and a
namespace-only `/etc/passwd`. Only `wl-paste` and `loginctl` are stubbed; the
host is untouched. The test covers:

- **Before the grant:** with the approved grant not applied, the owner's
  command refuses and writes nothing.
- **The deposit:** the owner runs the shipped `scripts/switchyard paste-image`
  wrapper. The getfacl output shows exactly the entries in the table above.
- **The tenant's reach:**
  - it lists its project's directory and reads the image;
  - it cannot list the runtime or the parent;
  - it cannot create, write or delete there.
- **Another tenant:** reaches nothing.
- **The listener:** the tenant runs the shipped listener `main()` with the
  environment its generated unit declares. Only `run_forever` is replaced, by
  one pass of the real `listen_once` against a fake database.
  - `main()` wires the handoff.
  - The real `directorctl send` puts one direct line into a tmux server the
    tenant owns.
  - A stand-in provider opens the image a second after the line lands, and gets
    the copied bytes.
  - A restarted listener does not deliver it again.
- **Removal:** the owner's sweep keeps the image until 15 minutes after the
  copy, then removes it with its manifest. Revoking the grant removes a second
  handoff and both directories.
- **Main, from a git archive:** it has no `paste-image`, and its listener
  delivers nothing.

**`tests/image_handoff_test.py`** covers each decision on its own:
- which clipboard tool answers, and the bounded read;
- image-type choice;
- every refusal about whose policy proves the caller, including a same-account
  project;
- write order and grants, and that a failed grant leaves nothing;
- the owner directory's refusals and the sweep's scope;
- the collector:
  - waiting while busy, delivering once, and the restart ledger;
  - the delivery window;
  - six kinds of refused manifest, each reported once;
  - retry after a failed send, and that a broken pass cannot stop the
    listener;
- the 200-character line;
- how the listener reads its own policy;
- that the shared names agree;
- that the listener's real `listen_once` runs a pass.

## Evidence

- **Mutation:** 29 mutants over the safety decisions, all killed:
  - the owner's command: policy trust, the cap, image types, grants, write order and cleanup;
  - the directory grants, the applied-grant check, the sweep's TTL and link handling, rollback,
    and the watch loop's sweep;
  - the collector's owner, no-follow, digest, role, window, idle-gate, ledger and isolation checks;
  - the message length;
  - both listener lines.

  Two survived at first, both test gaps. A planted symlink's own mtime was the current time, and a
  tampered image differed in length, so the size check alone caught it. The tests now cover both.
- **The sweep:** 131 guard suites that pin the dispatch, the command tables, `desktop_access` or the listener,
  run serially on main (3d410ca) and on this tree.
  - **Newly red, then fixed:** 35 failed on the one copied check in `tests/launcher_main_view.py`
    that pins `switchyard_main`'s nested imports. `paste_image` is added to that shared list, and
    to `switchyard_dispatch_boundary_test`'s `BRANCHES`, `SHAPE` and `NESTED`, as SYRD-567 and
    SYRD-579 did for theirs. `switchyard_commands_boundary_test`'s answers were regenerated with
    its own `run()`; a script shows each differs only by `paste-image`, unprivileged and never
    served. All 35 then pass on this tree.
  - **Already red on main:** 34 suites fail on both trees with identical normalised output.
  - **Not run:** `team_launcher_layout_upgrade_commands_test` (it writes into the real HOME) and
    `runtime_registration_wait_test` (it reaches the live testing board).

## Not proven here

The live paste on Zorin is the acceptance (SYRD-222), and it is parked; this
host has no GNOME session. These steps are for when the User resumes it:

1. Upgrade `test19` to a release containing this change. That refreshes the
   owner's desktop-access helper, so its service sweeps, and restarts the
   listener, so `main()` wires the handoff.
2. As the desktop owner, install `xclip` if it is missing, copy a known
   non-sensitive image, and run `switchyard paste-image test19 <claude role>`.
   The role should open the image. Repeat for a Codex role.
3. Check that the file is gone 15 minutes later. Check text paste, focus and
   resize. Repeat after a presentation close and reattach, and after an idle
   role restart.
