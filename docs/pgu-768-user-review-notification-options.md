# PGU-768 User Review Notification Measurement

Measured 2026-08-29 against current `origin/main` and the live read-only board connection.

## Current State

The `user` role cannot receive any automatic notification today.

Evidence:

- `ticket_board.ticket_notification_queue.target_role` is constrained to pane roles only: `director`, `main`, `app`, `perf`, `ops`, `audit`, `inspector`, `research`. Live DB query found the same constraint and zero queued `target_role='user'` rows.
- `ticket_board.transition_target_role('user_review', 'user')` returns `NULL` in `schema.sql`, so `notify_ticket_state_transition()` does not enqueue transition notifications when tickets enter `user_review`.
- `ticket_board.notify_ticket_owner_in_place_change()` also uses `transition_target_role()`, so edits/comments on a `user_review` ticket do not target `user`.
- `scripts/ticket_board/notify_listener.py` excludes `user` from `ROLE_TO_TARGET`, returns `None` for `user_review`, and has no non-pane delivery path.
- `scripts/ticket_board/project_provision.py` removes `user` from generated notification roles, so provisioned tenants inherit the same non-delivery behavior.

This means adding `user` to the queue constraint alone would be misleading: the listener would have no tmux target and would drop or strand the row depending on where the change landed.

## Delivery Options

1. Board-only inbox/read badge.
   Add `user` to queue targets, expose pending user notifications through the board UI, and ack them when the user opens or explicitly dismisses them. This is the least surprising option if the user is a human at the browser rather than a pane.

2. Director-mediated delivery.
   Keep queue targets pane-only and enqueue `user_review` notifications to `director` with wording that asks the director to contact the user. This preserves current delivery machinery but codifies the manual relay instead of notifying the user directly.

3. User desktop notifier.
   Add a user-scoped notification consumer, such as a desktop notification or email/webhook bridge. This is the most direct user notification path, but it introduces a new privileged integration and needs Eric's choice of channel.

4. Explicit read-on-board-only state.
   Keep `user_review` as a board-visible bucket with no queued delivery, and document that user-facing review is discovered by board inspection. This matches current mechanics but should be chosen deliberately because it does not solve the missed-notification problem.

## Recommendation

Do not change the queue constraint in PGU-768 without choosing and implementing a consumer. The cleanest product direction is option 1: a board UI inbox/read badge for `user`, because it treats the user as a browser actor rather than inventing a pane. Option 2 is the smallest operational fallback if Eric wants a quick stopgap.

No existing pane-role notification behavior should change for this decision.
