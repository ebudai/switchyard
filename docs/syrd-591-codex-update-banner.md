# SYRD-591: Codex's update banner is not a composed draft

## What MEFP saw

SYRD-589 was deployed to MEFP at 2935434. luna-9 was restarted with `switchyard
worker-pool mefp restart luna-9`; its pane was seeded `team_launcher.start`
three seconds after its provider started, and it showed a fresh Codex 0.162.0
at its empty prompt. MEFP-51's notice (6781) was still retried as `pane busy`,
attempt 113, with `active_work_notified_at` empty. Trace event 210288 named the
cause `human_composing`, from a composer snapshot that was active with 328
characters (sha256 `e9a843c0...`). The screen had only Codex's startup banner
(`Update available! 0.162.0 -> 0.162.1`), the header and footer, and the empty
`Ask Codex to do anything` prompt.

## The screen, measured

Codex 0.162.0 ran in a private tmux server (160x45), inside bwrap with no
network, a throwaway `CODEX_HOME` logged in with a placeholder key, and the
repo trusted. Codex reads the banner from its own update cache,
`CODEX_HOME/version.json` (`latest_version`, `last_checked_at`,
`dismissed_version`), and a recent `last_checked_at` keeps it off the network.
So writing `{"latest_version": "0.162.1", "last_checked_at": <now>}` there
brings up the real banner offline. Keys were typed into the composer only and
never submitted. The captures are in `tests/fixtures/syrd591_codex_update_banner.json`.

```
 0|╭───────────────────────────────────────────────────────────────╮
 1|│ ✨ Update available! 0.162.0 -> 0.162.1                       │
 ...
 6|╰───────────────────────────────────────────────────────────────╯
 9|  >_ OpenAI Codex (v0.162.0)
10|     /tmp/w/repo
14-30|  (the logo, indented)
41|› Ask Codex to do anything          <- cursor (2, 41)
43|  GPT-6.1-Sol default · /tmp/w/repo
44|  ← for agents · ? for shortcuts
```

On this screen, main's composer snapshot is active with exactly MEFP's 328
characters and sha256. The banner is the same box at any pane width, and the
snapshot reads only its inside, so the fixture is MEFP-51's screen.

## Why it read as a draft

The banner is a closed, rounded box, and its borders are rules of 63 `─`.

- **The gate** (`pane_composer.composing`) takes the nearest rule above the
  cursor as a ruled composer's top. That was the banner's bottom border, row 6.
  With no rule below the cursor, it calls the region a composer when every row
  between is composer-shaped: blank, prompted, or indented. Codex's header,
  logo and footer rows are all indented. So the header's text was a draft,
  `human_composing`, at the first gate and the recheck, on every retry.
- **The snapshot** (`composer_snapshot_from_pane_text`) takes the last two
  rules, which were the banner's own top and bottom. It read the banner's text
  as 328 characters of draft. The snapshot is only diagnostic (`composer_before`,
  `suspected_clobber`), but it is what the trace showed.
- **The delivery proof** (`delivery_proof.read_composer`, through
  `pane_composer.composer_text`) read the header as the composer's text too. So
  `left_in_composer` could never tell whether a send to such a pane stayed
  unsubmitted.

## The fix

All of it is in `scripts/ticket_board/pane_composer.py`, plus the snapshot's
rule filter in `pane_activity_gate.py`.

- **A box that closed above the cursor does not hold it.** When the nearest
  rule above the cursor is a box's bottom border (a `╰ └ ╯ ┘` corner at either
  end), it is no composer's top rule, and the cursor is read as unruled. A
  cursor inside a box still has that box's top border as its nearest rule above,
  so a box composer is read as before. Plain rules, like Claude Code's, are
  unchanged. The rule below the cursor is unchanged as well: no box below a
  cursor was measured.
- **The snapshot reads only rules without corners.** It is Claude Code's ruled
  composer that it reads. A box composer's snapshot always counted its side
  bars as text, so it was active on every capture and told nothing. Now it
  finds no composer there.
- **A Codex draft's empty continuation line is still a draft.** Codex does not
  rule its composer. A draft's first row is prompted (`› fix the build`) and its
  further rows are indented under it. With the cursor at home on an empty third
  line, main held that draft only because the banner made every screen look
  typed into. Without a banner, main read it as no draft. Now, when nothing
  left of the cursor is a prompt, indented rows with text directly above a
  prompted row with text are the draft's rows. That holds the draft and
  `composer_text` reads all of it. An idle Codex cursor sits on its prompted
  row, `› `, so this never applies to it.

