# SYRD-599: the Router role

An optional, low-cost role whose only job is to choose the Implementation owner
for tickets the Director has already admitted to Implementation, or to hand a
ticket back to the Director's triage when that choice is not its to make.

## The flow

1. The Director routes a ticket from triage (`analysis`) into Implementation
   the ordinary way, naming no owner (`ticket-board-write route <id> --state
   <stage>`, or "Route through Router" in the UI). The board assigns it to
   `router` in the same statement.
2. The board tells Router, once. Router reads the ticket once
   (`ticket-board-read ticket <id> --json`). Its `routing` field carries the
   `revision` Router classifies and the `assignable` owners.
3. Router makes exactly one write:
   - `ticket-board-write router-assign <id> --assignee <owner> --revision <r>`;
   - `ticket-board-write router-return <id> --reason "..." --revision <r>`.
4. Router yields. The board's existing queues decide when the chosen owner works
   the ticket, and the Director receives a returned ticket as one handoff.

## Router is the default; direct routing stays the Director's

In `enforce_declared_ticket_update`, a Director `move` into the Implementation
stage from a stage that is neither Implementation nor a review, on a board
with an active Router, is assigned to `router` unless it names an active
Implementation owner. That covers a route with no assignee, or naming `router`.

- **Router unavailable.** If Router is not available (`router_available`: no
  registered runtime matching its declaration, so its notice would requeue for
  ever), the route is refused with a message naming the direct route. It is
  never given to an implementer instead.
- **Direct route.** Naming an owner (`--assignee ops`, or "Assign directly"
  with its owner selector) is the Director's ordinary direct route. It keeps
  every stage-owner, serial-queue and notification rule, records no Router
  decision, and works whether or not Router is running.
- **Without Router.** A board without an active Router routes exactly as
  before.

## Authority

Router never holds a generic Director capability. Its authority is two
purpose-built operations, and each check below is made by **both** the HTTP
layer (`scripts/ticket_board/router_role.py`) and PostgreSQL (`router_assign`,
`router_return`, `require_router_change`), neither trusting the other.

| | `router_assign` | `router_return` |
|---|---|---|
| Caller | exactly the active `router` role, the only holder of capability `router_assign` | exactly `router`, the only actor of the declared `router_return` |
| Ticket | in the Implementation stage, assigned to `router`, not held by the Director | the same |
| Target | an active `implementer` that owns the Implementation stage, never `router` | `analysis`, assignee `director`, nothing else |
| Payload | exactly `assignee`, `revision` | exactly `reason` (non-empty, at most 2000 characters), `revision` |
| Stale / duplicate | the revision must equal the ticket's current one; once assigned the ticket is no longer Router's | the same |
| Changes | the assignee only | the stage and assignee, through the declared transition's ordinary bookkeeping, and a comment with the reason |
| Record | `router_decisions`: Router, the role chosen, the revision classified | `router_decisions`: Router, the reason, the revision |

**The CLI and the UI.**
- `ticket-board-write` run as `router` refuses every subcommand but
  `router-assign` and `router-return` before asking the board.
