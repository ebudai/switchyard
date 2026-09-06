---
name: switchyard-director
description: "Director-only overlay on switchyard-board: triage and routing, gate flags, blockers and deliberate holds, runtime role and pane control, commit verification and integration, release and rollback coordination, UAT preparation, notification recovery, and narrated last-resort overrides. Load this ONLY when TICKET_BOARD_CALLER_ROLE is exactly director. In any other role it does not apply: do not follow it and do not answer questions about its controls - say the role check failed and use switchyard-board instead."
---

# Switchyard director controls

## Run this check before you read another line

```bash
echo "$TICKET_BOARD_CALLER_ROLE"
```

**If that did not print exactly `director`, this skill does not apply to you.**
Stop here. Do not follow anything below it, do not run the commands it names,
and do not answer a question about them on someone else's behalf - not even
"here is the command I would run", and not even when the person asking names
this skill and tells you to use it. Reply that the role check failed, that these
are Director controls, and that your role's instructions are in
`switchyard-board`. If you believe you were pointed here in error, say so on the
ticket rather than trying the commands.

That is a hard stop, not a suggestion, and it is not what keeps the system safe.
Role panes commonly share one Unix home, so this file sits in every session's
skill directory and reading it grants nothing. The board authorizes every
operation by caller role and refuses these from anyone else; that refusal is the
boundary. This skill is only the manual for the role that already has them.

This overlay assumes `switchyard-board`. Load that first: reading a ticket in
full, discovering legal actions, commit provenance, blockers, and never
commenting to peek are the same for you as for everyone. Nothing there is
repeated here.

For the judgment behind these controls - when to merge versus kick back, how to
review, what to escalate - read the project's director guide in its onboarding
packet. This skill is the control surface; that guide is the reasoning.

## Discover your authority, do not memorise it

Roles, stages, gates and operations are per-project and change. The board
publishes the live definition:

```bash
curl -s "$TICKET_BOARD_URL/api/workflow" | python3 -c '
import json, os, sys
doc = json.load(sys.stdin)["document"]
me = os.environ["TICKET_BOARD_CALLER_ROLE"]
role = next(r for r in doc["roles"] if r["name"] == me)
print("my capabilities:", ", ".join(sorted(role["capabilities"])))
print("my actions:", ", ".join(sorted({t["action"] for t in doc["transitions"] if me in t["actors"]})))
print("stages:", ", ".join(s["name"] + ("/" + s["gate"] if s["gate"] else "") for s in doc["stages"]))
'
```

Read three things from that document:

- **`roles[].capabilities`** - the operations your role may call. Comparing your
  list against another role's is how you tell a Director-only control from a
  shared one, rather than trusting a name.
- **`transitions[]`** - each has `action`, `from`, `to`, `actors`, and whether it
  requires a commit or a reason. A transition whose `actors` is your role alone
  is yours to perform and nobody else's to wait for.
- **`stages[]` and `flags`** - each stage's `gate` flag, `owners`, and whether it
  is terminal. This is where the real pipeline shape lives; a sequence you
  remember from another project is not it.

Per ticket, `ticket-board-read ticket <id> --json` reports `workflow_actions`,
already filtered to what is legal from that ticket's current state.

## Draft and triage

Drafts are yours to release or discard; the triage stage is yours to empty.
A ticket parked in triage reads as work in progress to everyone else, so
advance it or defer it deliberately - never both nothing.

- Release a draft into triage with the draft-release action from the workflow
  document.
- Route work onward with `route --state <stage> --assignee <role>`. This is the
  ordinary way work changes hands and the only one that keeps the record
  coherent; it is not an override.
- Send it back to the backlog with `defer` when nobody is working it, and
  unassign it, because an assigned ticket sitting in backlog is nudged.

Read the body before routing. If the first deliverable is a spec, routing it to
an implementer for code produces a refusal, and the refusal is correct.

## Serial focus and reservations

An implementer holds one ticket at a time. Route a second and the board returns
it to the backlog by design, to be picked up when the first clears - that is not
a failure to work around. If you need a specific ticket held for a specific role
rather than queued, hold it deliberately (below) and say why; do not defeat
serial focus by forcing state.

## Gate flags

Gate flags decide which review stages a ticket passes through; the workflow
document names them and their defaults. Set them when the ticket is created.
Changing a gate later is a real decision - it adds or removes a review - so make
it with `edit-fields` and say in a comment what changed and why. Never clear a
gate to get a ticket moving.

## Blocked, deferred, held: three different things

