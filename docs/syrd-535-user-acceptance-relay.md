# SYRD-535: relaying a User's acceptance where the workflow was declared after the grant

## What happened

MEFP-649 is a no-code coordination decision: `commit_exempt`, no commit,
`needs_user_signoff`, in `user_review` assigned to the User. The User accepted
its recommendation in conversation. The MEFP Director followed the Director
skill:
```
ticket-board-write workflow-action MEFP-649 relay_user_sign_off --payload-json '{"reason": ..., "commit_hash": ""}'
```
It was refused: `director cannot call relay_user_sign_off`. Nothing was forced.

## Measured, read-only, on MEFP at revision 66 and on main b93c858

- **No relay is declared.** In `user_review`, MEFP's workflow declares the
  User's `user_sign_off` (approve → `director_review`), the User's
  `user_reopen` (reopen → `analysis`), and the Director's route, cancel and
  defer.
- **Why not.** pgu953 (SYRD-217) grants `relay_user_sign_off` by migration,
  once, to the document a board has when the migration runs. Every condition
  it checks holds for MEFP today:
  - exactly one control role, `director`;
  - a `user` role with no target;
  - `director_review` is not terminal.

  But MEFP declared its workflow later (`legacy_workflow_declared`), and an
  applied migration does not run again. Nothing a Director could run added the
  relay.
- **The executor already handles a no-code acceptance.** On a commit-exempt
  ticket with no commit, the payload must not contain `commit_hash` at all; an
  empty string fails the hash check ("invalid commit hash"). A coded ticket
  must name its recorded candidate, and the stage gate and every earlier
  review must be in place.
- **No rejection relay can exist on MEFP.** A relay may only return or approve,
  and must mirror a move the relayed role makes from the same stage
  (`workflow_config.validate`). MEFP's User rejects by `user_reopen`, so no
  rejection relay can be added. Giving the User a return move is a decision
  about MEFP's workflow, and is not made here.

## The change

- **`scripts/ticket_board/workflow_relays.py`.** It holds pgu953's rule in
  Python, against a document the caller holds.
  - **The relay:** `relay_user_sign_off`, copied from the User's own approval
    in `user_review` (same destination, same sign-offs cleared). It is granted
    to the single control role, requires a reason and a commit, and records
    `relays_decision_of: "user"`.
  - **When it declines,** saying why: there is no User approval; more than one
    control role; the relay already exists; the action name is taken; the User
    has a pane; or the approval would end the ticket.
  - **Scope.** It adds one transition and changes nothing else.
    `rejection_relay_finding` states the rejection case.
- **`ticket-board-write add-user-acceptance-relay`**, the Director's
  configure-workflow authority, bounded to that one change.
  - **Preview, by default, changes nothing.** It shows the live revision, the
    transition it would add, digests of the document before and after, the
    rejection finding, and the board's own validation as a dry run.
  - **`--apply --expected-revision <N>`** applies exactly that change. It is
    refused if the workflow is not at the revision reviewed, and the board's
    own revision check backs that up.
  - `write_client.read_workflow()` is the plain GET this needs.
- **The Director skill and onboarding guide.**
  - **Conditional relays.** Use a relay only when the ticket's
    `workflow_actions` lists it with you among its actors.
  - **No-code acceptance:** a reason and no `commit_hash` key.
  - **Coded acceptance:** the recorded candidate. A rejection relay only where
    it is listed.
  - **The configuration path** when the acceptance relay is missing, and why
    a User who reopens has no rejection relay.

**Unchanged:** pgu953 itself; every relay rule in validation and in the
executor; the earlier review gates; candidate binding for coded tickets; and the
User's own actions.

## Audit correction: one board for the read, the dry run and the apply

Audit returned a0ef773.
- **The split.** The workflow read always used the client's HTTP URL. The dry
  run and the apply went through `_post`, which prefers the Unix socket. With
  both configured, the command previewed board A and wrote A's document into
  board B; Audit reproduced it and so does the regression below.
- **The read follows the writes.** `read_workflow()` reads over the same Unix
  socket when writes resolve to one, never falling back to HTTP; HTTP-only is
  unchanged. `configure_workflow(…, same_endpoint=True)` posts to that socket
  with no TCP fallback.
- **The endpoint is resolved once.** The regression for an implicit socket
  caught a second defect in my first fix. The ambient socket is re-resolved on
  every call and chosen only if its file exists, so a board that vanished
  between read and write turned the "same endpoint" write into an HTTP write to
  board A. The command now runs on `client.pinned_to_resolved_endpoint()`: one
  resolution, as an explicit socket or as no socket.
