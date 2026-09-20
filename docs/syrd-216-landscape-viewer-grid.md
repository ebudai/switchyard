# Five roles, three across and two below

Zorin UAT opened the non-KDE viewer and found five roles arranged two columns by
three rows. Every pane was narrower than it needed to be, and the last row held
one pane where two cells had been divided, leaving the shape of an empty sixth.

Nothing in Switchyard asked for that arrangement. The viewer asked tmux for its
`tiled` layout, and `tiled` decides the grid like this:

```c
rows = columns = 1;
while (rows * columns < n) {
        rows++;
        if (rows * columns < n)
                columns++;
}
```

Rows grow first. For five panes that lands on three rows and two columns, and
tmux applies it without ever looking at the window: the same grid comes out of a
240x80 viewer as would come out of a tall one. On the wide window a desktop
viewer always gets, it is exactly backwards.

## The grid

`viewer_grid` takes the rows from the integer square root and the columns from
what is left:

| panes | tmux `tiled` | viewer_grid |
| --- | --- | --- |
| 2 | 1 col x 2 rows | 2 x 1 |
| 3 | 2 x 2 (one gap) | 3 x 1 |
| 4 | 2 x 2 | 2 x 2 |
| 5 | 2 x 3 | **3 x 2** |
| 6 | 2 x 3 | **3 x 2** |

Six changes too, and that is deliberate rather than incidental: the presentation
this ticket cites -- a row across the top and the rest across the bottom -- is
the same shape whether the last row is full or short, and leaving six at 2x3
while five became 3x2 would be an arbitrary difference between two adjacent
sizes. Six is what the live project runs, so it is the more visible of the two
changes and is called out here for that reason. A role's slot is 0-5, so six
panes is the most a viewer ever holds; the rule is defined beyond that anyway,
and pinned by a test, in case the cap moves.

## Why the layout is written out

tmux has no named layout with this shape, so the viewer now hands it an explicit
one. A tmux layout string is a tree -- `{...}` side by side, `[...]` stacked,
leaves as `WxH,X,Y,id` -- behind a 16-bit checksum that tmux refuses the layout
without. `viewer_layout_string` builds the tree row-major, so pane order reads
left to right and then down, and a short last row spreads across the whole width
instead of leaving a gap: with five roles the bottom two take half the window
each rather than a third.

Two things about it are worth knowing, because both were checked against a real
tmux rather than assumed:

- **The pane ids in the string do not have to match the window's.** tmux assigns
  panes to the parsed cells in order, so a viewer whose panes are `%8`..`%12`
  takes a layout written with ids `0`..`4`. Writing the real ids would mean
  reading them back first, for nothing.
- **The shape survives a resize.** tmux rescales a layout proportionally when
  the window changes, which is what happens the moment a client attaches at its
  own size. Resized to 180x50 and to 320x100, the grid stays three across and
  two below, both rows still spanning the full width.

The remainder column goes to the leftmost pane in a row, which is what tmux does
with its own spare column, so a viewer looks the same whichever path built it.

## What did not change

`pane-border-status` and the slot labels are untouched, so the borders and
titles read as before -- they are why a pane's content starts one line below its
row: the divider line carries the label.

The Konsole separate-layout presentation is a different path entirely
(`LAYOUT_MODE_SEPARATE`, one window per role, geometry from the project's
`layout.json`) and is not touched here. Neither is the display-slot proxying
underneath the viewer, nor the observer `ignore-size` policy from SYRD-27 that
keeps a viewer from resizing the workers it watches.

Both viewer builders are changed, because there are two:
`presentation_controller._launch_viewer` builds the display-slot viewer, and
`team_launcher.launch_tmux_viewer_session` builds the direct-attach one. Each
now passes its own pane count; neither asks for `tiled`.

## Verification

`tests/viewer_landscape_layout_test.py` drives a real tmux server on a private
`TMUX_TMPDIR` -- not `-L`, because a viewer pane's own command re-invokes tmux
to attach and carries no socket flag, so only the directory reaches it -- and
reads the geometry tmux actually produced.

The fixture strips `TMUX` as well as setting `TMUX_TMPDIR`. A tmux command with
`TMUX` set and no `-L`/`-S` talks to the server that variable names and ignores
`TMUX_TMPDIR` entirely, so without that this suite would build its sessions on
the caller's own server and end by killing it. That is not hypothetical: it is
what several exploratory probes did to a live Switchyard desktop while this
ticket was being written.

