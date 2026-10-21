# SYRD-625: the Publisher

The Director used to integrate approved work by hand: fetch, check ancestry,
push `main` with a lease, refresh the board's cache, close. The Publisher does
that routine mechanically, as a workflow stage. The Director keeps the
decisions that are not mechanical.

## What the Publisher is

The Publisher is a role named `publisher`, of kind `system`. It has:

- no runtime and no pane target;
- no slot, session, onboarding prompt or template;
- no capability.

Provisioning lists it among the roles that are never processes
(`NON_PROCESS_ROLES`), so no account, pane or launcher entry is ever made for
it. Its only moves are three declared transitions, and the database lets them
be taken only through the Publisher's own functions.

The board's notify listener runs it, as the project account. That account
already publishes every role's candidates with its SSH identity. Each listener
pass checks for tickets ready in the publication stage and, when there are some
and no worker is running, starts one background worker on its own database
connection (`scripts/ticket_board/publisher_pass.py`). The engine itself is
`scripts/ticket_board/publisher.py`. It never edits a file, rebases, merges,
chooses between candidates, force-pushes or waives a gate.

## Turning it on

```
switchyard publisher <project> enable [--dry-run]
switchyard publisher <project> disable
```

This writes the tenant's own declared workflow through workflow_manage, like
`router-role` and `documentation-gate`. Root does not run it. A board without
the Publisher behaves exactly as before.

`with_publisher` (`scripts/ticket_board/publisher_stage.py`) composes:

- **`publication`**, after Audit. It is owned by the Publisher, ungated,
  signed off by nobody, and notifies nobody. Every approval out of Audit, and
  Audit's own gate skip, now enter it. So it runs after every required code,
  documentation, inspection and Audit gate the ticket was subject to.
- **`publication_failed`**, held by the Director, who is told when work lands
  there.
- **The Publisher's moves out of `publication`**, owner-scoped, each with a
  reason:
  - `publication_published` goes to the stage Audit used to approve into, and
    the ordinary gate walk continues from there. On the shipped workflow, a
    ticket that needs the User's sign-off goes to Director acceptance and then
    User Review; any other goes to Final Sign-Off.
  - `publication_return` goes to the recorded implementer and clears every
    sign-off. The return also clears the commit, so the next candidate passes
    every gate again.
  - `publication_escalate` goes to `publication_failed`.
- **The Director's answers to a failure:**
  - `publication_retry` hands the ticket back to the Publisher;
  - `publication_kick_back` returns it to the implementer, clearing every
    sign-off;
  - defer and cancel are available from both new stages.

`workflow_config.validate_publisher` holds this shape in every document. The
Publisher cannot be given a runtime, pane, slot, prompt or capability, another
stage, or another move. Nothing but publication and the Director's own moves may
reach the stage it hands on to.

## One attempt

The board journals each attempt in `ticket_board.publication_attempts`, through
four functions only the listener may call (`publisher_pending`,
`publisher_begin`, `publisher_step`, `publisher_finish`; migration pgu1004).

An attempt belongs to a ticket and its assignment round. A ticket has at most
one open attempt; within a round a re-run resumes it, and an attempt from an
older round, or for a different recorded commit, is superseded.

The journal steps are `started`, `prepared`, `pushing`, `pushed` and
`verified`. Each is recorded before the next begins.

1. **Gates.** The ticket must be in `publication`, assigned to the Publisher,
   not held, not parked, not blocked. It must have every review sign-off it
   owes before publication: each review leading into the stage whose gate is
   on. If size review is on, this exact commit must be measured with no open
   finding. A blocked or held ticket simply waits.
2. **Authority.**
   - The board must verify commits against exactly one repository. That
     repository is the trusted cache.
   - The cache's `origin` is the public repository, and the push goes to its
     push URL (`remote.origin.pushurl` when set).
   - The integration branch is the one the public repository's `HEAD` names.
3. **Trusted-cache state.**
   - The candidate must resolve in the cache to the exact recorded commit. If
     it is missing, it is fetched there by its SHA from the cache's origin.
   - The cache's copy of the branch is brought up to the public tip.
