# SYRD-567: an optional design stage for the designer

Origin: Otto's report of 2026-10-07, item 14 (`OTTO-SYRD-14`). Otto's designer
had no work stage. Its only transition was `release_draft`, so design and
specification work ran outside ownership, review and sign-off, under a
Director hold.

## What was measured

On a board built the production way (schema.sql, the real
`ticket-board-migrate`, rbac.sql), driven through the HTTP API, a declared
designer-owned stage already behaves like any other owned stage:
- the designer is assigned and notified on entry;
- await-role and hand-off work;
- idle reminders reach the designer, and are suppressed while the ticket is blocked;
- a blocked forward move is refused.

Two things were missing:

1. **The design review's kick-back.** A Director's kick-back from design
   review to design was refused while the ticket was blocked, as a "forward
   promotion". Only a `return` is exempt, and a `return` could target only the
   implementation stage. Audit's kick-back into implementation is allowed in
   the same position.
2. **A supported way to add the stage.** Before this, the only way was
   hand-editing the whole document while keeping every Director floor intact:
   the Director can defer from every stage and move work out of every stage.

## What changed

- **A review can hand work back.** A `return` may now also go from a review
  stage to a non-review stage that submits into that review by an ordinary
  forward move. Both validators carry the rule:
  `workflow_config._review_hands_back`, and `validate_declared_workflow` in
  schema.sql and in migration `pgu976`.
  - A return still cannot skip forward, because nothing later submits into
    the review it leaves.
  - Implementation remains a return target exactly as before.
  - Every document that validated before still validates.
- **`switchyard design-stage`** adds the stage to the project's own declared
  document, through the same path as `apply`:
  - validation;
  - a board dry run;
  - the expected revision;
  - a rollback journal;
  - the tenant projection.

## What the command adds

| | Stage or move | Owner or actor | Notes |
|---|---|---|---|
| stage | `design` (kind `system`) | designer | assigned on entry; notified and reminded when the designer has a pane |
| stage | `design_review` (kind `review`) | Director | |
| move | `start_design` | Director | from the draft stage and from triage |
| move | `submit_design` | designer | owner-scoped hand-off to review |
| move | `return_design` | Director | a `return`, with a reason; allowed while blocked |
| move | `accept_design` | Director | into triage, where the ticket is routed to implementation as usual |
| move | defer, under the document's own name | Director | from both stages to the parking stage |

The command finds the draft, triage and parking stages by the shape the
document gives them, not by name. It refuses rather than guesses when:
- the shape is ambiguous;
- the designer is not an active role;
- the stages already exist.

## How a Director enables it

```bash
switchyard design-stage --dry-run   # print the document and projection; change nothing
switchyard design-stage             # apply it
```

`--project` defaults to the project of the pane you run it in. The output names
the journal that undoes it:

```bash
python3 -m scripts.workflow_manage rollback --journal <journal> --config <config> --board-url <url>
```

**A designer without a pane.** A provisioned designer may run as a plain CLI
with no pane target. The board cannot notify or remind such a role, so the
stage is declared silent and the command says so. The Director tells the
designer when work is waiting. Ownership, review, blockers and the record still
apply.

**Root's recorded workflow.** Like every workflow write, `role-prompt set`
included, this changes the board's document and the tenant's projection. Root's
recorded copy is not changed. Reconciling that record is SYRD-561 and SYRD-562.
Until they land, expect `migrate-workflow` and `finish-upgrade` to refuse on the
digest mismatch rather than revert anything.

## How draft release relates to design work

`release_draft` is unchanged. A draft that needs no design still goes straight
to triage, by the designer, the Director or the User as before.

Work that needs a specification takes the design path instead:

1. The Director sends it to `design`, from the draft stage or from triage.
2. The designer submits it.
3. The Director returns it to the designer, or accepts it into triage.
4. From triage it is routed to implementation like any other ticket.

So a draft is an idea, and design is owned work that is reviewed before anyone
implements it.
