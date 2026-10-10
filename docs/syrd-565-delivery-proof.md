# SYRD-565: delivery proves input, submission and receipt apart

## What otto saw (release 2951c5c9, report item 11)

- **A slow turn recorded as a failure.** Four of eleven "receipt not
  witnessed" notices were real turns. Their first act, a slow preflight
  compression, started 15–16 s after the send, just past the listener's fixed
  witness window. They were recorded `send_unconfirmed` and acknowledged, and
  nothing looked again.
- **An unsubmitted nudge recorded as delivered.** Its text was typed into a
  Hermes composer and never submitted, for 50 minutes. The failure showed only
  on directorctl's unread stderr.

Both come from one rule. After `directorctl send`, the listener waited a fixed
window for a turn in the recipient's hooks. Anything else was recorded
unconfirmed and acknowledged, which removes it from the queue. Nothing
distinguished "taken slowly" from "never left the composer". Meanwhile:

- **directorctl's own check is weak.** It treats any change in the pane after
  Enter as a submission, and Hermes redraws on its own.
- **A failed directorctl was requeued.** If directorctl failed after typing,
  the notice went back on the queue with its text still in the composer. Every
  later notice to that pane was then held behind it as "pane busy".

`tests/delivery_proof_test.py` measures each of these on main (edf50b3).

## Three proofs

1. **Input.** directorctl typed the text, shown by its exit status, as before.
2. **Submission.** The text left the composer. The listener reads the
   composer before and after the send, from an unjoined `tmux capture-pane`
   and the cursor, through the gate's own runners. It uses
   `pane_composer.composer_text`, the same composer SYRD-550's `composing` reads:
   - a ruled box's rows (Claude Code);
   - otherwise the cursor row and the prompted rows of a draft above it
     (Hermes);
   - on the cursor's row, only what is left of the cursor, because right of it
     is where an empty box shows its placeholder.

   The gate sends only to an idle pane, so an empty composer before the send
   and text after it is this notice's text, still there.
3. **Receipt.** The recipient's runtime started a turn, shown by its own hooks,
   as before (SYRD-268).

## What each outcome does

| Outcome | Before | Now |
|---|---|---|
| Text left, turn within the window | `send`, delivered | the same |
| Text left the composer, no turn within the window | `send_unconfirmed`, acknowledged, final | acknowledged and its **receipt watched**, reason `receipt_pending`. A later turn records `send` (`receipt_late`), so the ticket reads delivered. No turn within 15 minutes records `receipt_missed`, and the Director is told once. |
| Text still in the composer | `send_unconfirmed`, acknowledged | **never acknowledged**: `send_unsubmitted`, kept queued and parked, reason `unsubmitted_in_composer` |
| directorctl failed after typing | requeued, so the text is typed again or held as "pane busy" | the same as text still in the composer |
| Composer unreadable | `send_unconfirmed`, acknowledged | the same. "Cannot tell" never reads as either answer. |

### Parked, unsubmitted notices

The proof pass, `delivery_proof.run`, runs every listener loop before delivery.
For each parked notice:

