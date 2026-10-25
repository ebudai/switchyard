# SYRD-673: Audit's kick-back returns work to the producer of the revision it audited

## What happened

On pie, PIE-1 is a design-review loop. The designer owns Draft and produces the
design. Audit reviews it and either signs off or sends findings back. pie's
workflow declares:

```
audit_kick_back  audit -> in_progress  actors=[audit]  owner_scoped  clear_signoffs=[audit_signoff, user_signoff]
```

`in_progress` is owned by `main`, `app` and `ops`. The executor sends a `return`
to the ticket's recorded implementer, or else the destination's first owner. So
Audit's findings on a design would have gone to `main`, who never touched it,
and never to the designer. The workaround was a narrated Director override back
to Draft every round. That bypassed the sign-off clearing a kick-back does.

## The rule (Director decision, 2026-10-25)

Audit's kick-back returns the ticket to the **producer of the exact revision
Audit reviewed**: the stage and owner that made it.

- **The producer is set by:**
  - **A new revision carried by its holder's own declared transition.** This
    covers an implementer's submission, a no-code completion, and the
    Librarian's documentation commit.
  - **The ticket leaving a stage where its holder produces work.** That is a
    non-review stage whose owner is an implementer or a draft owner (a
    designer).
- **Connectors.** Review stages, the Director's stages and system stages
  produce nothing. A ticket passes through them with its producer unchanged.
- **A material revision resets the producer.** A new revision made any other
  way (a Director's edit, an override) leaves the producer unknown.
- **The immediate Audit-entry source is evidence, not the destination.** That
  is the stage, owner, action and revision the ticket entered Audit with, and
  it is recorded separately.

So:

| path into Audit | the kick-back returns to |
|---|---|
| Implementation (submission) | that implementer |
| Implementation, then Documentation with no change | the implementer |
| Implementation, then the Librarian's documentation commit | Documentation / Librarian, keeping that commit |
| Draft, by the designer's submission | Draft / designer |
| Draft, released to Analysis, then the Director's override | Draft / designer |
| User Review, sent back by the Director, candidate unchanged | its author |

## Refusal and recovery

The kick-back is refused, and nothing moves (no stage, owner, flag, comment or
notice), when:

- the board does not know who produced the revision the ticket carries now. The
  ticket was already in Audit when the board was upgraded, or its revision was
  made or cleared by an edit or an override;
- the producer's stage is gone, terminal, a parking stage, or the stage being
  left;
- the producer is no longer an active role, or no longer an owner of that stage.

The refusal says why and names the repair. The ticket's `audit_return` says the
same, the Director's queue lists it, and the board card shows it. The Director
records the return:

```
ticket-board-write set-audit-return <id> --stage <stage> --assignee <owner> --reason "..."
```

It is the control role's alone. It is validated like the recorded producer, it
moves and reassigns nothing, and it holds only for the revision the ticket
carries now. Audit then kicks back as usual.

The upgrade (`pgu1013`) moves nothing. A ticket already in Audit has no record,
so the Director is told once for each such ticket.

## What is unchanged

- **The declared action.** `audit_kick_back` keeps its actors, owner scoping,
  required reason, and `to` in the document and in `workflow_actions`.
  `workflow_actions` adds `returns_to` (or `refused`) for this ticket.
- **The sign-offs it clears.** These are the ones its declaration names; the
  test compares them with main's. Entering a review stage clears that stage's
  own sign-off, as always.
- **The candidate.** A return clears it, except a return into a review stage
  (the Librarian's), whose owner builds on it.
- **Reservations and serial focus.** A return into Implementation queues
  behind the implementer's active ticket exactly as before.
- **Notifications.** Only the owner it returns to is told.
- **Every other return** (Inspection's, the Director's DAT kick-back, the
  Librarian's, the Publisher's). These go to the recorded implementer as before.
- **`--target-assignee`** is still a confirmation (SYRD-307). For Audit's
  kick-back it must name the producer.
- **Legacy boards** (no declared workflow) are untouched.

## Where it lives

- **`scripts/ticket_board/schema.sql`**, copied in
  `migrations/pgu1013_syrd673_audit_return.sql`:
  - `ticket_audit_returns`, one row per ticket;
  - the `tickets_z_audit_return` trigger (`record_audit_return`);
  - `producing_holder`, `audit_return_status`, `audit_return_problem` and
    `set_audit_return`;
  - the executor change in `enforce_declared_ticket_update`.
- **`scripts/ticket_board/audit_return.py`:** the operation, the
  `--target-assignee` confirmation, and the `workflow_actions` annotation.
- **Read side:** `ticket_read_query.py` (`audit_return`), `read_client.py`
  (ticket text, Director queue) and the board card.
- **`ticket-board-write set-audit-return`.**

## Evidence

`tests/audit_return_test.py` runs the cases on boards built the production way.
It covers the shipped inspection workflow with an active designer owning Draft,
the Librarian's gate and a designer's `submit_design`. It drives the real HTTP
API, `ticket-board-write` and the notify listener. The same scenario runs on
main from a git archive and reproduces the defect: the design goes to `main`,
and the Librarian's commit is dropped on the way to the implementer.
