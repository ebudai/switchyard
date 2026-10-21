# SYRD-640: an empty Copilot input box is not a human's draft

## What happened (syrd, 2026-10-21, SYRD-634 acceptance)

Inspector ran Copilot CLI 1.0.101: signed in, Allow All. After a completed
turn, its hook recorded `copilot.agentStop`, idle. Copilot drew its input as a
filled box:

```
╻▄▄▄▄▄▄▄▄…    row 39
┃             row 40, the cursor at x=2 (cursor 2 40 43)
╹▀▀▀▀▀▀▀▀…    row 41
```

There was no prompt glyph. `pane_composer._text()` strips only prompt glyphs
and whitespace, so it read the bar `┃` as draft text. The gate answered
`human_composing`, and handoff SYRD-639 stayed queued as "pane busy" through
four retries. The same capture also made delivery proof's `composer_text`
read `'┃'` rather than `''`.

`tests/fixtures/syrd640_copilot_input_box.json` is that pane exactly. It holds
the 43 rows that `tmux capture-pane -p` returned and the cursor, taken from the
Director's own capture during the UAT.

The box is drawn only for the signed-in account. The same binary run offline
draws its plain `❯` between `─` rules, as SYRD-634's recordings do. That was
tried under bubblewrap with no network and a throwaway home, using `--allow-all`,
`--experimental`, true colour and a 106x43 pane. So the live capture is the only
recording of this style.

## The change: `scripts/ticket_board/pane_composer.py`

A filled input box is recognised from its own edges:
- the nearest rows above and below the cursor that are not behind the bar `┃`
  must be a top edge `╻` + `▄`… and a bottom edge `╹` + `▀`… (at least 40 of
  each);
- every row between them must be behind the bar, the cursor's included.

Inside that box the bar is decoration and whatever follows it is the draft.
That holds for every row, and for the whole of the cursor's row, right of the
cursor as well as left: the empty box shows no placeholder, so text there is
a draft whose cursor was moved back into it. `composing` and `composer_text`
both read the box this way.

The transcript's earlier prompts are `▄`/`▀` blocks without the `╻`/`╹`
corners. Those, a bar without both corner edges, and a row between the edges
that is not behind the bar are all read exactly as before. No provider name is
read anywhere: the box is recognised by its shape.

## Tests: `tests/copilot_input_box_test.py`

The real PaneActivityGate runs over a real hook-state store (`copilot.agentStop`,
idle), with tmux stood in by its two answers. The before-run is main `41dd7c8`,
from a git archive.

| case | main | this tree |
|---|---|---|
| the live empty box | `human_composing`, text `┃` | idle, text `''` |
| a one-line draft, its cursor at the end or moved back to the start | composing | composing, `hello there` |
| text against the bar, `┃hello` | composing | composing, `hello` |
| a two-line draft, the cursor at its end or on its empty second row | composing | composing |
| two empty box rows | composing | idle |
| a bar without the corner edges; a top edge with no bottom edge; an unbarred row between the edges | composing | composing (unchanged) |
| Copilot's permission question (SYRD-634's recording) | `provider_prompt` | `provider_prompt` |

**Delivery.** The real notify listener runs on a production-built board
(schema.sql, ticket-board-migrate, rbac.sql, the example workflow). The
Director moves PGU-639 into Inspection for Inspector, and four listener passes
follow:
- **On main:** it stays queued as `pane busy`, deferred as `human_composing`.
- **On this tree:** it is sent once, with nothing left queued and no deferral.
  The listener's own witness sees the turn start in Copilot's hook
  (`copilot.userPromptSubmitted`), and nothing ever types into the pane except
  the one send.

**Mutation:** 7 mutants, all killed by their named checks:
- the box ignored, by `composing` or by `composer_text`;
- an edge with no corner;
- a box with no bottom edge;
- the bar read as text;
- only what is left of the cursor counted;
- an unbarred cursor row accepted.

**Neighbours, on main and this tree:** `pane_composer_test` 22,
`codex_update_banner_composer_test` 29, `delivery_proof_test` 20,
`composer_hold_alert_test` 22, `pane_activity_gate_boundary_test` 291 and
`provider_prompt_test` 57. `ticket_board_notify_listener_test` and
`copilot_runtime_test` are red on both trees: the listener suite with the same
44 failing tests, compared test by test, and the Copilot suite on the same
check.
