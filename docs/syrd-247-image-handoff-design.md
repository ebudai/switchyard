# Handing over an image, not a clipboard

`test19` launched all five roles in the right layout on Zorin. Focus and resize
work. Pasting a known image fails in both provider families:

* Claude: “no image in clipboard”.
* Codex: “Failed to paste image: clipboard unavailable: Unknown error while
  interacting with the clipboard: X11 server connection timed out because it was
  unreachable.”

This is the design for the fix, with the evidence it rests on, written before any
code so it can be argued with. `test19` has not been touched.

## The finding: the compositor, not the grant

Two Switchyard hosts, same grant, different result:

| | this host (SYRD-98's host) | Zorin `test19` |
| --- | --- | --- |
| compositor | KWin (Plasma) Wayland | GNOME Shell 46 Wayland |
| Claude image paste | **works** (SYRD-98 records it) | fails, “no image in clipboard” |
| Codex image paste | fails (X11 timeout) | fails (X11 timeout) |

The difference is not the ACL, the receipt, or the role environment. It is
whether the compositor offers a protocol that lets a client read the clipboard
**without keyboard focus**.

A terminal CLI under a tmux pane has no Wayland surface and never holds focus, so
the ordinary `wl_data_device` selection is never offered to it. Reading the
clipboard from there requires a privileged protocol —
`wlr-data-control-unstable-v1`, or its successor `ext-data-control-v1`.

Verified locally on this host, as the tenant account (uid 1006, not the GUI owner
uid 1000), with only the ACL the existing policy grants:

```
$ ls -la /run/user/1000/wayland-0
srwxrwxr-x+ 1 eric eric ... /run/user/1000/wayland-0     # note the ACL '+'
$ wl-paste --list-types
image/png
application/x-qt-image
...
```

A non-focused, non-owner, ACL-granted client reads clipboard **types** here. So
the grant shape is sufficient — where the compositor implements a data-control
protocol. KWin implements `ext-data-control-v1`.

Mutter implements neither. GNOME/mutter issue #524, “Support wlr-data-control
(wayland protocol)”, remains open, and the maintainers' position is that handing
one application another's clipboard is what a compositor exists to prevent.

## Both providers, verified from the binaries

Read out of the installed programs on this host rather than inferred from the
error text.

**Claude Code** (`/opt/claude-code/bin/claude`, a Bun-compiled ELF with the JS
bundle embedded) shells out, per platform. Its Linux image branch:

```js
linux:{
  checkImage:`xclip -selection clipboard -t TARGETS -o | grep -E "image/(png|jpeg|jpg|gif|webp|bmp)" || wl-paste -l | grep -E "image/(png|jpeg|jpg|gif|webp|bmp)"`,
  saveImage:`xclip -selection clipboard -t image/png -o > … || wl-paste --type image/png > … || xclip … image/bmp … || wl-paste --type image/bmp > …`,
}
```

`checkImage` runs first and a non-zero exit aborts before `saveImage` is tried.
Text paste has a native-addon fallback; **images do not** — the only image paths
are those two subprocesses. The user-visible string is `No image found in
clipboard` (the ticket quotes the User's paraphrase, “no image in clipboard”).

**Codex** (`openai-codex 0.155.1`, stripped Rust ELF) does not shell out at all.
It links `arboard` and `wl-clipboard-rs`, and it **binds both** data-control
protocols — `zwlr_data_control_*` and `ext_data_control_*` are all present in the
binary. Its own fallback log lines are:

> `Successfully initialized the Wayland data control clipboard.`
> `Tried to initialize the wayland data control protocol clipboard, but failed.
> Falling back to the X11 clipboard protocol. The error was: `

and the error substituted in comes from `wl-clipboard-rs`:

> `A required Wayland protocol (ext-data-control, or wlr-data-control version 1)
> is not supported by the compositor`

with the X11 leg's own string, verbatim, in the same binary:

> `X11 server connection timed out because it was unreachable`

The message the User saw is `Failed to paste image: ` + `clipboard unavailable: `
+ that arboard error.

So both failures are now explained at the mechanism level, and the explanation is
the same one: **no data-control protocol, and no reachable X11.**

* Claude's `xclip` and `wl-paste` both need one of those two transports.
* Codex asks for `ext-data-control` *or* `wlr-data-control`, is told the
  compositor supports neither, and falls back to X11 — which the policy
  deliberately makes unreachable by removing `DISPLAY`/`XAUTHORITY`. Hence a
  *timeout* rather than a clean “unsupported”.

This also disposes of a tempting misreading. Codex already supports the modern
protocol; there is no provider setting, version bump or configuration that fixes
this, because the compositor is the side that declines.

### One correction to my own first reading

I initially suspected the tenant simply could not open the GUI socket, since
`/run/user/1000` is `drwx--x---+` and listing it as uid 1006 is refused. That is
wrong, and the empirical check above is why: `wl-paste --list-types` **succeeds**
as uid 1006. The ACL grants traversal and socket access without granting
directory listing, which is exactly what `docs/desktop-access.md` says it does.
The grant is working; do not go looking for a permissions bug that is not there.

### What this rules out

* **Exporting `DISPLAY`/`XAUTHORITY`, or `xhost +SI:localuser:<tenant>`.** This
  turns the tenant into a full X client with access to every other X client's
  input and contents. Forbidden by this ticket and by SYRD-98, and it would not
  be narrow: every role shares one Unix UID on these hosts, so it is a grant to
  all of them.
* **Xvfb, or requiring a different compositor.** A blanket host dependency to
  paste a picture, and explicitly ruled out.
* **Waiting for GNOME.** The protocol is declined upstream, not pending.

There is no narrower *native* fix. The clipboard cannot be read by these
processes on GNOME Wayland at all, so the image has to be handed to them by
somebody who can read it.

## The design: the owner hands over one image

The only party that can legitimately read that clipboard is the GUI owner, who
already has it. So the owner hands over **one image**, not a capability.

    eric (GUI owner)                     tenant role pane
    ----------------                     ----------------
    copies image in the desktop
    runs:  switchyard paste-image test19 director
      |
      +- reads clipboard ONCE, as eric
      +- refuses unless an image/* type is offered
      +- refuses above the size cap
      +- writes ONE spool file, owner-owned, 0640
      +- ACL: read for the tenant uid, this file only
      +- checks the role is a live registered pane
      +- types the spool path into that pane  ------>  provider reads the file
      +- removes the file on ack, or after its TTL

Six properties, each answering a constraint the ticket and SYRD-98 set:

1. **Who authorizes.** The GUI owner, by running the command as themselves. The
   tenant cannot initiate; there is no endpoint it can call. A role never gains a
   clipboard read primitive, so a same-UID sibling gains nothing either — which
   is the whole of SYRD-98's objection to environment-based fixes.
2. **What crosses.** One bounded file: an `image/*` MIME chosen from
   `wl-paste --list-types` (metadata, never content), refused above a size cap,
   written by the owner. No caller-chosen path — the helper picks it, the role
   only receives it.
3. **Live runtime identity.** Before sending, the helper checks that the named
   role is a registered, live pane of that project, using the session records
   Switchyard already keeps — not a pane name, not an environment claim. A stale
   or replaced runtime is refused, failing closed.
4. **Conveyance.** The path is typed into the pane through `directorctl send`,
   the reviewed delivery primitive the notify listener already uses. It is
   ordinary text, so text paste, scrollback, focus, resize and the viewer are
   untouched by construction.
5. **Cleanup.** The file is removed when the send is acknowledged, or after a
   short TTL by the same GUI-owned service that already runs per tenant. One
   outstanding handoff per role. Failure removes it and says so.
6. **No new authority surface.** The ACL is granted on one regular file, through
   `desktop_access.grant()`, which already refuses any change that would broaden
   an unrelated masked entry, and `restore_grant()` already knows how to take it
   back without trampling another actor's entry.

Nothing in the approved policy widens: the socket grant, the receipt, the role
environment and the headless path are all unchanged.

## Test plan

Focused, on the boundary that changes:

* an image type absent from the clipboard is refused, with our own message, and
  nothing is written;
* a type that is not `image/*` is refused;
* an image over the cap is refused, and the partial spool file is removed;
* the spool file is owner-owned, `0640`, with exactly one added ACL entry for the
  tenant uid, and `restore_grant` removes it;
* a role that is not a live registered pane of that project is refused — the
  same-UID sibling case — and nothing is written or sent;
* a stale/replaced runtime is refused;
* what reaches the pane is the path and nothing else, through the existing
  sender;
* the file is gone after acknowledgement, and gone after the TTL when it is not;
* clipboard bytes never reach a log line, a Board record or a test fixture;
* headless policy: the command refuses, unchanged.

Live UAT on Zorin, on `test19`, with a known harmless image:

1. the User copies the image; `switchyard paste-image test19 <role>` for one
   Claude role and one Codex role; the image appears as an attachment in both
   panes with no X11 timeout and no “no image” message;
2. ordinary text paste still works in both;
3. close and reattach the presentation, repeat 1;
4. restart one affected role at an idle checkpoint, repeat 1;
5. confirm the spool directory is empty afterwards.

## The conveyance, now settled for both

**Claude.** Its own documentation lists three ways to add an image to a running
session, and the third needs no clipboard:

> 3. Provide an image path to Claude, for example "Analyze this image:
> /path/to/your/image.png"

**Codex.** `-i/--image` is documented as attaching to the *initial* prompt, so it
cannot reach a long-running role. But the interactive composer takes `@` and
lists files with a picker, and an `@`-attached image becomes a real image
attachment. That is the mid-session path.

There is one documented trap, and it decides where the spool file goes. Codex
issue #46391, still open: when the **session** working directory differs from the
**process** working directory, an `@`-attached image "silently becomes a plain
text path" — the picker lists it, the composer inserts the bare filename, and no
image is sent. Relative resolution diverges between the two cwds.

So the spool file is written **inside the role's own working directory**, in
`.switchyard/paste/`, not in a shared or owner-side location:

* a Switchyard role is launched in its worktree and nothing issues `/cd`, so the
  session cwd and the process cwd are the same directory — the condition the bug
  needs never arises;
* the reference sent to the pane resolves under that same directory for both
  providers;
* the ACL is granted on one regular file inside a directory the tenant already
  owns, which is a smaller change than granting anything in the GUI owner's
  runtime.

`.switchyard/paste/` is ignored by the project's VCS, so a handed-over image
never becomes a commit.

Neither provider gains a capability by this: each is handed one path, to one
file, that the owner deliberately created.

## Residual risks, named

* **A provider changes its attachment gesture.** Both gestures are documented,
  but neither is API. A live UAT step exists for exactly this, and the failure
  mode is visible — the image arrives as text — rather than silent corruption.
* **Codex #46391 regressing in our favour or against it.** Placing the file under
  the session cwd avoids the condition entirely rather than depending on the bug
  being fixed.
* **The owner runs the command for the wrong role.** The role is named on the
  command line and checked against live pane records; a wrong name is refused,
  not delivered elsewhere.
