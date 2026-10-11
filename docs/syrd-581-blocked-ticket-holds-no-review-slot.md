# SYRD-581: a blocked ticket held Audit's serial lane

## What MEFP showed

MEFP runs release f92a3d4d with workflow revision 115. On 2026-10-11 at about
04:36 (Toronto), Audit signed off MEFP-592's resubmission. That resubmission
was a no-code deferral that followed an Audit kick-back, and 592 left Audit.
MEFP-593 stayed pending:
- `finish current`, attempt 820;
- still pending at 04:40:05, after its recorded retry time;
- 594 and 595 waiting behind it;
- Audit live and idle.

SYRD-542's idle release (pgu975) did not clear it.

## What held it

This was read from MEFP's board through its HTTP API only, at about 11:40
(Toronto) the same day. Nothing was written, and no pane was touched.

- **Nine tickets in Audit, assigned to Audit.** Eight had a pending notice
  held as `finish current`: 593, 594, 596, 599, 609, 610, 611 and 612.
- **MEFP-529 was the ninth, with nothing queued:**
  - it was resubmitted to Audit on 2026-10-07 at 23:53:06;
  - at 23:54:03 it was blocked on MEFP-665, which is still in progress;
  - every other Audit ticket entered Audit after it.

Serial review admission, `finish_current_stage_blocker`, takes a review role's
current ticket to be the earliest arrival in the stage. The listener drops a
blocked ticket's transition notice (`_notification_is_current`), so 529 was
never handed over again. The admission check still counted it as Audit's
current ticket. So 529 held every later Audit ticket for good.

This follows by elimination. A held notice waits on the earliest other
arrival. The earliest arrival overall waits on nothing, unless its notice is
dropped. 529 is the only Audit ticket with no notice, and it is blocked.

SYRD-542's release asks the same check whether a hold still applies, so it
found 529 each time and released nothing. 592's kick-back and resubmission
were not the cause. 592 was held behind 529 too, and Audit reviewed it
anyway: a review is a board write, and needs no delivery.

## The change

Migration pgu986 replaces `finish_current_stage_blocker`, and schema.sql
carries the same text. Its review-stage branch, which is also the legacy
implementation path, changes in two ways. The declared implementation branch
(SYRD-568) is unchanged.

1. **A blocked ticket holds no review slot.** A ticket with an unresolved
   blocker is not a candidate. SYRD-568 already applies this rule to an
   implementer's slot. The listener never delivers a blocked ticket, so it is
   nobody's current work.
2. **Work under way keeps the slot.** Rule 1 lets a later arrival be handed
   over while an earlier one is blocked. When the earlier one unblocks, arrival
   order alone would hand it over too, which would give Audit two tickets at
   once. So the current ticket is the one handed over most recently in the
   stage. "Handed over" means a transition notice to the role was sent since
   the ticket entered the stage. That is read from the notification trace's
   `send` record, which is never pruned and has its own index
   (`notification_trace_send_lookup_idx`). It is not read from
   `last_transition_notified_at`: the listener acknowledges a notice it drops
   as stale, and that acknowledgement stamps the column too, so a blocked
   ticket would look handed over. When nothing has been handed over, arrival
   order decides, exactly as before.

   In these terms, a candidate holds the asking ticket when either:
   - the candidate was handed over and the asking one was not; or
   - both were handed over, and the candidate more recently; or
   - neither was handed over, and the candidate arrived first.

   That is a strict order, so no two tickets can ever wait on each other.

**Unchanged:**
- **The rest of the check:** manual control, and the exclusion of a candidate
  whose due notice another transaction is claiming.
- **The listener:** its activity gate, composer and background holds, live
  identity checks, attempt history and backoff. No Python changed.
- **The release:** `reset_notification_backoff_for_idle_roles` (pgu975) is
  not touched. It calls this function, so on an upgraded board, Audit's next
  idle pass releases rows that are stuck now.
- **The function's signature and grants:** `CREATE OR REPLACE` keeps both.

## After deployment on MEFP

Nothing on MEFP is changed by this ticket. Once the release with pgu986 is
installed, MEFP-529 stops holding the lane. Audit's next pass with trusted idle
evidence releases the earliest of the waiting tickets, and the rest follow one
at a time in arrival order. When MEFP-665 resolves, 529's unblock notice waits
for whatever Audit has at that moment.

## Tests