4. **Lineage.** A candidate already on the public branch is recorded as
   published, with no push. Otherwise it must be exactly one commit whose only
   parent is the public tip.
5. **Bundle.** A bundle of that one commit is built from the trusted cache, in
   the Publisher's scratch repository under
   `$XDG_STATE_HOME/switchyard/publisher/<project>/`. It must hold one head
   (the candidate) and one prerequisite (the tip), and its sha256 is journalled.
   A bundle that is missing or changed when an attempt resumes is rebuilt.
6. **Push.** The push is made from a fresh repository that holds only the
   public branch and the verified bundle:

   ```
   git push --force-with-lease=refs/heads/<branch>:<tip> <push URL> <candidate>:refs/heads/<branch>
   ```

   The lease is the compare-and-swap. The candidate's only parent is the
   leased tip, so the update is always a fast-forward; if the branch moved,
   the forge refuses the push and nothing is overwritten.

   Git runs with no global or system configuration, no inherited `GIT_*`
   variables, no prompts, and `ssh -o BatchMode=yes`. So no role's
   `insteadOf`, hook path or credential helper can redirect or change what is
   pushed.
7. **Verify.** The public branch is read back through the public URL until it
   carries the candidate, at most 5 times. The trusted cache's
   `refs/remotes/origin/<branch>` is refreshed and checked too.
8. **Record.** `publisher_finish` writes the evidence comment and takes
   `publication_published`, in one transaction. The database refuses the
   record unless:
   - the journal reached `verified`;
   - the verified commit is the ticket's recorded candidate;
   - every owed sign-off is present;
   - size review has nothing open.

## Where a failure goes

Each attempt ends exactly once. Closing it is the same transaction as the
transition, so a return and an escalation exclude each other and neither
happens twice.

- **Returned to the implementer** when the candidate or its lineage must
  change. The comment carries the exact diagnostic, and every sign-off is
  cleared. This happens when:
  - public main advanced past the candidate's parent (the comment names the
    paths both sides changed);
  - main moved during the push, and the lease refused it;
  - the candidate is several commits, or a merge;
  - its history differs from main;
  - it is published nowhere.
- **Escalated to the Director**, with the exact diagnostic and the repairs
  tried, for:
  - a refused credential;
  - a hook or branch-protection refusal;
  - transient failures that outlasted the retries;
  - an unusable publication target;
  - missing sign-offs or unresolved size findings;
  - a journal refusal;
  - a git helper failure;
  - a push that reported success while the public branch does not carry the
    candidate;
  - a publication the trusted cache cannot be brought up to.

## Mechanical repairs (the whole list)

- a stale trusted-cache branch, refreshed;
- a candidate missing from the cache, fetched by its SHA from the cache's
  origin;
- a missing or changed bundle, or a scratch repository git cannot read,
  rebuilt;
- an attempt that no longer matches its ticket, superseded;
- a transient remote read, fetch or push, retried at most 3 times;
- an attempt that stopped part-way, resumed from its journal and from what the
  public branch says.

Each repair is named in the evidence. None can change the candidate's SHA, tree
or parent, any sign-off or gate, or where the candidate is published.

## Who can take the moves

A trigger (`tickets_publisher_window`) refuses each of the Publisher's moves,
and any ticket write by a caller or actor named `publisher`, unless it comes
from `publisher_finish`'s window. That window names one ticket for one
statement. So no pane, HTTP caller, relay, stalled-ticket recovery or delegated
board-UI action can take a Publisher move. The Director's narrated override is
not a Publisher move and is unaffected.

## Evidence

`tests/publisher_board_test.py` runs on real boards, with real git: a bare
forge, the trusted cache as its clone, and implementers publishing at
`roles/<role>/...`. Where a case needs git to answer one command differently, a
refused credential or a dropped connection, the answer is injected around the
real git.

`tests/publisher_command_test.py` covers:

- the composition;
- every validator refusal;
- that the Publisher is never a process;
- the command against a real projection;
- the board's own SQL validator.