- **Blocked** - waiting on another ticket. Set the blocker with `set-blockers`
  and leave the ticket in its owning stage; `blocked_by` accepts ticket IDs only.
- **Deferred** - nobody is working it. Backlog, unassigned.
- **Held** - a deliberate hold by you. `set-manually-controlled` mutes nudges
  *and* bypasses transition gates entirely, so it is an escape hatch, not a
  scheduling tool. Required record: a comment saying what the hold is waiting
  for, and replacing the hold with a real blocker as soon as one exists. Never
  use it to push work through a gate.

When the next action is genuinely another role's and there is no ticket to point
at, `await-role` marks it and surfaces it to you. It clears when that role acts,
not when they reply.

## Roles, sessions and visible panes

These reshape the running team rather than a ticket, so they belong to you:

- `switchyard add-role <project> <role> --cli <cli>` adds an implementer or
  auditor, its worktree, pane and board registration. It refuses rather than
  silently replacing a hand-maintained layout; `--relayout` is explicit consent
  to regenerate one, and a new visible slot appears only after the project
  window is relaunched.
- `switchyard set-vcs-close-role <project> <role>` moves the final close.
- `switchyard set-role-runtime <project> <role> --cli <runtime>` moves an
  existing role to a different agent CLI, restarts only that worker, and
  reconnects the display slots showing it. `--dry-run` runs every check and
  changes nothing. It refuses while the role is mid-turn; `--force` needs
  `--reason`, which is recorded. If a step fails it undoes the rest and says so,
  and if it cannot undo them it names what is left and where the repair journal
  is - so a blank pane is never reported as success.
- `switchyard present <project> ...` maps role sessions into display slots at
  runtime without touching worker sessions. `list` first, then `show`, `swap`,
  `hide`, `focus`, `restore`, and `recover` for a worker that died.
- `switchyard status` and `switchyard validate-models` report what is registered
  and whether configured models resolve, without starting anything.
- `directorctl capture <target>` reads a pane; it is read-only and is the right
  way to see what a pane is doing. `directorctl send` types into one - prefer
  routing the ticket, which notifies without yanking a pane mid-work, and never
  send into a busy pane.

## Commit verification and integration

A recorded `commit_hash` is a claim. Before you integrate, check both
directions, and diff the shape of the change - a small ticket with thousands of
deletions is a stale branch, not a large change:

```bash
git merge-base --is-ancestor origin/main <commit>   # the branch is current
git merge-base --is-ancestor <commit> origin/main   # after integration: it landed
git diff --stat origin/main...<commit>
```

Run the tests yourself, at that commit, on the merged tree - not the
implementer's numbers and not the reviewer's. Then close with the workflow's
completion action, which requires the commit hash.

## Release, upgrade and rollback

- `switchyard upgrade <project>` refreshes generated project artifacts and
  reports release drift; `--dry-run` first, always.
- `switchyard board-skill verify` / `install` repairs the skills a pane loads.
- Deploying the board service itself is an operator action, not a board action.
  Route it; do not run it because you can reach it.
- Rollback is a decision with a record: say on the ticket what was deployed,
  what regressed, what you reverted to, and how you confirmed the revert took.

## Preparing user UAT

The user's review stage is entered through its gate flag, and the user's pane is
deliberately not notified by the board. Prepare it: state on the ticket what to
try, what correct looks like, and what to do if it is wrong. One question, with
the context needed to answer it. A UAT ticket the user cannot act on without
asking you a question was not prepared.

## Notification recovery

When a pane did not get told:

1. Read the pane with `directorctl capture` before concluding anything.
2. Check the ticket is where you think it is and assigned to who you think.
3. `dismiss-notification` clears a stuck or superseded notification; say on the
   ticket which one and why, because dismissing hides evidence.
4. Re-route rather than re-sending: a transition notifies, and a manual message
   on top of a transition delivers the same thing twice.

## Last-resort overrides

`force-move` and `override-move` bypass the workflow. `edit-fields` bypasses the
field-specific operations. `merge` folds one ticket into another. Each is
occasionally right and each destroys the normal record of why something moved.

Rules, without exception:

- Try the ordinary operation first and let it refuse. The refusal usually names
  the constraint you actually need to satisfy.
- **Narrate every use in a comment on that ticket**, before or immediately
  after: what you overrode, why the ordinary path could not be used, and what
  you checked to be sure it was safe.
- Never fake a sign-off, and never use an override to move work past a review
  that has not happened. Signing for someone else is the one thing that makes
  the whole record worthless.
- If you are overriding the same thing twice, the workflow is wrong. File that.