`tests/blocked_review_slot_test.py`, 25 checks. It runs on a production-built
board:
- companion roles, `schema.sql`, `rbac.sql`, the real migrate runner, then
  `rbac.sql` again;
- a declared workflow shaped like MEFP's, where implementers submit straight
  to a serial Audit;
- the real notify listener, driven one loop iteration at a time, with idle
  evidence under the scenario's control.

The sequence:
1. K (529) is submitted, kicked back, resubmitted, then blocked in Audit.
2. A (592) arrives.
3. B (593) and C (594) arrive and are held eight times, to the 300-second cap.
4. Audit kicks A back. A is resubmitted no-code, Audit approves it, and the
   Director defers it to the backlog, unassigned.
5. Audit goes idle.

**Main 0b3e2c6 and the deployed f92a3d4**, each run from a git archive in a
child process:
- K has nothing queued, and A, B and C are held behind it;
- after A leaves and Audit is idle, B stays held at the cap.

That reproduces the report.

**f92a3d4 upgraded in place** with this tree's migrations and `rbac.sql`:
- Audit's next idle pass releases B with its attempt count kept;
- a restarted listener delivers B once;
- C stays behind B;
- the function, owner and grants equal a fresh install's.

**This tree:**
- **K, blocked, holds nothing.** A is handed over on arrival, and B and C are
  held behind A. A's resubmission waits behind B, which arrived first.
- **The release.** A leaves and Audit is idle. The idle pass releases B, keeps
  its attempts, and B is delivered once. C keeps its hold and deadline.
- **K unblocks while Audit has B.** K waits, even through Audit's idle passes.
  When B leaves, K goes before C, which arrived after it.
- **A busy pane** (someone typing) still holds C as `pane busy`. When it
  clears, C is delivered.
- **No mutual wait.** P arrives first but is blocked, so Q is handed over.
  P's notice is dropped afterwards, which is not a handover. P unblocks and
  waits behind Q. Q is blocked and unblocked while P still waits. Q, which
  Audit already had, goes first, and P follows.
- **Exactly once.** Each handover is delivered once; that is K's submission,
  resubmission and unblock, and Q's submission and unblock.
- **Other roles.** Another role's waiting notice and its deadline are
  untouched.
- **Parity.** `schema.sql` and the migrations install the same function, owner
  and grants. The listener can execute it, and the service role cannot.

## Evidence

- **`tests/blocked_review_slot_test.py`:** 25 checks pass on this tree. Its
  child runs of main 0b3e2c6 and of f92a3d4 reproduce the stall, and the
  upgraded f92a3d4 board releases it.
- **Mutation:** seven mutants, run through `tests/bounded_run.py mutate`; all
  seven were killed. Each kill was read on its failing assertion. The parity
  checks run last, so a behaviour fault is reported by the behaviour it breaks.
  - **"A blocked ticket holds the slot":** caught by the f92a3d4 upgrade
    check, which finds B still held.
  - **"A handover holds nothing":** caught when B and C are not held behind A.
  - **"The earliest handover is current":** caught when K is delivered while
    Audit has B.
  - **"Handed work waits behind earlier arrivals":** Q and P wait on each
    other, and Q's unblock is never delivered. Caught by the delivery count.
  - **"Arrival order is reversed":** caught by the upgrade check, where C goes
    before B.
  - **"A dropped notice counts as a handover":** P is delivered while Audit
    has Q. Caught by the delivery count.
  - **"schema.sql keeps blocked tickets":** caught by the parity check.

  Two edits were not mutated, because nothing can tell them apart:
  - the tie-break between equal handover times;
  - the order the blocker is reported in, which only names a ticket in the
    log.
- **The sweep:** 106 related suites were run serially on this tree and on main
  0b3e2c6, with provider stubs that refuse to start.
  - Both trees have the same 15 red suites.
  - Those 15, and the five serial-admission suites, were rerun after the last
    SQL change. Their full output, with temporary paths, times and ports
    normalised, is identical on both trees, apart from this ticket's migration
    named in two lists of applied migrations.
  - The serial-admission suites are green on both trees:
    - `serial_handoff_latency_test`;
    - `ticket_board_review_admission_turn_attribution_test`;
    - `assigned_implementation_queue_test`;
    - `user_review_serial_release_test`;
    - `worker_pool_rehearsal_postgres_test`.
- **MEFP:** read through its HTTP API only. Nothing on MEFP was written or
  woken.
