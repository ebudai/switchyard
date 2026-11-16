# SYRD-795: on a pull board, work for an implementer who claims nothing queues in place

## What happened

MEFP, 2026-11-15 (filed by mefp; a successor of SYRD-772):
1. **MEFP-1213 was pinned somewhere nobody could take it from.** The Director routed it to ops while ops held
   MEFP-1209. The board diverted it into the pull policy's ready stage (`pull_ready_source_first`), assignee
   unassigned, `queued_for_assignee=ops`, `queued_behind_ticket=MEFP-1209`. That is the pull queue (SYRD-539): there,
   only the pinned implementer may claim it.
2. **Ops is not a claimant.** MEFP's claim transition names luna-1 to luna-9, and ops is an implementer the Director
   dispatches: not ephemeral, so not in `pull_claimant_pool`.
3. **The freed slot consumed nothing.** MEFP-1209 was blocked by MEFP-1214 and released ops's slot (SYRD-772), but
   nothing took MEFP-1213.
4. **The Director recovered it by hand.** It withdrew MEFP-1213 to Analysis and routed it to ops again.

## What changed

`enforce_declared_ticket_update` (migration `pgu1044`, `schema.sql` in place) diverts to the ready stage only when
the assignee is a claimant. Both of its diversions follow that rule: a move into implementation, and a reassignment
within it.

Any other implementer's ticket now lands in the implementation stage, assigned, and waits in its serial queue, as on
a Director-dispatched board (SYRD-568). When the slot frees, the ticket becomes the active one and its owner is told
once, with no queue marker left behind. That holds whether the slot frees because the previous ticket finished, was
blocked (SYRD-772) or moved on.

A claimant's work is pinned in Ready behind its active ticket, as before.

A ticket already pinned to a dispatched implementer before this change is not moved. The withdraw-and-route recovery
the Director used still applies to it.

## Tests

`tests/pull_dispatched_queue_test.py`. A production-built board with MEFP's shape of workflow: the shipped example
under a pull policy whose claimants are main and app, with ops dispatched by the Director. It runs the real server
and listener.
- B, routed to ops while ops holds A, waits in Implementation, assigned to ops, with no queue marker, and is not
  handed over.
- A is blocked by another ticket. B becomes ops's active ticket, is told once (and not again on another pass or a new
  listener), with `serial_queue.active` true and no stale marker.
- app's ticket, reassigned to a busy ops, also waits in ops's queue.
- A claimant (main) still has its second ticket pinned in Ready behind its first.

Three mutants, all killed: each diversion's claimant condition dropped, and no diversion at all.
