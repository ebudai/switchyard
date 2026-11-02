# SYRD-726: the owner asks the Director to review a standing external blocker

SYRD-725 sends the User's answer to an operator wait to the Director. SYRD-693 showed the other edge:
- Eric approved the bounded canary and its timing.
- The Director kept `operator:eric` standing until the canary was done.
- Main ran the canary and recorded the evidence, but had no way to tell the Director the blocker was ready to
  release:
  - `request-dependency` is refused while an external blocker stands;
  - Main's plain "please release" comment notified nobody.
- The ticket stalled until Eric told the Director himself.

On main `98563090` the same steps give the same result.

## When to release and when to keep an operator blocker

The Director decides what a person's answer ends:
- **Authorization only.** A blocker that waited only for an authorization (approval, timing, a decision) is released
  when the Director accepts it, with that as the result. The owner's approved work then goes through its gates like
  any other.
- **The person's own result.** A blocker that waits on something the person does themselves (a key, a run they
  perform, their own result) is kept until that result is recorded.
- **Never combined.** An approval and the owner's later work are never folded into one release, and a blocker is
  never kept for the owner's own approved work as a way to watch it.

If the Director does keep a blocker while the owner finishes something, the owner needs a way to say it is ready.
That is this operation, the fail-safe for completion evidence that is ready behind a retained blocker. The Director
skill, the board skill and the board guide say this.

## The change

**The operation.** The command is:

```bash
ticket-board-write request-blocker-review <id> --ref operator:eric --reason "what is done, and where the evidence is"
```

It runs as `request_blocker_review` (pgu1027, mirrored at the end of schema.sql; rbac.sql grants it to the board's
writer).

**Who may ask.** The ticket's current owner, from its own registered pane:
- the board server names the calling process from the kernel, over the socket under process authority, as for
  SYRD-723's `set-blocked-reason`;
- the database checks again that the caller is the assignee and an owner of the stage, that the workflow declares
  the stage implementation, and that the process is the role's registered live runtime for the runtime and target the
  role declares.

A blocked ticket gives up its owner's serial slot (SYRD-568). So "active" means "owned in implementation", not "holds
the reservation": otherwise no owner of a blocked ticket could ever ask.

**What it names.** Exactly one standing external blocker on that ticket (`operator:<name>` or `project:PREFIX-N`),
and a reason of up to 2000 characters. A blocker that is a ticket on this board resolves by itself and is refused.

**What it does:**
- records the request in `ticket_blocker_reviews`, and on the ticket as the owner's comment "Review of <ref>
  requested: <reason>";
- touches the ticket, as a comment does;
- queues one notice to the Director, a `ticket_update` with payload kind `blocker_review`. The notice names the
  ticket, the blocker, the owner and the reason, and says to release with the result, keep it with a comment, or
  replace it, and that nothing was released or moved. The owner is not notified.

**What it leaves alone.** The blocker, the stage, the assignee, the sign-offs, any wait and every authority. Only the
Director releases, keeps or replaces a blocker. `release-external-blocker` tells the owner once, with its reason, as
SYRD-725 left it.

**Bounded.** The latest request for a blocker that still stands as it was recorded decides:
- **Same reason** (after trimming): the request already made. Nothing is written or sent.
- **Different reason before the Director has acted:** refused, saying when the pending request was made and what
  answers it. The owner's own comments don't count as the Director acting.
- **Different reason after the Director has acted:** a new request and a new notice. Acting means a Director comment
  on the ticket since the request, a release, or the blockers recorded again.

A blocker row that is removed ends every request about it. A release removes it; so does `set_blockers`, which deletes
and reinserts the list when it records the blockers again. A trigger (`end_blocker_reviews`) does this. Each request
has its own queue key, so two external blockers on one ticket get a notice each.

**Delivery.** The listener sends the notice only to the Director, and only while its request has not ended
(`blocker_review_is_current`, granted to the listener). If the blocker is released or recorded again before delivery,
the notice is dropped as stale. For a release, the owner hears once, from the release.

**Ordering.** The request takes the ticket's row first, as `release_external_blocker` and `set_blockers` do:
- a release committed first leaves nothing to review, and the request is refused;
- a request committed first is ended by the release, and its notice is dropped.

