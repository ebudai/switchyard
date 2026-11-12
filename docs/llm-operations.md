# Switchyard LLM operations reference

This is a compact retrieval index for roles already running inside a
Switchyard pane. It does not replace role skills or the live workflow. Load the
smallest normative source that answers the current decision.

## Source-of-truth order

1. **Identity and endpoints:** `TICKET_BOARD_CALLER_ROLE`,
   `TICKET_BOARD_URL`, `TICKET_BOARD_SOCKET`,
   `TICKET_BOARD_TICKET_PREFIX`, and `TICKET_BOARD_PROJECT` in the pane.
2. **This ticket now:** `ticket-board-read ticket <id> --json`, especially the
   body, implementation, comments, blockers, candidate and
   `workflow_actions`.
3. **This project's contract:** `GET $TICKET_BOARD_URL/api/workflow` for roles,
   capabilities, stages, gates and transitions.
4. **Role authority and invariants:** `switchyard-board`, plus the overlay for
   the current role when one exists.
5. **Exact CLI syntax:** `<command> --help` and `<command> <subcommand> --help`.
6. **Explanation:** the onboarding and operator guides indexed by
   [Switchyard documentation](documentation-map.md).

Never infer a port, prefix, role, stage sequence or legal action from another
project or an old ticket record.

## Start every ticket

```bash
echo "$TICKET_BOARD_CALLER_ROLE"
ticket-board-read ticket "$TICKET_BOARD_TICKET_PREFIX-123" --json
```

Read the whole ticket, not only its notification. Confirm the intended action
lists your role in `workflow_actions`; if it is owner-scoped, confirm the ticket
is assigned to you. Read `<action> --help` before writing when its required
arguments are not already in the role skill.

Reads are HTTP GETs and change nothing. A comment is a write, is permanent and
notifies the assignee; never comment merely to inspect.

## Authority by role

| Role | Normative instruction | Boundary |
|---|---|---|
| Router | Its launch prompt only | Read the named ticket once; `router-assign` or `router-return` |
| Implementer, Audit, Inspector and ordinary roles | `switchyard-board` skill | Only actions published for the role and ticket |
| Director | `switchyard-board`, then `switchyard-director` after the exact role check | Coordinates, routes and recovers; cannot grant itself approval |
| Librarian | `switchyard-board`, then `switchyard-librarian` after the exact role check | One ticket; one of three documentation decisions; documents only |
| User | Board UI and the workflow's User actions | Acceptance belongs to the human; it is never an ordinary role dependency |
| Publisher | Workflow and listener implementation | Mechanical actor with no pane or model; no role impersonates it |

An overlay whose role check fails is not an advisory reference. Stop using it
and return to `switchyard-board`.

## Board contract

The live workflow, not this page, names stages and actions. To inspect the
filtered actions for one ticket:

```bash
ticket-board-read ticket <id> --json | python3 -c '
import json, sys
t = json.load(sys.stdin)
for a in t["workflow_actions"]:
    print(a["action"], "->", a["to"], "actors=", ",".join(a["actors"]),
          "commit=", a["require_commit"], "reason=", a["require_reason"],
          "owner_scoped=", a["owner_scoped"])
'
```

To inspect the project contract, read `/api/workflow`. Directors have a compact
query in the `switchyard-director` skill. Use underscores in JSON action names
and dashes in CLI subcommands.

## Evidence and provenance invariants

- A ticket's candidate hash is a claim. Resolve it in the correct repository
  and check ancestry before relying on it.
- Never borrow an unrelated hash. Use the workflow's no-commit action, with a
  real reason, when finished work legitimately produced no commit.
- A review verdict covers the exact candidate and evidence it names. A changed
  candidate clears later sign-offs.
- Say what you ran, where, at which SHA, and what it printed, and what you
  reused. “Tests pass” is not evidence when the run belonged to another tree,
  or names no commit, commands or output.
- The implementer records the complete validation exactly (commit and tree,
  base, commands, environment, outcomes, baseline comparison). Audit verifies
  it applies and runs focused checks; Final Sign-Off verifies the exact
  candidate, decisions and gates. Neither reruns the complete suite merely
  because the ticket entered its stage: a rerun needs a named reason the result
  could differ, recorded on the ticket first (board guide, “Test evidence”).