Nothing else changes: not the anti-clobber decision, not the column rule, not
the placeholder allowance, and not Claude's ruled composer.

## What the screens give now

The real gate, with luna-9's hook state (a launcher seed three seconds into a
fresh provider) and its process shape (SYRD-589's daemon):

| screen | main | this tree |
|---|---|---|
| banner, empty prompt | busy `human_composing` at both gates | idle `working_timer_idle` at both gates |
| banner, `fix the build` typed | busy `human_composing` | busy `human_composing` |
| banner, cursor on the draft's empty second line | busy `human_composing` | busy `human_composing` |
| banner, two lines typed | busy `human_composing` | busy `human_composing` |
| banner, cursor on the draft's empty third line | busy `human_composing` | busy `human_composing` |
| no banner, cursor on the draft's empty third line | **idle** (the draft would be typed into) | busy `human_composing` |

On this tree each banner screen reads exactly as the same screen without the
banner.

## Not changed, seen along the way

- **`directorctl` has its own copies** of the "last two rules" reading:
  `is_director_typing` and `composer_state_json`. The first guards only the
  Director's own pane, a Claude pane whose composer is plainly ruled. The
  second is a diagnostic. Neither is on the worker delivery path that held
  MEFP-51, so neither is changed.
- **No Director alert.** SYRD-570's composer-hold alert did not fire for
  MEFP-51. Its hook state was the launcher's seed, which is not a trusted idle
  source (`trusted_idle=false`), so the 113 holds were never reported.
- **A Codex draft with the cursor moved to the start of its first row** reads
  as no draft, with or without the banner. In a plain capture,
  `› fix the build` with the cursor at home looks exactly like the empty
  prompt's placeholder `› Ask Codex to do anything`. This is unchanged from
  main without the banner.
- **Codex's update check is left on.** The Director ruled out
  `check_for_update_on_startup=false`, and nothing here needs it.

## Evidence

`tests/codex_update_banner_composer_test.py` (29 checks) runs main's code (BEFORE =
2935434, the release MEFP runs) in a child built from a git archive, then this
tree.

- **On main:**
  - the captured banner gives MEFP-51's snapshot (328, `e9a843c0...`);
  - the idle pane is held `human_composing` at both gates, and its composer
    reads as the Codex header;
  - on a production-built board with the real server, listener and gate, the
    notice is requeued as `pane busy` on every retry and never sent.
- **On this tree:**
  - the same notice is delivered at once, exactly once. Its trace records an
    empty composer and no suspected clobber, and later passes send nothing;
  - with a draft under the banner, the notice is held (`human_composing`,
    nothing typed). Once the draft is gone, it is delivered once;
  - every reader shape reads as on main, apart from the box snapshots
    described above: Claude Code's real captures, Codex's SYRD-566 screens,
    every SYRD-550 shape, boxes the cursor is inside, and Claude's ruled
    composer under a box;
  - two shapes that are not drafts: indented output above a blank cursor
    row, and a prompted line's continuation above an empty prompt.

## Guard adapted

`tests/pane_activity_gate_boundary_test.py` (SYRD-475) lets a function that
reads `notify_listener` import nothing else. The snapshot now also imports the
leaf module `pane_composer` after it, to ask which rules are a box's edges. The
guard already lists a leaf import like this for `_target_cursor_state`
(`LATER_IMPORTS`). It now lists this one too, and requires it to come after the
`notify_listener` import. The guard still passes all 290 checks. Two mutants,
another import and the imports in the other order, are both caught.

## Neighbours

Twenty-eight suites that build the gate or read the composer ran serially on
main (2935434) and on this tree:

- 23 pass on both;
- 5 are red on main, and their normalised output is identical on this tree:
  `claude_permission_hook_test`, `desktop_access_test`,
  `pane_state_boundary_test`, `ticket_board_listener_pane_state_authority_test`
  and `ticket_board_notify_listener_test`;
- `pane_activity_gate_boundary_test` went red on this tree, for the import
  above, and is green again after the guard change.

## Mutation

8 mutants, run through `tests/bounded_run.py mutate` in the foreground against
this test and `tests/pane_composer_test.py`, are all killed:

- a closed box above the cursor still read as a top rule;
- the snapshot keeping box edges;
- `box_border` never true;
- continuation rows off;
- continuation without a prompted head;
- continuation under a prompted cursor row;
- `composer_text` reading prompted rows only;
- `composing` ignoring continuation.
