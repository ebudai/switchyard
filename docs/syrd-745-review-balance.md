# SYRD-745: waiting review work goes to an idle second reviewer

Origin: MEFP, 2026-11-07.

## What happened

MEFP's Audit stage is owned by `audit` and `audit-2`. `audit-2` is an ephemeral reviewer templated on `audit`. Around
11:25 America/Toronto, both were live and connected:
- `audit` held four tickets. MEFP-932 was delivered and active. MEFP-942, -953 and -956 were pending as "finish
  current".
- `audit-2` held nothing.

The Director moved MEFP-956 to `audit-2` by hand. At that instant its delivery read `pending`, with 0 attempts.

## Why

**Every submission goes to the first owner.** A transition into a stage names an owner only if it says so.
Otherwise the declared update (`enforce_declared_ticket_update`) gives the ticket the stage's first owner:
`owners[1]`. So every submission into Audit went to `audit`.

**A reviewer works one ticket at a time.** It takes the one last handed to it, or else the earliest arrival
(`finish_current_stage_blocker`). The rest wait as "finish current". Nothing ever gave waiting work to `audit-2`.

**The reassignment was delivered.** MEFP's board shows the Director's reassignment at 16:25:09 UTC and `audit-2`'s
independent sign-off at 16:27:46, 2 min 37 s later. "Pending, 0 attempts" was a look at the instant the notice was
queued. Reproduced on a board, the same reassignment to an idle `audit-2` is delivered on the next listener pass,
on main as on this tree. So the second expectation, prompt delivery of a direct reassignment, already holds. It is
kept as a regression check.

## The rule

On every listener pass, `review_balance.run` looks at each declared review stage with more than one owner. Each
owner that the listener can give work to now is offered the stage's oldest waiting review. "Can give work to"
means `pull_pickup.why_not_eligible` finds:
- a registered provider process that is still live;
- hook state idle;
- nothing running.

`ticket_board.balance_review_stage(stage, owner, now)` decides, under the board's own rules. It runs only for the
listener.

**The receiving owner must:**
- own the stage, which more than one role owns;
- hold no ticket in it, so an idle owner that is still holding a review is not given a second one;
- not be provider-held (`role_provider_hold_is_current`, which includes SYRD-672's account holds).

**The ticket must be waiting:** another owner holds it, behind that owner's current ticket.

**Never moved:**
- a reviewer's current ticket, so an active review is never taken;
- a manually controlled ticket, which `finish_current_stage_blocker` never counts as waiting;
- a blocked or awaited ticket;
- a re-review its reviewer already began, meaning it was handed to that reviewer in this stage before. A returned
  ticket's re-audit stays with the auditor who knows it.

**What a move does:**
- It changes only the owner, inside a one-ticket window that `enforce_declared_ticket_update` admits beside Router's.
  The stage-owner check and the flags still apply.
- It is recorded on the ticket: "Board balance in audit: audit -> audit-2. audit is reviewing MEFP-932, and audit-2
  was live and idle with nothing in audit, so this waiting review goes to it."
- The new owner is handed it as a reassignment is. The notice still queued for the old owner no longer names the
  assignee, so it is dropped.

**What it does not change:**
- Where a submission first lands: still the first owner.
- A busy, held or stopped second reviewer is never given anything.
- A board with one owner per review stage runs nothing new.

## Migration

`pgu1033_syrd745_review_balance.sql` redefines `enforce_declared_ticket_update`. It is pgu1022's copy plus the
window. It also adds `balance_review_stage`, with `REVOKE EXECUTE … FROM PUBLIC`.
- `rbac.sql` grants the function to `ticket_board_listener`.
- `schema.sql` carries both.
- The migration moves nothing.

The number is pgu1033: SYRD-744 holds pgu1031 and SYRD-746 pgu1032.

## Tests

`tests/review_balance_test.py` builds the example workflow with `audit-2` added the way MEFP has it, on a
production-built board:
- the real HTTP server and notify listener;
- the real PaneActivityGate, reading real hook-state files;
- every role registered with a live stand-in provider process (SYRD-644's rig);
- panes that are a recording sender. A reviewer handed a ticket stays busy reviewing it until the test ends its turn.

**Main runs first, from a git archive.** All three submissions stay with `audit`, and `audit-2` is given nothing. A
direct reassignment to idle `audit-2` is delivered on the next pass.

**On this tree:**

| Case | Expected |
|---|---|
| Three submissions | The oldest waiting one goes to idle `audit-2` and is delivered, and the move is recorded. `audit` is handed only its current review. |
| `audit-2` idle but still holding its review | It is given no second review. |
| `audit-2` signed off and idle again | The next waiting review goes to it, and `audit` hears nothing. |
| Blocked, manually controlled, awaited | Never moved. |
| `audit-2` busy, provider-held, or its process gone | Nothing moves to it. |
| A re-review `audit` already began | It stays with `audit`, while another waiting review goes to `audit-2`. `audit`'s current review is never taken. |
| A direct reassignment to idle `audit-2` | Delivered on the next pass. |
| Both functions in `schema.sql` | They are pgu1033's copies. |

## Mutation

13 mutants. Each is killed by a behaviour check, except the one that changes `schema.sql`'s copy only, which the
parity check kills as intended. The mutants:
- **The board's rules:** the waiting rule; the re-review rule; the blocker and await clauses; the holds-nothing
  check; the provider-hold check.
- **The move itself:** the trigger window; the hand-over notice; the recorded comment; the oldest-first order.
- **The listener:** its eligibility check; the pass's call.

Two clauses were removed because nothing can reach them:
- a manually controlled ticket is never waiting;
- `parked` belongs to backlog.
