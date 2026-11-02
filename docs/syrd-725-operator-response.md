# SYRD-725: the User's answer to an operator wait reaches the Director

A ticket blocked by `operator:<name>` is waiting on a person outside the board, and only the Director (whoever
holds `set_blockers`) can release that blocker. On SYRD-691:
- Ops asked the Director for Eric's approval, and the Director recorded `operator:eric`.
- Eric answered on the ticket as the User: "approved, and now is a good time".
- `add_comment` told Ops, the owner, who cannot release the blocker.
- The Director heard nothing, and Ops had no way to tell it: `request-dependency` is refused while an external
  blocker stands, and a plain comment from a role notifies nobody.
- The ticket stalled until Eric noticed.

On main `172b5b17` the same steps give the same result: the User's comment wakes Ops and never the Director. A
release made at the same moment as the User's comment deadlocks, and the comment is refused.

## The change

**Routing** (`add_comment`, migration pgu1026, mirrored at the end of schema.sql). When the User comments on a
ticket that has an unresolved `operator:<name>` blocker and is not in a terminal stage:
- the Director gets one notice, and the owner gets none;
- every other comment is routed as before.

**The notice** is a `ticket_update` to the Director whose payload kind is `operator_response`, the same shape as
`ask_director`'s question. Its text:
- names the ticket, its stage and its owner;
- names the standing operator blocker or blockers;
- says the User answered, and adds "marked urgent" when the comment was;
- says only the Director can inspect the comment and release or keep the blocker;
- says a comment is evidence to read, not proof that the prerequisite is met, and that nothing was released;
- says the owner is not told while the blocker stands and that a release tells it, unless the Director is the owner.

**Who answered.** The board records no tie between the User and an operator's name. Board UI users are OS
accounts, and the comment row says `user`. So the notice says the User answered while the named blocker stands,
and claims no more. The User is the role `add_comment` already calls the User (`user`).

**The comment** is stored exactly as before, with its text, author and urgency. Nothing reads its text. Nothing
releases the blocker, which stays until the Director releases it.

**Once per comment.** `ticket_operator_responses` records each comment the Director was told about, keyed on the
comment's id, and the notice is enqueued only when that record is new:
- retrying or replaying the call for the same comment, even after its notice was delivered and acknowledged,
  queues nothing more;
- the queue row is keyed on the comment, so two answers are two notices;
- the listener sends each notice once, as it sends anything: a restarted listener finds nothing left to send, and a
  notice claimed by a listener that died goes out once after the claim timeout.

**Delivery** (`notification_eligibility`). The notice is current only while it is addressed to the Director and a
blocker it names still stands on an unfinished ticket (`ticket_board.operator_response_is_current`, granted to the
listener). If the Director releases before the notice goes out, it is dropped as stale. The owner hears once,
from the release.

**The release** (`release_external_blocker`) tells the owner once, as before, and now says what it recorded:
"external blocker released (operator:eric): <reason>". The evidence stays in the release comment.

