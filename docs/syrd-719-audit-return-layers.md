# SYRD-719: Audit tells the work's origin from the Librarian's documentation layer

## What happened

SYRD-673 made Audit's kick-back return work to the producer of the exact
revision Audit reviewed. A Librarian documentation commit is a new revision
carried by its holder's own transition, so it became the producer. Every Audit
finding on that combined candidate then went back to the Librarian, including
code, test, configuration, security and behavioural defects the Librarian may
not touch. Main reproduces this: Audit's kick-back on a Librarian commit lands
on Documentation/librarian (`tests/audit_documentation_return_test.py`, the
upgraded board, before its migration).

## The two layers

The board now records two layers for each ticket.

- **The substantive origin** is the producer SYRD-673 records: the stage, owner,
  action and revision the work entered the review chain from. That is
  Design/a designer, Implementation/an implementer, or another work-producing
  stage. A review stage is never the origin.
- **The documentation layer** is the ticket's latest pass through the
  Documentation stage, by its holder's own decision: a documentation commit
  (`documentation_sign_off`) or no change (`documentation_no_change`). It
  records the stage, the owner, the revision the ticket left with, and the
  **base**. The base is the revision the layer was built on. On a later round
  over the same layer, the base stays the one that layer already had, so it is
  always the origin's work.

The origin holds for the revision under review when it made that revision, or
made the base of the documentation layer the ticket carries.

A new origin, or a revision made any other way (a Director's edit, an
override), ends the documentation layer. A Documentation kick-back clears the
candidate, so it records no pass.

## Audit chooses

Audit makes one explicit decision. The board never classifies a finding by its
wording, the files it names, or who made the latest commit.

| Action | Goes to | Clears | Candidate |
|---|---|---|---|
| `audit-kick-back` (the default) | the substantive origin | what its declaration clears, plus the Documentation sign-off | cleared, as for any return to non-review work |
| `audit-documentation-kick-back` | the Documentation stage and owner the ticket passed | Audit's and the User's sign-offs, and the Documentation sign-off | kept: the exact candidate Audit reviewed |

- **The ordinary kick-back** takes every substantive or mixed finding: code,
  tests, configuration, executable examples, embedded CLI help or usage text,
  and security. The Documentation sign-off is owed again, so the next
  submission, even of the same commit, passes the documentation gate again
  while the gate is on.
- **The documentation return** is declared by the documentation gate, out of
  each stage `audit_kick_back` leaves, by the same actors. It requires a
  reason. It is refused, moving nothing, unless all of these hold:
  - the live workflow declares it, which needs an active Documentation stage;
  - the ticket's last Documentation pass left it with the revision under
    review;
  - that stage and owner still take work (the owner is active and still owns
    the stage).

  The refusal names the ordinary kick-back as the way back.

Both returns name exactly one destination. Only that owner is told. A return
into Implementation queues behind the implementer's active ticket as before.
`--target-assignee` is a confirmation of that owner and nothing else, and a
payload `assignee` or another stage is not taken.

## Read surfaces

- **`audit_return`** keeps its keys for the origin. It adds `documentation`
  where the workflow declares the documentation return from the ticket's
  stage, with these keys: `valid`, `stage`, `assignee`, `by`, `commit`, `base`,
  `problem` and `otherwise`.
- **`workflow_actions`** carries `returns_to` (or `refused`) on both actions.
- **The ticket text** (`ticket-board-read ticket`) names both destinations.

## Workflow changes and repair

- **Turning the gate off** (`without_documentation_gate`) removes the
  documentation return. The origin still stands, so the ordinary kick-back
  works.
- **Turning it back on under the Director** leaves an earlier Librarian layer
  unreturnable while the Librarian is inactive.
- **`set-audit-return`** records the origin only, and refuses a review stage.
  On a ticket carrying a documentation layer, it records the origin of the
  work under it.
- **Nothing records a documentation layer by hand.** Where the board has none,
  Audit uses the ordinary kick-back.

## The upgrade (`pgu1022`)

The upgrade moves nothing.

- **The Librarian's commit.** A record where that commit stood as the producer
  becomes the documentation layer, with no known base. The work under it has
  no recorded origin: nothing is inferred.
- **A Director's record naming a review stage** is unrecorded too.
- **The Director is told once** for each such ticket in Audit, with the repair
  (`set-audit-return`). A round begun on main finishes on the record: the next
  Librarian commit layers on the old one, and the Director's record holds for
  that base.
- **A Documentation pass with no change, before the upgrade**, was never
  recorded. Its documentation return is refused rather than invented.
- **A board with the documentation gate** gains
  `audit_documentation_kick_back` as a new workflow revision, exactly as
  `documentation_gate.with_documentation_gate` composes it.

## Where it lives

- **`scripts/ticket_board/schema.sql`**, copied in
  `migrations/pgu1022_syrd719_audit_return_layers.sql`:
  - the documentation-layer columns of `ticket_audit_returns`;
  - `record_audit_return`;
  - `audit_return_status`;
  - `substantive_origin_problem`;
  - `set_audit_return`;
  - the executor (`enforce_declared_ticket_update`);
  - the validator, which admits this one return into a review stage.
- **`scripts/ticket_board/documentation_gate.py`** composes, removes and
  validates the return. `workflow_config.validate` admits it.
- **`scripts/ticket_board/audit_return.py`** handles confirmation and
  advertisement, and `read_client.py` the ticket text.
- **`ticket-board-write audit-documentation-kick-back`**.

## Evidence

`tests/audit_documentation_return_test.py` runs on boards built the production
way, with the shipped inspection workflow, an active designer and the
Librarian's gate. Writes go through the HTTP API, `ticket-board-write` and the
board service's connection, and the real notify listener delivers. It covers:

- Design, Implementation, and Documentation with no change;
- the Librarian's commit, with substantive, mixed and documentation-only
  findings over three rounds;
- spoofed targets, stages and actors;
- the gate off, removed, and re-owned by the Director;
- a board without the gate;
- an upgrade from main with tickets in flight;
- document and schema parity between schema.sql, the migration and the
  upgrade.

`tests/audit_return_test.py` (SYRD-673) now expects the implementer for the
Librarian's commit.