- Workflows, examples and configuration consumed by programs are product
  inputs, not prose. A documentation role reviews them but does not edit them.
- Commit and publish from the checkout that actually contains the candidate.
- A cross-board report goes to the immediate report parent unless `--to` names
  one ancestor or `all`; `ticket-board-read report-ancestors` lists them. Use
  more than one only when the same report concerns each. One preview and
  digest cover the whole set; a partial result is retried with the same
  digest, which files only where it is missing (board guide, report ancestry).

## Waits, blockers and silence

| Situation | Action |
|---|---|
| Work waits on another role | `request-dependency <id> --role <role> --reason "..."` |
| Work waits on another ticket or an operator | Ask the Director to record `blocked_by`; the Director later uses `release-external-blocker` |
| Work waits on the User | Finish through the declared gates, or the Director uses `await-user` only where the workflow permits it |
| A reminder should pause until a known time | Director uses the declared snooze operation; do not invent a blocker |
| Work is paused (`ticket-board-read pause`; a `WORK PAUSED` notice after a stop) | Drain: finish the turn you are in, then stay idle. Stop: one comment on your ticket saying what is done, half-done and next, then start nothing new until `WORK RESUMED`. Only the Director or the User pauses or resumes |
| A role is quiet after routing | Yield; the board reports work and stalls. Make one targeted check only with concrete evidence of failed delivery |

A comment does not record a wait. A blocked ticket stays in its own stage with
its owner. The awaited role acting clears a role wait; merely replying may not.

## Background work

- Track the exact process started; never wait with a process search that can
  match itself.
- Give tests and comparisons a deadline, preferably with
  `switchyard-bounded`.
- Long commands have execution leases visible through
  `ticket-board-read leases`; only the Director extends or interrupts them.
- Stop services, VMs and other long-lived children started for the ticket
  before transitioning it.
- Do not poll tickets, panes or queues. The notification path is the normal
  continuation mechanism.

## Reviews and returns

- Review roles sign off or return findings; they do not become implementers or
  reassign work.
- Findings name the surface, observed behavior, expected behavior and evidence
  needed to act without another conversation.
- Audit's `audit-kick-back` returns to the work's substantive origin recorded in
  `audit_return` (the implementer or designer, never the Librarian's
  documentation layer). It is the default for every finding that is not
  documentation-only: code, tests, configuration, executable examples,
  embedded help, security or a mix. If that record is absent or invalid, Audit
  requests the Director; it does not guess.
- `audit-documentation-kick-back` returns a documentation-only finding to the
  Documentation owner the ticket passed (`audit_return.documentation`), on the
  audited candidate. Audit chooses it explicitly; without that record it is
  refused, and the ordinary kick-back is the way back.
- The Librarian either approves unchanged with a reason, publishes one
  documentation-only child of the exact candidate and signs off, or returns
  concrete findings.
- The Director verifies evidence and integration identity but does not write
  product code or self-approve.

## Human acceptance and publication

Before a ticket enters User Review, the Director records current UAT steps:
what the User does, what they should see, and what choice they are making. The
ticket must first pass every gate it owes.

Where a Publisher is enabled, Audit approval hands the exact candidate to a
mechanical compare-and-swap publication step. Roles do not push the integration
branch in parallel. A non-fast-forward candidate returns to its producer; an
operational publication failure escalates to the Director.

## Refusals and recovery

Treat a refusal as a named constraint, not an invitation to change caller role,
socket, board or repository.

1. Read the refusal literally and re-read the ticket's live actions.
2. Correct the one missing precondition within your authority.
3. If another role must act, record a dependency rather than only commenting.
4. If the work is wrong or outside your role, use the role's declared return
   action with concrete findings.
5. Only the Director uses recovery, override, release or rollback controls, and
   narrates last-resort overrides on the ticket.

For host or release failures, use the human task index rather than inventing a
shell recovery: [role and pane recovery](documentation-map.md#find-a-task),
[board service](ticket-board-service.md), and
[upgrade discovery](documentation-map.md#discover-what-changed-before-an-upgrade).

## Finish the turn

1. Record enough evidence on the ticket for the next role.
2. Take the transition yourself; a “done” comment does not move a ticket.
3. Verify the write succeeded from its command response.
4. Stop. Do not follow the ticket into the next role's review or poll it for
   movement.