**Privileges.** The three new functions run as their owner (`SECURITY DEFINER`). Each is closed to PUBLIC in the same
transaction that creates it, because the migration runner revokes PUBLIC only after every migration and rbac.sql runs
after that (SYRD-725's finding). rbac.sql grants the service the request and the listener the question, and nothing
else.

**SYRD-725 unchanged.** A User comment while an operator blocker stands still goes straight to the Director. This
operation covers the later edge, the owner's completion.

## Evidence

`tests/blocker_review_test.py`, 74 checks. It uses `router_role_board_test`'s production-built boards and the real
listener, plus the real Unix-socket server under process authority, with this process registered as the owner's
pane. Main runs first, from a git archive, then this tree, then main's board upgraded with this tree's migrations.

**SYRD-693 as it ran:**
- Main asks the Director for Eric, and the Director records `operator:eric`.
- Eric's approval reaches the Director (SYRD-725), who keeps the blocker and says so.
- Main's plain "please release" comment reaches nobody, and Main cannot submit.
- On main there is no review request.
- Here, Main's request is one Director notice with the blocker and the reason, and nothing goes to Main:
  - the blocker, stage, assignee, sign-offs and wait are unchanged;
  - the ticket shows Main's comment and was updated;
  - the queued notice records the blocker, the reason and who asked.
- The release wakes Main once with its reason, and Main submits.

**Bounded:**
- **Same request again:** from the pane, or through the CLI with the reference in other case, it is a duplicate.
  A restarted listener sends nothing.
- **Something new:** refused before the Director acts, even after Main's own comment. After a Director comment,
  it is one more notice.
- **Release:** the two requests on record are ended by the release, after which a request finds no standing
  blocker.
- **Recorded again:** recording the blockers again ends a pending request, and a new one is accepted. If that
  happens before delivery, the notice is dropped.
- **Two external blockers** (`operator:eric` and `pie:PIE-17`) on one ticket: a request each gives two notices.

**Stale and races:**
- **Released before delivery:** the notice is dropped, and the owner is told once.
- **A release racing the request,** each queued behind a held row lock with its wait confirmed in
  `pg_stat_activity`:
  - release first: the request is refused and the owner gets one wake-up;
  - request first: the notice is dropped and the owner gets one wake-up.

  The Director gets nothing in either order.

**Refused, changing nothing and telling no one:**
- another role's ticket;
- a blocker the ticket doesn't have, a blocker that is a ticket, a malformed reference;
- no reason, or one over 2000 characters;
- an extra field, or a reference that isn't text;
- the HTTP port;
- the Director on another role's ticket, and on its own Triage ticket;
- a reviewer;
- a retired runtime, both at the server and with the database asked directly;
- a deferred ticket.

**Delivery:**
- A copy of an open request's notice addressed to ops is not delivered.
- The listener's question is false for an ended, missing or unnamed request.

**Privileges.** Main's board is built as deployed, then pgu1027 is applied alone:
- before rbac.sql, no role (pane, service or listener) may execute any of the three functions;
- after it, only the service may ask and only the listener may check;
- the control, the same migration without its revocations, lets app and ops execute all three until rbac.sql runs.

**Mirror.** schema.sql's last copies of pgu1027's functions are pgu1027's, byte for byte.

**Mutants:** 30, each killed.
- **SQL mutants**, planted in the migration and schema.sql's copy alike:
  - the trigger and its update;
  - the row lock;
  - the owner, stage-kind and runtime checks;
  - the external, named and reason checks;
  - the duplicate, pending and Director-only rules;
  - the open-request filter;
  - the comment, recipient, queue key, touch and payload;
  - the currentness filter;
  - each revocation.
- **Python mutants:** the eligibility branch, its recipient and its answer; the field and type checks.
- **The two grants:** removing the service's grant refuses the request. Removing the listener's makes its pass fail
  with "permission denied".

**Removed as no-ops:**
- the `awaiting_role_comment_touch` wrapper around the touch, since no wait stands beside an external blocker;
- the currentness function's blocker join and terminal check, since a blocker's removal already ends the request
  and no ticket finishes while a blocker stands.

Nothing live was changed.
