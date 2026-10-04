# SYRD-542: a reviewer's next ticket waited out a deadline nothing was holding it to

## What happened (MEFP, 2026-10-03)

Serial review admission held MEFP-570's Audit notice behind MEFP-569, which
Audit was reviewing. Each hold requeued the notice as `finish current`, with
the listener's ordinary doubling backoff:
- 5, 10, 20, … seconds;
- capped at 300.

By the eighth attempt its next try was about five minutes away. Audit then
finished 569, which went on to Ops, and Audit's pane went idle. 570 was still
not delivered until its accumulated deadline came round, about five minutes of
an idle reviewer with work queued. The rows behind 570 (571..580) were
correctly held behind it.

## Cause

On every pass with trusted idle evidence for some roles, the listener calls
`ticket_board.reset_notification_backoff_for_idle_roles`. That function (newest
copy pgu776) re-armed a role's waiting rows only when their last error was
`'pane busy'`. A row held as `'finish current'` kept its long deadline after the
ticket holding it left the reviewer.

## Fix (migration pgu975, schema.sql identical)

The same function also re-arms a held **transition** notice whose last error is
`'finish current'`, but only when the admission check that held it,
`finish_current_stage_blocker`, now finds no ticket holding it. Everything else
is unchanged:
- **Behind a current ticket:** a row still behind one, even one selected but
  not yet delivered, keeps its hold and its deadline;
- **Evidence:** release needs the role's own idle evidence, the listener's
  existing idle map, which already leaves out roles with background work. No
  role releases another role's rows;
- **Delivery checks:** the released row is claimed on that pass and goes
  through every delivery check as before: live identity, the activity gate (a
  busy pane or a human typing still holds it, now as `pane busy`), session
  clear, the turn-start witness, one delivery per assignment;
- **Backoff:** the attempt count is kept, and no retry delay is shortened for
  anything else;
- **Signature:** the function keeps its signature, so the listener's existing
  grant stands. No Python changed.

## Evidence

`tests/serial_handoff_latency_test.py`, 11 checks. It runs on a
production-built board (companion roles, `schema.sql`, the real migrate
runner, `rbac.sql`) with a declared workflow in which Audit is serial, and the
real notify listener, driven one loop iteration at a time in `listen_once`'s
order. The listener's activity gate and idle evidence are under the scenario's
control.

**Main, e698056, run in a child from a git archive:** B is held eight times
behind A, and its deadline doubles to the cap. A leaves Audit and Audit's idle
evidence arrives, but B is not delivered and still waits about 300 seconds.
That reproduces the report.

**This tree:**
- the same holds accumulate;
- Audit's idle pass releases B's hold and deadline, keeping its attempt count;
- a restarted listener delivers B on that pass;
- C, behind B and not yet delivered, keeps its hold and deadline across later
  passes;
- each assignment is delivered exactly once;
- with only another role's idle evidence, Audit's rows are not released;
- with idle evidence but a live-busy pane, C is held as `pane busy`, then
  delivered when the pane clears;
- another role's waiting notice and its deadline are untouched;
- a fresh `schema.sql` board installs the same function as the migrated one.

**Mutation (`tests/bounded_run.py mutate`, each kill read on its assertion
line):** four mutants, all killed:
- finished holds not released;
- every hold released, without the blocker check;
- release for any role;
- the stage read from the wrong payload key.

Dropping `q.kind = 'transition'` was not mutated: only transition notices are
ever held as `finish current` (`_serial_gate_stage` returns nothing for any
other kind), so it is equivalent by construction.

## No live dependency

The fix is reproduced deterministically on a disposable board. Nothing was read
from MEFP's homes or changed on any tenant.
