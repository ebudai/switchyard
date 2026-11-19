# SYRD-834: Ctrl-V in a presentation tab hands the clipboard's image to the role there

## What failed

The User pastes images with Ctrl-V and text with Ctrl+Shift+V. Since SYRD-791, Ctrl-V with an image copied fails in
every role pane:
- **On a project's sandboxed socket:** "clipboard unavailable … X11 server connection timed out". The
  security-context socket withholds `ext_data_control_manager_v1`, which is what stops an agent reading the
  clipboard at will, so Claude Code falls back to X11, which is unreachable.
- **In panes launched before that socket existed:** "no image found in clipboard". They still name the owner's
  compositor socket, which is now behind a 0700 runtime.

`switchyard paste-image <project> <role>`, run by the desktop owner, works, but it is a command in another terminal,
not the key the User presses.

## The change

Each tab of a presentation window runs `scripts/switchyard-pane-window` as the desktop owner (SYRD-43). The pane
window now runs its attach through `scripts/switchyard_pane_relay.py`, a pty relay between Konsole and the attach.

**Everything passes through.** Every byte typed or pasted reaches the attach unchanged and at once, and so does every
byte it writes back. Window resizes are passed on. Konsole's Ctrl+Shift+V arrives as bracketed text, never as
Ctrl-V, and passes through like everything else.

**Ctrl-V (0x16) is held,** and a handover process runs `paste_image.pane_image(<project>)`. It has the same proof,
reader and directory as `paste-image`:
- the caller must be the project's approved desktop owner, proven by the policy in their own home;
- the clipboard is read with `wl-paste`, then `xclip`, capped at 10 MiB while it is read;
- the image goes into `<grant directory>/switchyard-paste/<project>/` (`/run/switchyard-desktop/<gui-uid>/…`),
  which the tenant may read and nothing else (SYRD-791).

What happens next depends on the answer:

| answer | the pane gets | the tab shows |
|---|---|---|
| an image | its path, as a bracketed paste (plain text if the attach turned bracketed paste off) | nothing |
| text or nothing | the 0x16, as before | nothing |
| a refusal, a crash, or no answer in 20 s | nothing for that Ctrl-V | the reason in the split's header for 8 s, then the header again |

Keys typed during the handover are held, then passed on after it, in order.

**No role, no manifest.** The path goes into this tab's own pane, which is the role on screen there, so no role is
named. That matters because a display slot's role changes at runtime (SYRD-733). The listener has nobody left to
tell, so no manifest is written. The owner's access service still removes the image 15 minutes after the copy, as it
removes every handoff.

**A Ctrl-V is a typed one:** a 0x16 outside any bracketed paste and outside any escape sequence the terminal sends.
A 0x16 inside a paste, or inside a terminal's reply (OSC, DCS), is data. Only this tab's keys reach the relay, so
"only when Switchyard has focus" needs no check. The role cannot drive the terminal's replies either, since tmux sits
between them.

**An unfinished sequence waits 50 ms for its next byte, then ends.** A terminal sends a reply or a key's sequence in
one write. The Escape key is a lone ESC, and Alt+] or Alt+P an escape prefix, with nothing after it. Without the
limit, the next key was taken as that sequence's end: Esc to interrupt the CLI, a screenshot, Alt-Tab back and Ctrl-V
handed the CLI a bare 0x16 (Audit, round 1). The limit is not a reset at the end of every read, so a reply split
across two reads inside it stays data. A bracketed paste has no limit: it always has its end marker.

**Where the reason is shown.** It goes in the split's header (OSC 30), not on the screen. Text written over tmux's
screen stays until tmux redraws those cells. A redraw can be forced with a same-size SIGWINCH, which works on tmux
3.8 directly, but `sudo`'s pty (use_pty) forwards only size changes. A real resize would reflow the CLI twice. The
relay reads the header tmux last sent and puts it back. A reason is written only where the attach's output stopped
between two escape sequences and outside a UTF-8 character.

**When there is no relay at all:**
- **A project with no approved desktop policy:** a headless one, or one that runs as the desktop owner's own account.
  The relay runs the attach in its own place (`exec`), and Ctrl-V is the CLI's.
- **A pane window without the relay beside it:** an older release, or a host without `/usr/bin/python3`. The attach
  runs as before.

The layout passes `--paste-project` only to a pane window that ships the relay. The desktop half builds the layout
with its own release but runs the tenant's pane window. An older pane window would take the flag for the command to
run, which would leave every tab dead.

**Still kept:**
- **SYRD-43.** When the attach ends, the relay hands the terminal back as it found it and exits with the attach's
  status. The pane window then says the tab is inert and waits; no shell runs. The relay also ends with the attach
  when something the attach started still holds its terminal open. When the tab closes (a hangup, heard as a signal
  or only as the end of input) or the relay is told to end, the attach is told as a closed terminal tells it, and is
  killed after 3 s.
