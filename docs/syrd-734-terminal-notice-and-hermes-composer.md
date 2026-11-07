# SYRD-734: an ended ticket's notice starts no model, and a notice a Hermes turn took is not "unsubmitted"

MEFP, 2026-11-05. MEFP-748, a failed Hermes/Qwen canary, was cancelled while luna-1 held no ticket. luna-1 is an
ephemeral pool worker on the local Qwen model. Its "MEFP-748 … entered Cancelled" notice:

1. **Started a turn.** It started a fresh Hermes conversation, and then a three-minute local-model turn that held the
   one model slot, about 8.6 GiB of GPU memory and 21.8 GiB of RAM. Nobody could act on the ticket.
2. **Raised a false alarm.** The listener told the Director the notice "has sat unsubmitted in its composer …
   pressing submit did not send it". The notice was in the transcript, the composer was empty, and by then luna-1 had
   been stopped on purpose.

## Why

**The turn.** MEFP's board declares luna-1 ephemeral. A `transition` notice is one of the kinds that hands a ticket
over (`SESSION_CLEAR_KINDS`), so a notice to an ephemeral role whose pane does not hold that ticket's conversation goes
through the first hand-off path: `/new now`, then the notice. The eligibility rules keep a transition into a terminal
stage current on purpose (it tells a worker that holds the ticket to stop). So nothing distinguished "tell the worker
holding this ticket that it ended" from "start a worker to read that a ticket it never held has ended".

**The alarm.** It was measured on Hermes 0.15.2 itself, offline: a private tmux pane, a loopback-only stand-in model
server that lists the model and holds every completion open, and no model called. The probe is
`tests/fixtures/syrd734_probe`, and its frames are in `tests/fixtures/syrd734_hermes_0.15.2_turn.json`. While a turn
runs, Hermes's input row reads:

```
⚕ ❯ msg=interrupt · /queue · /bg · /steer · Ctrl+C cancel
```

with the cursor after `⚕ ❯ `. `pane_composer` knows `❯` as a prompt glyph, but not `⚕`, so it read `⚕ ❯` as text in
the composer. After the send that looked like the notice left in an empty composer, so the notice was parked as
unsubmitted (SYRD-565). The proof pass then:
- found the same `⚕ ❯` each time;
- asked whether a turn had started only when the composer read empty;
- pressed submit into the running turn three times;
- reported it.

A pane that was later stopped was never settled either: it could not be read, so its proof stayed open.

## The change

- **`pane_composer`:** `⚕` directly in front of a prompt glyph is Hermes's running-turn mark and belongs to the
  prompt. Hermes's status bar begins `⚕ <model>`, and that stays text.
- **`delivery_proof.left_in_composer`** takes what was typed. Text after a send is the notice left behind only when it
  is the notice's own text, compared without whitespace, since the composer wraps it. That means the whole notice, or
  its end of at least `TAIL_MIN_LETTERS` (64) letters, which is how a composer that scrolled shows a long notice. A
  starting turn's row (Hermes's spinner and status while it initialises) is not the notice's text. Neither is a word or
  a short draft that merely occurs in the notice.

  Audit found the first candidate (`60eebbe6`) accepted any fragment of the notice. Short text like `a`, `Audit` or
  `Qwen worker` then counted as the notice, could be parked, and could later be submitted by the proof pass.
- **The send** (`notification_dispatch`) parks only the notice's own text. After the short receipt window, a composer
  that no longer holds the notice's text counts as the notice having gone in, not only one that reads empty. Its late
  turn is then watched (`receipt_pending`), rather than recorded as unconfirmed.
- **The proof pass** (`delivery_proof._settle_unsubmitted`):
  - a pane that is gone (`target_exists` is false) is never pressed and nobody is told. The proof is abandoned, and
    the notice is dead-lettered as `tmux_target_missing`, as one is when it is claimed for a missing pane;
  - when the composer no longer holds the parked text, either empty or showing something else, and a turn started
    after the send, the notice is delivered. Before, only an empty composer was asked about a turn, and anything else
    was reported to the Director as edited.