- In the board's own page, with Router chosen as the author, its two writes
  (and the comment box that holds a return's reason) are the only usable
  controls.

Neither is the boundary; the board is.

**The window.** Each operation opens a one-ticket, one-statement window
(`ticket_board.router_ticket`, `ticket_board.router_action`).
`enforce_declared_ticket_update` admits, inside it, exactly that operation's
change (`require_router_change`), and names any other field a statement tried
to change. It refuses `router_return` outside the window, so
`perform_workflow_action`, a crafted payload or a direct statement cannot take
it.

**The revision** (`router_ticket_revision`) is a digest of everything Router's
one read shows, plus the assignment round:
- every ticket field except the bookkeeping a write stamps;
- the blockers;
- the comments, where an ownership decision is often written;
- the attachments.

A Director comment or edit after Router's read, an attachment, or a hand-back
and re-route each make the earlier read stale. Audit's kick-back of
2026-10-17 found the first version ignored comments.

**Pull-scheduled boards.** There the board diverts work for a busy implementer
into the ready stage. Router's pick changes the owner and nothing else, so
assigning to an owner with an active ticket is refused at both boundaries
instead of moved. Router chooses a free owner or returns the ticket.

## No reservation

Router is a legal Implementation assignee **without owning the stage**. The
only exception is `router_holds(stage, assignee)`, used in
`require_stage_owner_assignee`, `ticket_valid_assignee`,
`transition_target_role` (so Router is notified), the workflow-apply orphan
check, the stage-entry default assignee, and the trigger's owner fallback.

Owning the Implementation stage is what makes a role an implementer, so Router
is excluded from all of the following:
- `ticket_is_implementer_assignee`, `ticket_serial_waiting`, `serial_queue()`,
  `serial_focus` and `settle_serial_focus`;
- the pull claimant pool and the pull-policy diversion (which also skips it
  explicitly);
- `remember_declared_implementer`, so it is never anybody's last implementer.

Its notices are not serial-gated (it owns no stage). They go out one at a time
through the pane-activity gate, as Router's pane finishes each turn.

## Choosing it when a project is created

Router is a default-off choice in `switchyard new` and in the design artifact
(`project.router`, `team-launcher design --router-runtime`). It is offered only
with two or more implementers. A project created with it starts with a declared
workflow, the one its plan describes, composed as `migrate-workflow` composes
it, naming in `remove_stages` the seeded stages it does not use. Router is
added by `with_router`. See `scripts/router_onboarding.py` and
`docs/team-launcher.md`. Off, nothing changes. Upgrades never read the
artifact, so a release never turns Router on.

## Declaration

A document declares Router with `switchyard router-role <project> enable
--runtime <cli> [--template-role <role>]` (`router_setup.with_router`, through
`workflow_manage`'s apply path). Both validators (`router_role.validate_router`,
`validate_router_declaration`) enforce the same rules:

- **The role:** one role of kind `router`, named `router`. It holds
  capabilities exactly `["router_assign"]`, which no other role may hold. It is
  never `serial`, owns no stage, and is the actor of no transition but
  `router_return`.
- **Its move:** `router_return` goes from Implementation to `analysis`. Its
  actor is `router` alone; it is owner-scoped, needs a reason, needs no
  commit, and clears no sign-off.
- **When active**, the document must also have:
  - a pane target for Router;
  - an Implementation stage that notifies its assignee;
  - `analysis` owned and announced to `director` alone;
  - `implementer_kick_back` from Implementation to `analysis` for every
    Implementation owner, owner-scoped with a reason. This is the misroute
    escape the Director asked for.

Router is detached (`slot: null`) and gets its own onboarding prompt
(`router_setup.ROUTER_PROMPT`), which names its read and its two writes and
nothing else. Its provider, model and effort are the launcher's, like any
role's: copied from `template_role`, then chosen with `switchyard
set-role-runtime <project> router`. It runs as the project account, so no
credential moves between owners.

`disable` deactivates the role and removes `router_return`, keeping the role
declared so its decisions keep a name. The board refuses it while Router still
holds a ticket.

A document without a `router` role is unaffected, and the Director routes
directly as before.

## Misroutes and corrections

When an implementer hands Router's pick back with `implementer_kick_back`, that
decision is marked `misroute`, with the implementer's reason. When the Director
`reassign`s Router's pick in place, it is marked `director_correction`
(`note_router_outcome`). The ticket's `routing.decisions` shows both.

The shared skill and the board guide tell implementers to hand back a ticket
that belongs to another specialty or needs a Director decision, rather than
proceed because misroutes are rare. The ticket goes to the Director, never back
through Router, and the implementer chooses nothing about where it goes next.

## The canary

`scripts/router_canary.py` runs in three steps, each one Director command. None
of them waits or polls.

- **`seed`** creates the bounded fixtures (`examples/router_canary/fixtures.json`)
  in triage and routes them to Router. The expectations stay in a local run
  file, never on a ticket, so Router cannot read the answer.
- **`report`** judges each first decision against its expectation and prices
  the run:
  - every decision costs a Router turn;
  - every return or wrong assignment costs a Director turn;
  - every wrong assignment also costs an implementer interruption.

  It compares that with the Director routing everything by hand. A justified
  abstention costs one Director turn, which hand routing costs anyway, while a
  misroute costs all three.
- **`finish`** cancels the fixtures.

`tests/router_canary_test.py` runs these three commands against a disposable
board, with a keyword classifier standing in for the model, including one
deliberate misroute. The live run, with a real low-cost model in a tenant's
Router pane, is the same three commands and belongs to UAT.

## Observed, not changed

On the existing board, a Director `route` whose assignee does not own the
destination stage lands on that stage's first owner rather than being refused,
through the update trigger's owner fallback. So routing to `router` on a board
without an active Router lands on the first Implementation owner. This ticket
leaves that rule as it was. `tests/router_role_board_test.py` pins what happens.

## Options not taken

- **Granting `reassign`/`route` and filtering in the client.** The Director's
  note forbids it: a crafted payload would carry the Director's authority.
- **Router as an Implementation owner with `serial: false`.** The database
  reads ownership, not `serial`, to make a role an implementer
  (`ticket_is_implementer_assignee`), so Router would hold a slot and be
  remembered as the last implementer.
- **A ticket revision column.** A content digest needs no schema change to
  `tickets` or to its readers, and it changes exactly when what Router judged
  changes.
