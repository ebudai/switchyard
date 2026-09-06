# Durable awaiting-role handoffs (SYRD-15)

A new `set_awaiting_role` call now commits its wait and four existing-queue
notifications in one transaction. PostgreSQL NOTIFY wakes the listener; the
persisted queue remains authoritative if that wake is missed. No scheduler,
new transport, or in-memory retry state is introduced.

## Delivery and resolution policy

| Step | Due after original wait | Recipient | Delivery window |
| --- | --- | --- | --- |
| 1 | Immediately | Awaited role | 0–5 minutes |
| 2 | 5 minutes | Awaited role | 5–15 minutes |
| 3 | 15 minutes | Awaited role | 15–30 minutes |
| 4 | 30 minutes | Director | 30 minutes–4 hours |

Each window uses existing claim leases, activity gating, delivery retry/backoff
(default 5 seconds doubling to 300 seconds), missing-pane dead letters, and
tracing. Busy-pane retries retain their existing policy. These are at most four
successful sends per wait generation; transport failures may retry within the
window. The final escalation also goes to director when director was originally
awaited. Missing-pane dead letters do not consume the later scheduled steps.

The listener rejects expired steps, so a long outage produces only the current
step rather than a burst of overdue reminders. At four hours, the final step
expires and the existing owner-nudge suppression expires. The wait stays visible
until explicitly resolved; expiry and delivery ACK do not falsely mark it done.
Manual-control/parked/blocked owner policies still govern subsequent owner nudges.
A total transport outage cannot guarantee human receipt; pending/dead-letter
queue rows and notification traces remain operational evidence.

Repeated requests for the same role retain the original clock and schedule,
including after successful ACKs. Clear then set starts a new request; retargeting
also starts a new generation. A persistent notification-generation marker
prevents migration reapplication or repeated requests from recreating ACKed rows.

Before delivery (and again after pane activity probing), the listener checks the
wait's role and timestamp, ticket state and assignee, and window expiry. Clearing,
retargeting, state/assignee changes, terminal transitions, and existing awaited-role
non-comment activity make old rows stale. Stale rows are ACKed without sending when
due; future rows need not be deleted immediately. Comments and ACKs do not resolve
waits (PGU-909/915). Existing PGU-912 reentry and PGU-916 owner-idle rules are unchanged.
Delivery is at-least-once: a process crash after sending but before ACK can repeat a
message, as with the existing transport. A resolution concurrent with the actual
external send has the same unavoidable narrow race as other queue deliveries.

## Schema and migration

`pgu920_syrd15_durable_awaiting_role.sql` follows the existing migration runner's
accepted filename grammar and sorts after PGU-916. It adds one nullable internal
marker column, extends the queue kind constraint, and replaces awaiting-role
setter/clear functions. The scheduling helper is private (PUBLIC execution
revoked); existing actor checks and RBAC entry points remain in force. Ticket
and notification-state locks use the same order as ticket activity triggers.

Migration backfills existing waits younger than four hours without extending
their original clock. Old expired steps will be discarded by the listener.
Fresh provisioning has matching function definitions and schema. Migration
reapplication is safe even after ACK deleted queue rows.

## Verification and deployment handoff

Run `python3 tests/ticket_board_durable_awaiting_role_test.py` in the source
checkout with PostgreSQL, psycopg and tmux installed. It creates disposable
PostgreSQL databases and isolated tmux servers. It exercises production RBAC,
queue/claim/retry/ACK, the production listener, installed directorctl and actual
idle-pane receipt. It tests fresh schema and upgrade from reviewed 8651f78,
concurrent deduplication, failed delivery and process replacement, abandoned-claim
recovery, comments/ACKs, retarget/edit/clear/state/assignee/terminal cancellation,
pre-send resolution, expiry/escalation, rollback and migration reapplication.

Director owns review import, normal audit submission, publication and deployment.
Use the existing tenant deployment configuration; retain its explicit source
verifier override. No seed repository, launcher, live pane or service changes are
part of this source branch.

Coordinate the listener and database upgrade: stop the old tenant notification
listener, deploy the audited release and apply its migration with
`scripts/ticket-board-migrate` using the existing privileged database environment,
then start the listener from that same release. Do not leave an old listener
running against new awaiting-role queue rows: its owner routing check can discard
them. Existing `ticket-board-service.sh` and
`ticket-board-notify-listener-service.sh` deploy paths apply migrations. Select the
actual system/user service scope from the tenant configuration; do not assume a
new unit or use script defaults intended for another tenant.

Post-deploy, verify the selected release hash for both processes and the recorded
migration name; create one real active handoff via the normal write API. Confirm
four queue/trace enqueues, the awaited role's actual pane receipt, and that ACK
leaves the wait visible. Clear via API and confirm no later send from that wait.
If testing the timed escalation, keep the wait unresolved and observe the 5/15/30
minute steps. Do not mutate production timestamps to accelerate acceptance.

For rollback, stop the listener before restoring the prior board/listener release.
Retain the additive column and accepted queue kind; remove/administratively dismiss
pending `awaiting_role` rows and restore the previous setter/clear function bodies
from the prior reviewed release transactionally under director/operator control.
Restoring the old behavior reintroduces the missed-wakeup bug; arrange an explicit
human handoff for unresolved waits. Do not merely downgrade the listener while
leaving the new setter active. Preserve traces and take the normal database backup
before either deployment or rollback.