- **A notice about an ended ticket never spends a session clear or restore.** In `_clear_role_session`, which runs only
  when the role is ephemeral and its pane does not hold the ticket's conversation, a ticket in a terminal stage
  (`eligibility.terminal`, from the workflow's own terminal stages) ends the notice. It is discarded as
  `terminal_notice_without_conversation`, with a `drop` trace saying so. Nothing is typed.
- **What stays the same:**
  - a worker that holds the ticket's conversation is still told it ended, with no clear;
  - a non-ephemeral role is unchanged;
  - every hand-off of live work still clears and delivers.

The listener file stays at its 1250-line ceiling: the terminal check is wired in on an existing line.

## Evidence

**`tests/terminal_notice_ephemeral_test.py`:**
- **The board:** a production-built board with the example workflow; `app` is an ephemeral Hermes role, `ops` a Hermes
  role.
- **The real parts:** the HTTP server, notify listener, PaneActivityGate and hook-state store.
- **The stand-ins:** tmux and directorctl only. The panes show Hermes's real frames, and Hermes writes its
  `pre_llm_call` hook as a turn starts.
- **Order:** main (`6d96bacb`) runs first, from a git archive.

**Main:**
- reads Hermes's running-turn row as `⚕ ❯`;
- presses submit three times on a notice Hermes took, and tells the Director it sat unsubmitted;
- answers a cancelled ticket's notice to an idle ephemeral worker with `/new now`, then the notice.

**Here:**
- **The composer reads:** empty when idle and while a turn runs; exactly what was typed when typed; and the status
  bar's `⚕` is still text.
- **A ticket handed to app, the hook prompt:** delivered and acknowledged; nothing parked, pressed or reported.
- **The hook late, the row showing the running turn:** the notice's receipt is awaited, then delivered.
- **The hook late, the row showing the starting turn:** the same, typed once.
- **A ticket app never held, cancelled with app idle:** nothing is typed, and the notice is discarded as
  `terminal_notice_without_conversation`.
- **A ticket app is working on, cancelled:** app is told, with no clear.
- **A long notice left in a composer that shows only its tail:** still read as the notice left behind. Hermes
  showed the last 1,021 of 2,710 letters.
- **Text that merely occurs in a notice is not the notice:** `a`, `Audit`, `notice`, `Qwen worker`, a long notice's
  last word, a short notice's last word or phrase, and 400 letters from the middle of a long notice. Only the whole
  notice is.
- **A short word from the notice in ops's composer after the send** (a draft begun with `entered`): not parked, and
  never submitted. Under the first candidate's rule it was parked and then submitted.
- **A notice really left in ops's composer:** parked, submitted once, delivered.
- **Parked text someone else submits, the row showing the starting turn:** delivered, not reported as edited, typed
  once.
- **Parked text whose pane is then stopped:** abandoned, nothing pressed, nobody told, dead-lettered as
  `tmux_target_missing`.

**Mutants:** 19, each killed by a failed check:
- **The parser:** `⚕` never stripped, or stripped wherever it appears.
- **The parked text:**
  - not compared;
  - only the whole notice accepted, so a long notice's tail stops counting;
  - any fragment accepted (the first candidate's rule);
  - any end, however short;
  - any 64 letters from anywhere;
  - a 4-letter tail minimum;
  - no text passed at the send.
- **The receipt check:** the late-receipt check back to "the composer reads empty".
- **A gone pane:** its check removed, its dead-letter removed, or the Director told.
- **The started turn:** its check removed, or kept to an empty composer only.
- **The terminal rule:** removed, unwired from the listener, terminal meaning only `done`, or the notice not
  discarded.

The neighbouring suites (delivery proof, composer, ephemeral sessions, context continuation and restore, composer
hold, Codex banner, Copilot box, replaced-provider redelivery) pass on main and here.

Nothing live was changed: MEFP was only read through its board's loopback GETs (`/api/workflow`, `/api/board`).
