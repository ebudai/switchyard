# SYRD-550: the composer's content decides whether someone is typing

## The defect, both ways

`PaneActivityGate._target_cursor_state` called a pane "human composing" when its
cursor sat right of the prompt's home column (`cursor_x > 2`). That rule was wrong
in both directions on real panes:

- **Redraw residue held the pane busy.** On otto (release 2951c5c9, OTTO-13,
  OTTO-71, OTTO-79), a relaunch resized a Hermes pane into a wider window, and
  prompt_toolkit left `❯ ❯ ❯ ❯` on its input line with the cursor at x=8. Nothing
  was typed, the hook state was idle, and every delivery was requeued as "pane
  busy" for hours.
- **A draft read as idle.** Claude Code indents a draft's continuation lines with
  no prompt glyph. When the current line of a multiline draft is empty, the
  cursor sits at x=2, home, and the old rule called the pane idle. A notice would
  then be typed into the half-written message and submitted with it. This is
  measured on a real Claude Code 2.1.288 pane (captured offline in a private tmux
  server). It is now `tests/fixtures/syrd550_claude_captures.json`
  (`multiline_empty_current`: the rows are `❯ first line of a draft`, `  second line`
  and an empty cursor row at x=2).

Otto's local patch to the shared file (sha prefix `64bca2d4`) fixes the first
direction. It does not fix the second: it walks up only through prompted lines,
and a Claude continuation line is not prompted.

## The rule now

`scripts/ticket_board/pane_composer.py` decides from what the composer shows. It
is a leaf module with no imports, and `_target_cursor_state` imports it when it
runs.

- **Prompt glyphs and whitespace are never text.** These are `❯ > › ▸ ❱`, NBSP
  included, however many there are, so residue reads as empty.
- **A ruled composer** has a `─` rule (at least 40 characters) above the cursor,
  and either a rule below it or only composer-shaped rows in between. It is
  composing when any row inside it holds text, whether that row is above the
  cursor, below it, or the cursor's own row left of the cursor. This is Claude
  Code's box.
- **An unruled composer** is composing when there is text left of the cursor, or
  when the cursor sits on an empty row directly under a prompted row that holds
  text, which is a draft whose rows are each prompted. Plain output above an
  empty prompt is not a draft. A rule far above, such as Codex's update banner
  with output beneath it, is not a composer.
- **On the cursor's own row, only what is left of the cursor counts.** Right of
  it is where an empty box shows its placeholder.

## What it keeps, and where it fails closed

- **Rows are captured unjoined** (`capture-pane -p`, no `-J`), so row *y* is the
  cursor's row. A wrapped row joined by `-J` would shift every row after it.
- **Rows missing from the bottom of a short capture** are empty rows.
- **No capture at all** (tmux failed): the column rule decides, exactly as before.
- **An empty capture,** or a cursor past home with nothing at all left of it,
  also falls to the column rule. Nothing is visible that could say otherwise, so
  the cursor's position still holds the pane. Only prompt glyphs and whitespace
  can turn a cursor past home into idle.
- **A position that cannot be read** is `cursor_state_unavailable`, busy, as
  before.

Holding a notice only delays it. Typing into a draft destroys the draft. So every
unclear case holds the pane.

## Tests

`tests/pane_composer_test.py` runs the real `PaneActivityGate.anti_clobber_trace`
over 22 screens on main (2951c5c, in a child built from a git archive) and on this
tree. tmux is stood in for only as the two calls the gate makes.

- **On main,** residue is `human_composing` and the Claude multiline draft is
  `hook_idle`. Both defects show.
- **On this tree,** the verdicts for empty, resized, cleared, typed, placeholder,
  banner, unruled and wrapped screens are the ones listed above. Exactly 7 cases
  change verdict from main: the defects' own cases and the shapes added to pin
  them.
- **Mutation:** 17 mutants of the rule and its wiring
  (`tests/bounded_run.py mutate`), all killed by an assertion.

Existing suites that needed adapting:

- `pane_activity_gate_boundary_test`, the SYRD-475 move guard, allows exactly
  this one call-time import. It requires that `pane_composer` import nothing. Its
  baseline golden still matches once the one new `capture-pane -p -t <target>`
  call directly after each cursor probe is set aside. More than 100 such calls
  are set aside, and no answer changes.
- `ticket_board_notify_listener_test`: its three sequencing capture helpers model
  the working-timer probe's successive `-J` samples. The composer read (no `-J`)
  now sees the current screen without using a sample up. Per case, the suite
  matches main exactly.

## Not in this change

- **A director notice for a delivery held on "pane busy" for many attempts while
  the hook says idle.** That is new notification plumbing and is left for a
  follow-up ticket.
- **A launcher redraw after a resize.** The gate no longer depends on it.
- **Hermes and Codex were not captured live on this host.** Hermes has no
  provider configured, and Codex's account bootstrap fails. The Hermes residue
  case is otto's reported line and cursor.

## Otto

Otto runs a locally patched `/usr/local/lib/switchyard/ticket_board/pane_activity_gate.py`.
A shared-release install overwrites it. When this fix is deployed to Otto, which
by the User's 2026-10-07 instruction happens only after the Otto report cohort
closes, tell Otto's Director so the local patch is retired rather than left to
drift. Its backups are `/var/tmp/pane_activity_gate.py.bak-20261006` and
`…bak-20261007-prefix2`.
