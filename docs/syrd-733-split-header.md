# SYRD-733: a split's header names the role its slot shows now

Live, slot 0 of the SYRD presentation was configured for Inspector, with Librarian detached. After
`switchyard present show librarian --slot 0`, the presentation said slot 0 showed Librarian: the desired and actual
role, the display proxy, `status-left` and `@switchyard_role` all agreed. The Konsole split header still said
`Inspector`.

The header comes from the pane wrapper, `switchyard-pane-window`, which sends OSC 30 once as the window opens, with
the configured slot role's label. Konsole has no layout key for a split's title (SYRD-122). Nothing sent the split a
header again, so a runtime change of what a slot shows left the header behind.

## The change

**The slot sends the header itself.** Each display slot already sets `set-titles on` and sends the project's name as
the terminal title, so the window's caption stays the project's under focus (SYRD-141). Its `set-titles-string` now
continues after that title: a BEL, then OSC 30 with the split's header. tmux wraps the string in its own `ESC ]0;`
and BEL, so the terminal receives the same two escapes the wrapper sends, in the same order:
`ESC ]0; <project> BEL  ESC ]30; <header> BEL`.

**The header** (`display_slot_split_title`) is:
- the presentation label of the role the slot shows (`pane_split_title`), while that role runs;
- `<label> (not running)` when the slot is built while the role's worker is down;
- `<label> (disconnected)` when the worker's session ends under the slot. The slot's own recovery hook respawns the
  status screen and relabels the split in the same hook command;
- `Slot <n>: hidden` when the slot shows no role.

**Text only.** The project name and the header are cleaned of the control characters a handoff's titles are refused
for, and `#` is doubled, since tmux reads the string as a format.

**When it is sent.** tmux sends its title to a client when the client attaches, and again whenever the option
changes. Every path that changes what a slot shows goes through `_configure_display_session`:
- `show`, `swap`, `hide` and `restore`;
- recovering a role, and reconnecting a role's slots;
- respawning every slot.

So each of these relabels the split through the tmux client already in it. A window that is closed and opened again
is corrected as its client attaches, whatever title its wrapper started with.

**A window opened after a swap** also starts right. `presentation_slot_titles` takes the presentation's own mapping,
and the window launch passes it, both for a window handed to the desktop account and for one opened here. Without a
mapping it gives the configured slots, as before.

**What does not change:**
- the window's title, which stays the project's;
- the workflow's role slots and the generated config;
- the viewer's pane borders and the slots' tmux options;
- the workers: nothing types into one or restarts one.

Nothing writes to a desktop terminal from outside it: the escape reaches the split through the tmux client the split
is already running, across the existing display bridge. On a non-Konsole terminal OSC 30 is an unknown escape and is
ignored, as the wrapper's already is.

Measured on tmux 3.2a, 3.4 and 3.8: each sends the string unchanged on attach and again on every change, recovery
hook included.

## Evidence

**`tests/presentation_split_title_test.py`:**
- **The configuration** has the live shape: Inspector configured in slot 0, Main in slot 1, the Director in slot 2,
  and Librarian detached.
- **The setup:** real worker sessions on a private tmux server. Each split is a real pty running the shipped wrapper,
  with the title and the attach a launch gives it, and its escapes are read as Konsole reads them.
- **Order:** main (`24525a93`) runs first, from a git archive, on every tmux build. On main, slot 0 shows Librarian
  and its header still says `Inspector`, and a window opened then names slot 0 Inspector.
- **Here,** through the product's own verbs, on tmux 3.8 and, through `SWITCHYARD_TEST_TMUX_BINARIES`, on 3.2a and
  3.4:
  - opened: `Inspector`, `Main Developer`;
  - show librarian 0: `Librarian`;
  - swap 0 1: `Main Developer` and `Librarian`;
  - hide 0: `Slot 0: hidden`;
  - show again: `Librarian` and `Slot 1: hidden`;
  - restore default: `Inspector` and `Main Developer`;
  - Librarian's session ended: `Librarian (disconnected)`;
  - back, and its slot reconnected: `Librarian`;
  - every slot respawned: `Librarian`;
  - ended again, then every slot respawned while it is down: `Librarian (not running)`;
  - back again: `Librarian`;
  - the window closed and reopened with the configured titles: the split starts as `Inspector`, and the attach
    corrects it to `Librarian`.
- **At every step:**
  - each slot's `@switchyard_role` agrees with its header;
  - the window's title is only ever `Demo Team`;
  - Inspector's, Main's and the Director's workers are never restarted;
  - Librarian's worker is never restarted until its own session ends;
  - nothing is typed into any worker;
  - the config file and its slots are unchanged.
- **A window opened after the swap** names slot 0 Librarian, both in the handoff to the desktop account and in the
  layout written here.

**`tests/team_launcher_presentation_titles_test.py`:**
- **New, with a real Konsole** (26.08.2, offscreen, with its own D-Bus): six display slots and the real wrapper. Slot
  0 is relabelled with the product's own title options, as `show librarian --slot 0` relabels it. Konsole's reading
  of the split moves to `Librarian`, the other five headers stay, and the window caption stays `Switchyard` through
  every focus change.
- **The slot-title argv case** now checks both parts: the window's is the project's for every slot, and the split's
  is the label. `#` is doubled, and ESC, BEL and C1 controls become spaces.
- **The guard** for which sessions send a title now reads `presentation_display_session.py`, where the function moved.
  It had been failing on main, so none of the suite's later cases, Konsole's included, had been running.

**Mutants:** 16, each killed by a failed check:
- the split part removed: caught by the pty test, by the suite's argv case, and by the real-Konsole case run alone;
- the raw role name in place of the label;
- the hidden label made the project's;
- the recovery hook's relabel removed;
- every state labelled live;
- `disconnected` and `not running` each collapsed into the other;
- `#` not escaped, or control characters not removed;
- each of the two window-launch call sites without the mapping;
- the mapping overridden by the configured slots;
- OSC 2 in place of OSC 30;
- the slug in place of the presentation label;
- `set-titles off`.

Nothing live was changed.
