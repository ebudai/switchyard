# Completed reviews in deliberately held stages

A legacy approval made while `manually_controlled=true` records the real
reviewer's signoff, retains the review state and assignee, and persists an
awaiting-role handoff after the operation's final ticket touch. The reviewer
does not need to post another comment or explicitly set a wait.

| Completed decision | Next decision stage |
| --- | --- |
| Inspection | Audit when required, otherwise DAT when user approval is required, otherwise Director Review |
| Audit | DAT when user approval is required, otherwise Director Review |
| User Review | Director Review |

The recipient is resolved through the existing stage-owner and notification
routing functions. Director is the fallback. This does not move a held ticket
past any remaining gate or waive another review. Releasing the deliberate hold
retains the existing automatic advancement behavior.

The private helper is callable only from the existing authorized database
decision operations. Those operations lock the ticket and recognize the new
approval before changing it. A repeated held approval returns without comments
or touches after authorization and gate validation; it cannot recreate a
resolved wait, overwrite a retargeted wait, or cancel a same-owner handoff.

SYRD-15 provides the persisted generation, immediate/5-minute/15-minute
notifications, 30-minute Director escalation, busy/failure retries, and
four-hour expiry. Delivery acknowledgement does not resolve the wait. Existing
clear/retarget/stage-change handling suppresses obsolete generations. Legacy
User Review is added to the shared wait-stage predicate and listener fallback;
the existing actor permissions for explicit wait operations remain intact.

Configured workflows continue using the SYRD-11 executor, its declared routing
and its existing held-approval behavior. The legacy helper exits when a declared
workflow exists. This change therefore depends on the SYRD-11 foundation and
does not duplicate its registry or executor.

## Installation and verification

Fresh schema and `pgu922_syrd22_held_review_handoff.sql` contain identical bodies
for all five changed/new functions. Apply the migration after the SYRD-11
`pgu921` migration through the normal reviewed release migration process, and
deploy the matching listener. Reapplying the migration changes no ticket or
wait generation. It does not replay approvals completed before installation;
previously stranded tickets remain under the Director's existing explicit
routing process. Rolling back the application does not erase persisted waits,
but an older listener cannot deliver User Review waits; use the matching
reviewed listener until those waits are resolved.

`tests/ticket_board_held_review_handoff_test.py` exercises actual HTTP actions,
service-role SQL and disposable PostgreSQL for all three review paths and gate
combinations. It verifies unauthorized approval rejection, private-helper
access denial, state retention, durable schedule, repeated migration/approval,
busy/failure retries, real shipped-directorctl receipt in isolated tmux,
acknowledgement without resolution, clear/retarget/stale delivery suppression,
custom legacy owner routing and unchanged unheld advancement. `--upgrade`
starts from the exact pre-change SYRD-11 schema. The configured workflow suite
checks that the shared foundation still emits its own held-review handoff.

No live SQL, service, pane or release changes are part of source implementation.
Independent Audit and Director-controlled release deployment remain required.