- **No longer current:** if the ticket moved, was cancelled, or the notice was
  otherwise superseded (the listener's own `_notification_is_current`), the
  notice is discarded and submit is not pressed. The Director is told once that
  stale text may remain in that composer.
- **Composer empty:**
  - if a turn started since the send, someone else submitted it and it was
    taken: the notice is acknowledged and delivered, never typed again;
  - if not, someone cleared the text and nothing took it, so the notice is
    released to be typed again into the empty composer.
- **Composer holds exactly what the send left there** (compared by SHA-256):
  submit is pressed with `directorctl submit <target>`, which types nothing.
  If the text then leaves the composer, the notice becomes a submitted one and
  its receipt is watched. After 3 presses that do not take, the Director is
  told once, and the notice is still never acknowledged or typed again.
- **Composer holds something else:** somebody edited it, so it is theirs now.
  Submit is never pressed on another's text, and the Director is told once.

### Restart safety

Both states live in `ticket_board.notification_proofs` (pgu979), so a
restarted listener picks them up rather than sending again. Each state change
is made in the same transaction as the queue change that goes with it:
`record_unsubmitted_notification` requeues, and `watch_notification_receipt`
acknowledges.

### Visible on the ticket

The existing `active_work_delivery` states carry the new outcomes:

- **pending**, reason `unsubmitted_in_composer`: still queued, held;
- **unconfirmed**, reason `receipt_pending` or `receipt_missed`;
- **delivered**, once a late receipt is witnessed.

Director notices are `ticket_update`s with payload kind `delivery_proof`.

## Size

`notify_listener.py` is 1,249 lines, so the pass is hooked in on lines that
already existed: its import and the pull-pickup call. The new logic lives in
`delivery_proof.py`, `notification_dispatch.py` and `pane_composer.py`.

A listener can be newer than its board's migrations, so the pass first checks
once that `open_notification_proofs()` exists, and skips otherwise.

## Tests

`tests/delivery_proof_test.py` runs on a production-built board (schema.sql,
the real ticket-board-migrate, rbac.sql) with the example workflow, the real
HTTP server, the real notify listener, and the real PaneActivityGate and
hook-state store. tmux is stood in for by a pane whose composer the sender
types into. Every later pass runs on a new listener, so each case also covers
a restart. It is checked against main (edf50b3) built from a git archive.

- **On main:**
  - a slow turn stays unconfirmed for good;
  - text left in the composer is acknowledged and dropped;
  - a directorctl that fails after typing is requeued, and its own text holds
    it as "pane busy".
- **Normal:** delivered once, with nothing owed.
- **Slow preflight:**
  - pending at the window, and shown so on the ticket;
  - still pending, with nobody told, while inside the horizon;
  - delivered through a restarted listener once the turn starts, sent once;
  - past the horizon with no turn, reported `receipt_missed`, and the
    Director told once.
- **Text left in the composer:**
  - kept queued and never acknowledged, shown pending on the ticket;
  - a restarted listener presses submit once, typing nothing, and it is
    delivered;
  - a submit that never takes is pressed three times, the Director is told
    once, and it is never acknowledged or retyped;
  - edited text is never pressed;
  - text someone else submitted and a turn took is delivered, not retyped;
  - text someone cleared is typed again;
  - text superseded by the ticket's cancellation is discarded, never pressed,
    and the Director is told once.
- **directorctl fails after typing:** parked, never retyped.
- **`composer_text`:** an empty box, a placeholder, typed text, a multiline
  draft, Hermes residue, a Hermes prompted draft, output above an empty
  prompt, a short capture and an unreadable cursor.
- **The shipped default, once:** `DirectorctlSender.submit` through the
  installed `directorctl`, against a recording tmux, presses Enter on the
  target and types nothing.
- **Grants and parity:** the new functions are executable only by the listener
  (and their owner), and a fresh schema.sql board installs the same text.
  These checks run last.

**Adapted neighbour suites:**
- `idle_nudges_boundary_test`: the listen loop's measured statements end with
  the pass's probe and read, after SYRD-539's policy read and before delivery.
- `ticket_board_stale_prior_turn_child_work_test`: a notice out of the
  composer with no turn witnessed is now acknowledged by the receipt watch,
  inside the database, rather than by an explicit ack.

**Mutation:** 23 mutants, run with `tests/bounded_run.py mutate` and
`SYRD565_MUTATION_SKIP_COPY_PARITY=1`, so copy parity never stands in for
behaviour. They cover the migration, the proof pass, the send path, the
composer reader and `directorctl submit`. All 23 are killed by assertions,
and each kill line was read.

## Otto

When this is deployed to Otto, tell Otto's Director, so any local workaround
for these delivery failures is removed rather than left to drift.