**Privileges.** The three new functions run as their owner (`SECURITY DEFINER`), and PostgreSQL lets PUBLIC execute
any new function. The migration therefore revokes PUBLIC's EXECUTE on each one in the same transaction that creates
it. Production runs the migration runner and then rbac.sql as two separate commands, and the runner revokes PUBLIC
only after every migration has run. Without the in-transaction revoke, every pane role could have called
`notify_operator_response` in the window between the migration committing and those later revocations, writing the
once-per-comment record and queueing a Director notice (Audit's finding on the first candidate). rbac.sql then
grants the listener `operator_response_is_current` and nothing else.

**Ordering.** `add_comment` now takes the ticket's row first (`FOR NO KEY UPDATE`), in the same order
`release_external_blocker` and `set_blockers` use. Before this, a release and a comment at the same moment both
appended at the same comment position and deadlocked, and the comment was refused. Now one waits for the other:
- **release first:** the comment sees no blocker and goes to the owner;
- **comment first:** the Director's notice becomes stale when the release commits, and the owner hears once.

Holding the row also means the blockers the comment reads are the ones standing when it lands.

## What it does not change

- **The formal hand-over.** The Director hand-over that asked for the blocker closes when the blocker is recorded,
  and `request-dependency` is still refused while the blocker stands. Its reminders stay silent.
- **Authority.** Releasing and keeping a blocker remain the Director's. The User's comment grants nothing.
- **Other comments:**
  - a User comment with no blocker, with a blocker that is another ticket, or after the release goes to the owner;
  - a role's urgent comment goes to the owner;
  - a role's plain comment notifies nobody;
  - the Director's own comment reaches the owner;
  - a comment on a finished ticket tells nobody.
- **A ticket the Director owns** gets one Director notice, not a Director notice and an owner notice.

## Evidence

`tests/operator_response_test.py`, 79 checks. It uses the production-built boards of `router_role_board_test`
(schema.sql, the real migrations, rbac.sql, the HTTP server and the real listener; only the panes are stood in
for). Main runs first, from a git archive, then this tree, then main's board upgraded with this tree's migrations.

- **SYRD-691 as it ran.** Ops' request reaches the Director. The Director records `operator:eric`, and Ops is told.
  The hand-over closes and cannot reopen. Then the User answers:
  - on main, one Ops notice and nothing to the Director;
  - here, one Director notice naming `operator:eric`, and nothing to Ops.

  In both trees the comment is stored as written and the blocker still stands.
- **Restart and replay.** A new listener sends nothing more. The notice is out of the queue, and its trace is one
  enqueue and one send. A notice claimed by a listener that died is sent once.
- **Retain.** The Director's own comment reaches Ops, and the blocker stands. A later urgent User comment is one
  more Director notice saying "marked urgent".
- **Release.** One Ops wake-up: "external blocker released (operator:eric): Eric approved on the ticket; run the
  approved install now." Nothing goes to the Director. On main the wake-up said only "external blocker released".
- **Released before delivery.** The notice is dropped as stale, and the owner hears once.
- **The race, both orders.** Queued behind a held row lock, each move's wait is confirmed in `pg_stat_activity`.
  Both writes land, the owner gets one wake-up, and the Director gets none. On main, release-first deadlocks.
- **Unchanged routes.** Each case listed under "What it does not change" was run.
- **Ownership.** A ticket the Director owns gets one notice. Two answers give two notices.
- **The database alone.** `operator_response_is_current`, called as the listener:
  - is true while a named blocker stands, including when one of two names stands;
  - is false for another operator, a released blocker, a resolved row, a finished or missing ticket, and a payload
    naming no operators.

  A notice addressed to anyone but the Director is not delivered. Re-raising the notice for a delivered comment
  queues nothing.
- **The upgrade window.** main's board is built as deployed. pgu1026 is then applied alone, the way the runner
  applies one file, before both the runner's exit-time revocation and rbac.sql:
  - no role, pane, service or listener, may execute any of the three new functions;
  - after rbac.sql, only the listener may execute `operator_response_is_current`;
  - the control (the same migration with its revocations removed) reproduces Audit's finding: app and ops may
    execute all three until rbac.sql runs.
- **Mirror.** schema.sql's last copies of pgu1026's functions are pgu1026's, byte for byte.

**Mutants:** 24, each killed.
- **SQL mutants** edit the migration and schema.sql's copy alike:
  - the row lock;
  - the User and terminal conditions;
  - the routing branch and the else;
  - the resolved and `operator:` filters;
  - the once-per-comment record;
  - the recipient;
  - both message clauses;
  - the queue key;
  - the payload's operators;
  - the currentness terms;
  - the release summary;
  - which comment is recorded.
- **Python mutants** cover the eligibility branch, its recipient test and its answer.
- **The revocations.** Removing any one of the three is caught by the upgrade-window check.
- **The grant.** Removing the listener's grant makes the real listener's pass fail with "permission denied".

Nothing live was changed.
