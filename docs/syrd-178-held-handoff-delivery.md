# Delivering a handoff out of a held ticket

A transition notification is not a reminder. It exists only because somebody
other than the recipient moved a ticket into a stage that role owns, and both
of those facts are settled when the row is written: `enqueue_transition_notification`
refuses to enqueue when the caller is the target, and for a ticket under manual
control `notify_ticket_state_transition` enqueues only when the move changes
which role owns the work (SYRD-107).

The listener then threw those rows away. `_notification_is_current` treated any
`transition` for a ticket carrying `manually_controlled` as stale, with a single
carve-out for the Director's final review handoff, so a held ticket's handoff
was dropped on its first claim and on every retry after it -- acknowledged,
removed from the queue, and recorded as `drop / stale_notification`.

Live on SYRD-146. It was submitted to inspection commit-exempt after its rollout
completed; the Board moved it, assigned Inspector and enqueued the handoff. The
Director then took hold of the rollout to steer the remaining work by hand,
which is what that flag is for. Nothing was ever delivered: `active_work_notified_at`
stayed empty -- the board reads it from a durable `send` row -- the listener
kept running with its original PID, and the ticket sat in inspection until the
User noticed it.

So manual control no longer voids a transition. Everything else that made the
notification trustworthy still runs on every claim: the ticket must still be in
the state the payload names, with the assignee it names, and the role addressed
must still be the one that stage routes to. Two flags still stop delivery, and
neither is a silence: a ticket with an unresolved blocker cannot be acted on
(SYRD-148), and `parked` belongs to a ticket held in backlog behind another.
Reminders -- `nudge`, `idle_reminder`, `triage` -- are silenced by manual
control exactly as before, because those are the messages it exists to stop.

The fix is in the listener alone. There is no schema change and no migration:
the delivery decision was never in the database, which is why SYRD-107's
migration could not have carried it.

## Verification

`tests/inspector_handoff_delivery_test.py` drives both board kinds -- the legacy
tables and a declared workflow, since syrd's board carries one -- against a real
Postgres cluster and the real listener. It reproduces the live shape, proves the
handoff is delivered once to an idle Inspector and deferred rather than dropped
for a busy one, that the deferral is released by the pane reporting idle, that
sign-off hands on to Audit exactly once, that an ordinary committed submission
is unchanged, and that a stale stage announcement, a reminder on a held ticket
and a blocked ticket's handoff each stay suppressed with the reason recorded in
`notification_trace`.