The boundary is an explicit socket path, chosen before anything runs and passed
as `-S` on every command the fixture issues, so there is no server it could
reach but its own whatever the environment says. Getting there took three
attempts and each failure is worth recording, because each one looked like
protection:

- the first asked `display-message` in `__init__`, before any server existed:
  tmux failed, the empty answer was read as "nothing unexpected", and the guard
  passed for the one reason it must never pass;
- the second proved provenance only *after* `new-session` had run, so a
  redirected fixture created a session on somebody else's server and refused
  afterwards;
- and containment was tested with `startswith`, which accepts a sibling
  directory whose name merely begins the same way.

Now `boundary()` refuses before the first command, containment is by path
components on resolved paths, `verify()` refuses an absent answer rather than
treating it as reassurance, and `kill()` will not issue `kill-server` without
both. Four cases test the refusals rather than the happy path: nothing running;
a fixture aimed at another server, which must refuse before issuing
`new-session` and leave that server's session list unchanged; the same fixture's
teardown, which must refuse before it so much as probes; and a sibling path that
collides on prefix.

`TERM` is assigned rather than defaulted. This project's role panes carry
`TERM=dumb`, which tmux cannot drive, and a client started under it dies with
"open terminal failed" before attaching. Inheriting it meant thirty seconds of
waiting followed by a layout timeout -- a true statement about the wrong thing.
The wait now notices a client that has already exited and reports what its
terminal said, in about a second, and there is a case that starts a doomed
client on purpose to prove it.

The transition case waits on the window's dimensions and its topology together.
Waiting on topology alone returns the instant it is already right, and the first
step wants `[3,2]`, which is exactly the creation-time layout: the wait finished
before the client had attached and the window was still its creation size. That
race is now tested directly -- a window nothing will resize must make the wait
time out -- and attachment is established as attachment, by polling
`#{session_attached}`, rather than inferred from a shape that already matched.

It drives `_launch_viewer` end to end at five slots and at six, checks the rows,
that each spans the full width with no gap and no placeholder, that the rows
cover the full height, that pane order reads left to right and then down, and
that the slot labels landed on the matching panes. It drives
`launch_tmux_viewer_session` with a recording runner at three, five and six
roles and applies the `select-layout` that builder emitted to a real window of
that size -- so the assertion is about the geometry that argv produces, not
about the argv matching the function that built it. It resizes a five-pane
viewer to 180x50, 320x100 and back, and checks every size from one to six.

Portrait is covered at five and six panes, the 120x80 near-square case is
covered because nothing else distinguishes the aspect rule, and the transition
is driven by resizing a real client's pty -- landscape, portrait, landscape --
asserting both the shape and that the window is still exactly the client's,
which a layout fighting the client over size would fail.

One case pins the defect itself: tmux's `tiled` really does give five panes
[2, 2, 1]. Without it the suite would keep passing against some future tmux
whose `tiled` already did the right thing, while asserting nothing about this
change.

Mutation coverage: 23 mutants across the grid rule and its aspect predicate, the
span arithmetic, the checksum, the tree shape, the hook's event and its
installation in both builders, the helper's use of what it measured, and the
fixture's own safety guard -- which is production code for this suite's safety
claims, so a guard nothing can break is a guard nothing checks.

Two survivors, both equivalent mutants rather than gaps, and both measured
rather than argued. Dropping `-w` from `set-hook` changes nothing, because a
`session:window` target already scopes the hook to the window. And removing
`kill()`'s check on the *running* server's socket changes nothing now that `-S`
pins it: what tmux reports is what the fixture handed it, so that check can no
longer fail once `boundary()` has passed. It is kept because it was explicitly
asked for and costs nothing, but it is no longer the thing doing the work --
the explicit socket is. That was measured rather than assumed -- with and
without the flag, on a session holding two windows, the hook lands on `s:0` and
not on `s:1` either way. The flag is kept for what it states, and the note above
records why.

Earlier passes turned up real gaps rather than missing assertions: the
remainder-column convention was unasserted; a single-row special case turned out
to be dead weight and was removed rather than tested; both builders would have
passed with their pane count hardcoded to five until each was driven at six; and
the cell-aspect constant was invisible until a 120x80 window was tested, because
240x80 and 80x240 cannot tell it apart from comparing the counts.

Live User acceptance on the preserved Zorin `test` tenant is required by the
ticket and gated by the board: `needs_user_signoff` is already set.
