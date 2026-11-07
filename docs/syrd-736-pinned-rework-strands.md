# SYRD-736: rework pinned to a worker that cannot take it is reported

Origin: MEFP, 2026-11-06. Two tickets sat in the pull-ready stage for nine hours, each waiting for one worker that
was never going to take it, while eight other workers sat idle. Nobody was told.

## What happened

The times below are from MEFP's board (`/api/board`).

| Time (UTC) | MEFP-768 | MEFP-769 |
|---|---|---|
| 13:18–13:22 | Audit kicks it back to luna-9, who holds MEFP-760. The board puts it in `pull_ready_source_first`, pinned to luna-9 (`queued_for_assignee`), behind MEFP-760 (`queued_behind_ticket`). | The same for luna-10, behind MEFP-761. |
| 13:42–13:44 | The Director reassigns MEFP-760 from luna-9 to luna-2. luna-9 now holds nothing. | The Director reassigns MEFP-761 from luna-10 to luna-3. |
| … | luna-9 is stopped. | luna-10 is stopped. |
| 23:12 | The Director finds it and moves it to luna-2 by hand. | The Director moves it to luna-3. |

When the Director looked, `worker-pool mefp status` reported 15 ready workers: 8 running, 8 able to take a ticket, none holding one.

## Why nothing happened

Three rules met, and none of them was wrong alone:

- **Pinned rework waits for its author** (SYRD-539). `ticket_ready_for_pull` lets only the author claim it, so the
  eight idle workers could not.
- **The listener claims only for a running worker** (SYRD-573), and luna-9 was not running.
- **Neither of the Director's notices could fire:**
  - The idle notice (`notify_pull_idle_capacity`) names running workers that could claim the ticket. The only worker
    that could claim it was not running, so there was no one to name.
  - The "can be routed now" wake (`notify_serial_focus_queue_wakeups`) is switched off under a pull policy, because
    the author's own claim was supposed to do that job.

The queue comment on each ticket said "Route it again once MEFP-760 leaves that implementer". When it left, nothing
said so.

## The rule

On every pass, the listener's pull pickup already judges each claimant. It now also says, for each claimant that is
not idle capacity, why not:
- "it is not running (no registered provider process)";
- "it is not running (registered provider process is gone)";
- "its provider is held for quota".

It hands that to `ticket_board.notify_pull_pinned_rework_strands(p_idle, p_unavailable, p_now)`, next to what it
already gives the idle notice.

**When rework is stranded.** A ticket in the ready stage is stranded when all of these hold:
- it is pinned to an author;
- its author could claim it now (`ticket_ready_for_pull`), so blocked, held, parked or awaited work is not stranded;
- its author holds no ticket, so the wait it was pinned for is over;
- its author is not idle capacity: the listener could not give it the ticket.

**The record.** `ticket_board.pull_pinned_rework_strands` has one row per stranded ticket:
- the author;
- when the stranding was first seen;
- the author's reason and the idle claimants, both rewritten on every pass.

The row is deleted as soon as the ticket stops being stranded. That happens when:
- someone claims it;
- it is withdrawn or blocked;
- its author runs again, or takes other work;
- its pin moves to another author. A move to another author starts a new record, with its own clock and notice.

**The notice.** It is sent once per stranding, to the Director, when both of these hold:
- the stranding has lasted the policy's `idle_alert_seconds` (900 on MEFP). That gives the project's admission time
  to start the author (SYRD-688), which is the usual fix;
- at least one claimant is idle with nothing to claim.

A worker that is merely running is not counted as idle. For MEFP-768 it would have read as follows, where `<time>` is the first pass that saw luna-9 both free and stopped:

> MEFP-768 is rework pinned to luna-9, and luna-9 has not been able to take it since <time>: luna-9
> holds no ticket now (it was queued behind MEFP-760), but it is not running (no registered provider process).
> These implementers are idle and cannot claim it while it is pinned: luna-2 (idle, nothing claimable), …. Start
> luna-9, or move MEFP-768 out of pull_ready_source_first and route it to one of them.

The notice carries the ticket's pin (`queued_for`, `reserved_by`), so the listener drops it undelivered if the pin
moves before it is sent (SYRD-108). It also carries the state and assignee, so it is dropped once the ticket is
claimed. Its kind, `pull_pinned_rework`, is addressed only to the Director.

**What it does not do.** It moves nothing. The ticket asked for either a reassignment or a notice. Rework is pinned
so that its author continues the saved conversation, and MEFP's onboarding prompt tells its authors to. Whether
starting the author is worth more than giving the ticket to a fresh worker is the Director's call. The record keeps
the evidence for that call: who was idle while the ticket waited, and why its author was not.

## Migration

`pgu1029_syrd736_pinned_rework_strands.sql` adds:
- the table;
- the function, with `REVOKE EXECUTE … FROM PUBLIC`.

`rbac.sql` grants the function to `ticket_board_listener`, and `schema.sql` carries both. The migration moves no ticket
and sends nothing. It does nothing on a board without a pull policy, which has no ready stage.

## What MEFP gets

Once deployed, the listener's first pass records every ticket that is stranded then. Any that are still stranded 15
minutes later, with a worker idle, are reported once each.

## Tests

`tests/pinned_rework_strand_test.py` builds MEFP's sequence on a real board, with the real listener and its real
pull pickup. It uses the shipped example workflow with pull scheduling: app is the author, main is the idle claimant,
and ops takes the predecessor.

Main runs first, from a git archive. There, the rework stays pinned to app with main idle well past the interval,
and the Director is told nothing.

This tree then runs on three boards:
- one built the way production builds them, where the migration's copy of the function runs;
- one built from `schema.sql` and `rbac.sql` alone, which runs `schema.sql`'s copy;
- main's board, upgraded by this tree's migrations.

On each board it checks these cases:

| Case | Expected |
|---|---|
| The author still holds the predecessor | Not stranded, however long it waits. |
| The predecessor reassigned away and the author stopped | Recorded at once, with the author's reason and main idle. Nothing is said within the interval. |
| Blocked | Not stranded. Unblocked, it is stranded again, counted from then. |
| Past the interval with nobody idle | Nothing is said yet. |
| Past the interval with main idle | One notice, with each part of the text checked. Later passes add nothing. |
| The listener's view of the author changes | The record's reason follows it: no registration, a dead registered process, or a quota hold. |
| The author running but busy | Not stranded. The existing idle notice still names the author, as it did before. |
| The pin moves to another author | A new stranding with its own clock and notice. Unpinned work is never stranded. A notice whose pin moves before delivery is dropped. |
| The author running again | It claims its rework, the record goes, and nobody else is told anything. |

## Mutation

25 mutants, all killed by a candidate check, never by a crash.

**19 change the SQL:**
- each condition of the stranded test;
- the record's identity and its delete;
- what counts as idle;
- the update on conflict;
- the clock and the interval;
- once per stranding;
- the payload's pin;
- the fallback reason and the text about what the ticket was queued behind;
- a single copy at a time: the migration's copy kills on the production-built board, and `schema.sql`'s copy on the
  `schema.sql`-only board.

**6 change the listener:**
- the reasons;
- the call itself;
- the eligibility kind;
- the grant;
- the idle map it passes.

Three guards were removed as no-ops, from what can reach them:
- the function's early return without a policy: with no ready stage, nothing matches and the loop is empty;
- an idle claimant's pool and reservation checks: the listener names only pool members, and "idle, nothing claimable" only after `claim_ready_ticket` found the worker holding nothing;
- the ready-stage time in the record's identity: a ticket that leaves the ready stage stops being stranded, and its record goes on the next pass.
