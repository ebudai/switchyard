# SYRD-881: one hand-off per entry

## What happened

Found building SYRD-879 (2026-11-25), and reported by main and Audit there.
1. A ticket entered Audit with `audit`. Its "entered Audit" hand-off was queued, but `audit` was busy, so the listener
   deferred it.
2. `audit` kicked the ticket back without ever being handed it, acting on the ticket from the board. The ticket left
   Audit, and the undelivered notice stayed queued.
3. The rework came back to `audit` (SYRD-875's return), and that queued a new hand-off.
4. The old notice was current again. The listener's check (`_notification_is_current`) compares the ticket, the stage
   and the assignee, and all three matched. So `audit` was handed one entry twice.

The same happens within a stage: a hand-off deferred for `audit`, the ticket moved to `audit-2` and back, and `audit`
handed the entry twice. It isn't specific to reviews. A pass while the ticket was away would have dropped the old notice
as stale. The window is a ticket that leaves and comes back between two claims of that notice, which a deferral's
requeue delay makes ordinary. The same behaviour masked mutant F06 in SYRD-875.

## The rule

A hand-off belongs to the entry it was queued for. When a ticket leaves a stage, or changes owner within one, the
`tickets_z_retire_left_handoffs` trigger (`retire_left_handoffs`) retires the hand-offs still queued for that stage and
owner:
- **Which notices:** undelivered `transition` notices, not dead-lettered, whose payload names the stage and owner the
  ticket just left.
- **What happens to them:** they are deleted, and each is traced as a `drop` with reason `left_its_entry`, including
  where the ticket went.

**Why retire on leaving, and not refuse at send time by entry.** The alternative was to bind each notice to its entry
(`entered_current_state_at`) and refuse an older one at send. That needs the queue row's timestamp to be reliably
ordered against entry timestamps written by other triggers in the same transaction, which is fragile. Retiring on the
move decides at the one moment the entry ends, from facts in that update (`OLD` and `NEW`). It also covers notices
queued by older code.
- **Why it can't race a delivery:** a notice being delivered holds the ticket row from its last check through its send
  (`hold_for_delivery`, SYRD-631), so the move waits for that send.
- **Why it never touches the new hand-off:** the new entry's hand-off names the new stage or owner, so it is never one
  of the retired notices.
- **What it leaves alone:** a hand-off already typed into its pane, whose delivery proof is still open (SYRD-565:
  `unsubmitted`, or awaiting its receipt). That proof settles it, and tells the Director to clear text left in a
  composer; a quiet delete would skip that. The first sweep caught this as a timing-dependent failure of
  `delivery_proof_test`: under load the cancel reached the typed notice before it was dead-lettered.

**Unchanged:**
- a hand-off deferred for the current entry is still delivered once its reviewer frees up;
- SYRD-873's and SYRD-875's fresh-review hand-offs still arrive: they are queued for the entry they serve;
- a busy pane is never interrupted.

## Migration

`pgu1061_syrd881_handoff_per_entry.sql` adds the function and the trigger. `schema.sql` carries the same, and
`schema_compatibility` declares it compatible with older code: it deletes only notices older code would have found
stale, or delivered as a second hand-off.

## Tests

`tests/handoff_entry_test.py` runs on `tests/review_pane_rig.py`'s production-built board, with synthetic tickets.
- **Main, from a git archive:** `audit` is handed one entry twice, by `notification_trace` sends for that ticket,
  target and stage. This holds for both the re-entry (PGU-1) and the owner change (PGU-2).
- **This tree:** exactly one send for each case, also after `audit`'s turn ends while it still holds the ticket. The old
  notice is traced as `left_its_entry`.
- **On both trees:**
  - nothing reaches `audit` while it is busy;
  - the hand-off deferred for the current entry is delivered once it frees up.
- **The typed hand-off (PGU-3, on both trees):** a queued hand-off with an open `unsubmitted` proof survives its
  ticket's move and is not traced as retired.
- **Mutants:** three fail by an assertion: retiring nothing, retiring nothing on an owner change, and retiring a typed
  hand-off with an open proof. A fourth retires every hand-off for the old stage, whoever it names. It is equivalent:
  - every earlier hand-off was retired when its owner lost the ticket, so a pending one only ever names the current
    owner;
  - the new owner's hand-off is queued after the update on every path (reassign and balancer).

  The owner clause stays as the guard, should a path ever queue it inside the same update.

**Suites whose expectations moved** (each re-read, no claim weakened):
- **`held_notice_revalidation_test`** (SYRD-631): a hand-over a reassignment or cancel makes stale is now retired as
  that write commits, not dropped later. The check accepts either recorded end, and the notice is still never sent.
- **`inspector_handoff_delivery_test`:** "an arrival message left queued does not survive the ticket moving on" now
  records `left_its_entry`.
- **`ticket_board_unresolved_turn_after_kickback_test`** (SYRD-203):
  - The Director's "copy" it relied on was the seeded entry's stale "entered Director review", which is now retired.
  - It now asserts that retirement, then queues a live Director copy about the ticket where it is, so SYRD-203's
    guard is still tested against a queued Director row.
- **`user_events_board_test`:** its "exactly main's" comparison had recorded this defect. On main, PGU-905's route
  hand-off to `main` was delivered a second time after a kick-back.
  - It now compares main's delivery less that one duplicate.
  - The trace leaves out notices that ended in a drop (main's late `stale_notification`, this tree's
    `left_its_entry`); the queue and the sends are compared whole.

## SYRD-883: a copy claimed before the move

Audit's finding on SYRD-881: retirement removes the queued row, but the listener may already have claimed it. That
claim holds no lock, so a ticket could leave and come back to the same owner between the claim and the hold. The held
recheck found the ticket current, and the copy claimed into memory was sent beside the new entry's own hand-off. That
gives two hand-offs again, in the window from the claim to the hold, which includes a session clear.

**The rule:** under the hold, the claimed row must still be queued (`drop_if_stale`, phase `held_recheck`). Otherwise
the copy is dropped and traced as `retired_after_claim`.
- **Why it can't be stale:** the hold locks the ticket row, which every move must update. So a retirement either
  committed before the hold, and the row is gone, or it waits until after the send. This is the same lock SYRD-631's
  recheck decides under.
- **Why existence, not `claimed_at`:** Audit suggested also matching `claimed_at`. The claim doesn't carry it into
  memory, and queue ids are never reused, so existence of the claimed id is the same decision.

**Test** (`handoff_entry_test`, case 4, PGU-4): a seam on `hold_for_delivery`, never a sleep, kicks PGU-4 back and
resubmits it to `audit` between the claim and the hold.
- **Main before this fix (a5fc71b4):** two sends.
- **This tree:** one send, and `retired_after_claim` traced.

The suite now runs main twice: f7e7731a for SYRD-881's own cases, and a5fc71b4 for this one. Dropping the recheck,
or reading a retired row as queued, each fail by an assertion.