- **The preview shows where it went.** The result names the endpoint it read
  and wrote.

**`tests/relay_workflow_endpoint_test.py`** sets up two disposable boards that
record every request: board A over HTTP, and board B over a Unix socket. They
serve different projects at the same revision.
- **Reproduction.** The rejected candidate, run in a child, reads A and writes
  `board-a` into B.
- **Now, with a socket:** the preview and the apply touch only B and write B's
  own document.
- **Refused before any configure call, with A untouched:**
  - a stale revision;
  - a missing socket;
  - a socket that refuses the read;
  - an implicit socket that vanishes after the read.
- **HTTP-only** still reads and writes the one HTTP board.
- **Results.** 10 checks, both under `env -i` and in a role pane. 6 of 6
  mutants were killed, each on its assertion line: the read over HTTP; writes
  ignoring the binding; a socket failure falling back; the reviewed revision
  unchecked; a refused read accepted; and the endpoint not pinned.
- **The 27 suites naming the client or CLI** match main, and the relay suite's
  33 checks still pass.

## Handoff for MEFP's Director

This was not done here, and nothing was written to MEFP. It follows once this
change is in the shared release MEFP runs, with the shared client by path if
the pane's `switchyard` tooling predates it:

1. **Preview, which writes nothing:**
   `ticket-board-write add-user-acceptance-relay`. Check:
   - `revision` (66 when this was measured);
   - `added` is exactly the `relay_user_sign_off` above, `user_review` →
     `director_review`, actors `["director"]`;
   - `validated` is true;
   - `rejection` says there is no rejection relay to add, because MEFP's User
     reopens.

   On MEFP's revision-66 document this adds one transition, leaves every
   other key unchanged, passes validation, and a second run adds nothing,
   checked read-only against a copy.
2. **Apply,** after review:
   `ticket-board-write add-user-acceptance-relay --apply --expected-revision <revision from step 1>`.
   Record the change, with the old and new digests, on a ticket.
3. **For MEFP-649,** only with the User's acceptance as MEFP's Director
   received it. Confirm `relay_user_sign_off` is now in the ticket's
   `workflow_actions`, then run:
   ```
   ticket-board-write workflow-action MEFP-649 relay_user_sign_off --payload-json '{"reason": "<what the User said>"}'
   ```
   It takes no `commit_hash`, not even an empty one.
4. **Rejections stay as they are.** Record the User's words on the ticket. If
   MEFP wants rejections relayable, giving its User a return move from
   `user_review` is a workflow review for MEFP to decide.

## Evidence

- **`tests/relay_user_acceptance_configuration_test.py`.** It runs on a
  production-built board with a declared workflow shaped like MEFP's (the User
  signs off or reopens), stored through the real apply, and drives the real
  CLI and HTTP server. Main's code and SQL, run in a child from a git worktree,
  reproduce MEFP's refusal and have no command to add the relay. The cases:
  - **Absent relay:** refused with MEFP's exact words, and nothing recorded.
  - **Preview:** writes nothing, and validates the transition on the board.
  - **Stale apply:** refused.
  - **Apply:** adds exactly the one transition, with every other key equal.
  - **Rerun:** adds nothing.
  - **No-code acceptance:** with an empty `commit_hash` it is refused ("invalid
    commit hash"). Without the key it is the User's sign-off, entered and
    recorded as relayed.
  - **Coded acceptance:** naming another commit is refused, and the recorded
    candidate is accepted.
  - **Missing earlier review:** refused.
  - **Rejection:** the User's reopen stays the User's, and there is no
    rejection relay.
  - **Each decline reason,** in its own pure-function case.
  - **The docs:** every documented command parses with the real CLI, and the
    no-code examples carry no `commit_hash`.

  It passes both under `env -i` and in a role pane: 33 checks.
- **Mutation, with `tests/bounded_run.py mutate`: 12 of 12 killed.** Each
  kill was confirmed from its assertion line. The mutants cover:
  - the relay's sign-offs, commit requirement and actor (killed by the
    board's own validator, through the preview);
  - each decline guard;
  - the rejection finding;
  - an apply ignoring the reviewed revision;
  - a preview that writes;
  - extra document changes.

  Three first died on a JSON parse rather than an assertion. The test now
  turns a refused preview into the preview's own assertion.
- **Sweep against b93c858.** 86 suites naming the CLI, the client, workflow
  configuration, relays or the Director documents were run under `env -i`,
  with refusing stubs for claude, codex, agy and konsole; none was reached.
  78 pass on both trees and 7 fail identically. `ticket_board_director_reassign_test`
  failed once on main under the parallel sweep and passes on both trees when
  rerun.