- **SYRD-791.** No role gains clipboard or compositor access. The relay runs as the desktop owner, acts only for its
  own tab's project, and writes only through `pane_image` into the grant directory.

## What each CLI does with a pasted image path

Measured offline: a fake API in a private network namespace, a throwaway HOME, and a real PNG pasted as a bracketed
paste through tmux.

| CLI (version) | what a pasted image path does |
|---|---|
| Claude Code 2.1.318 | attaches it at once as `[Image #1]`; the request carries an `image/png` block |
| Codex 0.166.0 | attaches it at once as `[Image #1]` |
| Copilot CLI 1.0.102 | attaches it at once as `[📷 paste-….png]` |
| agy 1.5.1 | attaches it at once ("1 media attached (file, 160 B, image/png)"); the request carries it |
| Hermes 0.15.2 | shows the path as text, and attaches it on Enter ("1 image attached") |

## Roll-out

A window keeps the program it opened with, so presentation windows need reopening to pick this up. Panes keep their
environment until relaunched, but that does not matter here: the role's own clipboard access is not used.

The viewer layout on a desktop that is not KDE opens one terminal running the attach directly, with no pane window,
so it has no relay. `switchyard paste-image` still works there.

## Evidence

**`tests/pane_window_ctrl_v_test.py`** (it prints its check count when it passes).

The setup:
- **A tab** is a real pty running the shipped `switchyard-pane-window` with the arguments the product's own layout
  gives slot 0 of a cross-account window, and the shipped relay beside it.
- **The attach** is a stand-in in a pty of its own: it sets raw mode, asks for bracketed paste and a header as tmux
  does, and reports every byte it receives.
- **`pane_image`** is stood in beside the relay, so its answer is chosen per step and every call is recorded.

**Main first** (`878d8bfb`, from a git archive): with an image copied, Ctrl-V hands the role a bare 0x16, and nothing
reads the clipboard.

**On this tree:**
- **Pass-through:** all 255 byte values but 0x16 pass through unchanged, and so does a paste carrying its own 0x16. A
  keystroke arrives within 0.25 s.
- **An image:** after a cursor key, Ctrl-V gives the bracketed path. Keys typed during a 0.6 s handover come after it,
  and the handover is asked for this project only. With bracketed paste off, the path comes as text.
- **Text or nothing:** the pane gets the 0x16.
- **A refusal:** the header shows its first clause, then the slot's current header again (`Librarian`, not the
  `Main Developer` the window opened with). The pane gets nothing for that Ctrl-V.
- **A crash, or a clipboard that never answers:** the header says so. Keys typed meanwhile wait, then arrive after
  20 s.
- **Size:** the attach starts at the terminal's size and follows a resize.
- **The attach exits:** the tab reports status 3 after the attach's last output. The terminal is cooked again, the
  tab's process is `sleep` with no children, and Ctrl-V or Enter reaches nothing.
- **The tab goes:** a hangup (heard as SIGHUP, or with SIGHUP blocked and seen only as end of input) ends the attach,
  and so does SIGTERM to the relay. An attach that exits while its child holds the terminal still ends the tab.
- **No relay:** a project with no approved policy gets none, and the attach runs directly under the pane window.
  Neither does a release without the relay file.
- **An older tenant release:** with a tenant pinned to main's pane window, the layout leaves the flag out and the
  tabs work.
- **After a key that only starts a sequence:** Esc, Esc-[, Alt+] and Alt+P, each followed half a second later by
  Ctrl-V, hand the image over. A terminal reply split across two reads 10 ms apart, with the 0x16 opening the second,
  stays data.
- **Units:**
  - the scanner: inside a paste, markers split across reads, OSC and DCS replies, cursor keys, charset designations,
    an unfinished CSI; every escape prefix settles to a typed Ctrl-V, and a half-received paste does not;
  - the watcher: bracketed paste in a parameter list, headers ended by BEL or ST, partial sequences and UTF-8;
  - writes are deferred to a sequence boundary;
  - the header text is short and plain;
  - the handover process's five answers.
- **`pane_image` and `pane_handoff_offered`:**
  - one image is written 0600 and granted `r--` to the tenant, with no manifest, and an expired handoff is swept;
  - text reads nothing and writes nothing;
  - an unapproved project is refused;
  - a failed grant leaves nothing behind;
  - a relay is offered only for this account's policy, for another account's tenant, and for this exact project.

**`tests/team_launcher_pane_commands_test.py`:** the legacy layout's pane command carries `--paste-project`.

**Mutants:** each applied alone to a copy of this tree, all killed by assertions; the ticket comment lists them.
