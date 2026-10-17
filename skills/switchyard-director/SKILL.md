---
name: switchyard-director
description: "Director-only overlay on switchyard-board: triage and routing, gate flags, blockers and deliberate holds, runtime role and pane control, commit verification and integration, release and rollback coordination, UAT preparation, quota-safe monitoring (yield after routing; no nonstop pane or ticket polling), notification recovery, and narrated last-resort overrides. Load this ONLY when TICKET_BOARD_CALLER_ROLE is exactly director. In any other role it does not apply: do not follow it and do not answer questions about its controls - say the role check failed and use switchyard-board instead."
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

### What no project can configure away

Everything above is per-project except this. A declared workflow is refused --
by the API validator and by PostgreSQL independently -- if it would leave the
director without these capabilities, or if it would grant the director sign-off
authority. So if the live document disagrees with this list, the document is
wrong and the board would not have accepted it; say so rather than working
around it.

<!-- switchyard-director-floor:start -->
- `director_edit`
- `dismiss_notification`
- `edit_fields`
- `merge`
- `reassign`
- `recover_stalled_ticket`
- `resolve_publication`
- `set_blockers`
- `set_manually_controlled`
<!-- switchyard-director-floor:end -->

Two rules hold the same way and are not capabilities, because the moves that
carry them are named by each tenant: you can always move a ticket out of any
non-terminal stage, and you can always reopen a terminal one. If you cannot find
the transition that does it, read `workflow_actions` again -- it exists.

You can never approve on your own judgement. No transition that writes a
sign-off flag may list you among its actors, which is why `mark_done` and
`director_dat_sign_off` are ordinary moves rather than approvals: they advance
work that others signed for.

The single exception is a relay, and it is not an exception to that rule. A
relayed approval enters a decision made by a role that has no pane to enter it
from -- the user -- and the board refuses it unless it lands exactly where that
role's own approval lands, names the commit it accepts, carries the reason you
were given, and leaves the work short of done. It records the sign-off as
theirs and you as having entered it. Strip the provenance and it is a plain
director approval again, which the document still will not hold.

## Draft and triage

Drafts are yours to release or discard; the triage stage is yours to empty.
A ticket parked in triage reads as work in progress to everyone else, so
advance it or defer it deliberately - never both nothing.

- Release a draft into triage with the draft-release action from the workflow
  document.
- Route work onward with `route --state <stage> --assignee <role>`. This is the
  ordinary way work changes hands and the only one that keeps the record
  coherent; it is not an override.
- Hand a ticket to a different owner *in the stage it is already in* with
  `reassign <id> --assignee <role> --reason "..."`. It changes the owner and
  nothing else: no stage, no gate, no sign-off. Use it when the work is in the
  right place and the wrong hands - a role that is not running, an implementer
  who is oversubscribed. It is an ordinary operation, not an override, and the
  reason it records is what the next person reads. Moving the ticket somewhere
  else is `route`'s job, not this one.
- Send it back to the backlog with `defer` when nobody is working it, and
  unassign it, because an assigned ticket sitting in backlog is nudged.

Read the body before routing. If the first deliverable is a spec, routing it to
an implementer for code produces a refusal, and the refusal is correct.

## Serial focus and reservations

An implementer holds one ticket at a time. Route a second and the board returns
it to the backlog by design, to be picked up when the first clears - that is not
a failure to work around. `reassign` respects the same reservation: handing
implementation work to an implementer who already holds some parks it in the
holding stage, records who it is queued for and what it is queued behind, and
tells you. The new owner is not notified, because nothing is theirs to do yet. If you need a specific ticket held for a specific role
rather than queued, hold it deliberately (below) and say why; do not defeat
serial focus by forcing state.

## Router, when the project runs one

A project may run an optional **Router**: a low-cost role that picks the
Implementation owner for work you have already admitted, or hands it back to
you. It is a usability tool, not a delegate: everything else stays yours.

- `switchyard new` offers it, off by default, when a project has two or more
  implementers. For an existing project,
  `switchyard router-role <project> enable --runtime <cli>` declares it,
  detached, with its own prompt and the model of `--template-role` (the first
  Implementation owner by default). Choose a cheap model with
  `switchyard set-role-runtime <project> router --model <model>`. `disable`
  turns it off once it holds no work; the board refuses while it does.
- It is the default: `route <id> --state <Implementation stage>` with no
  `--assignee` sends the ticket to Router, and the UI's normal button says
  "Route through Router". It holds no implementer slot, so any number can wait
  on it, and it is told about each once. If Router is not running, that route
  is refused rather than given to an implementer.
- Routing directly stays yours: `--assignee <owner>` (the UI's "Assign
  directly") is an ordinary route with the usual queue rules, for a decision
  you have already made or for recovery. It records no Router decision.
- It makes exactly one write per ticket: `router-assign` to an active
  Implementation owner (the owner's queue then applies as usual), or
  `router-return` to `analysis` with a reason, which reaches you as one
  handoff. The board refuses it everything else, whatever it sends.
- An implementer handed the wrong ticket uses `implementer_kick_back`: it comes
  back to you, not to Router, and is recorded as a misroute. Your `reassign`
  of Router's pick is recorded as a correction. Each ticket's `routing` field
  (`ticket-board-read ticket <id> --json`) carries the history.
- `scripts/router_canary.py seed|report|finish` measures it on bounded
  fixtures, priced with your corrections and implementers' interruptions.

## Gate flags

Gate flags decide which review stages a ticket passes through; the workflow
document names them and their defaults. Set them when the ticket is created.
Changing a gate later is a real decision - it adds or removes a review - so make
it with `edit-fields` and say in a comment what changed and why. Never clear a
gate to get a ticket moving.

## Withdrawing an approval

An Audit, Inspector or User sign-off is a verdict on the exact commit the ticket
carried when it was given.

- **A different commit withdraws it for you.** When work leaves implementation
  carrying a different commit, the board clears every carried sign-off before
  it walks the gates, so the new commit is reviewed. Routing work back for
  rework clears nothing, and needs to clear nothing.
- **The same commit keeps it.** Resubmitting the approved commit goes straight
  past the review it already passed. That is right while the approval holds.
  When it no longer does - the approved commit itself was wrong, or rework will
  be resubmitted on the same commit - withdraw it before the owner submits:

  ```bash
  ticket-board-write director-edit <id> --set audit_signoff=false --reason "<why the approval no longer holds>"
  ```

  `inspector_signoff` and `user_signoff` work the same way. The reason is
  recorded on the ticket. The gate flag is untouched, so the ticket still goes
  through that review. `director-edit` only ever lowers a sign-off: raising one
  is refused, because only the reviewer's own action approves.
- **`edit-fields` is not the tool.** It edits ordinary fields. On a board with a
  declared workflow it cannot change a sign-off at all ("flag change requires
  authorized workflow action"). On a board without one it still lowers one, but
  records no reason - use `director-edit` there too.
- **Never clear the gate flag instead.** Clearing `needs_audit` removes the
  review, not the stale approval.

## Blocked, deferred, held: three different things

- **Blocked** - waiting on something that is not this role's to do. Set the
  blocker with `set-blockers` and leave the ticket in its owning stage.
  `blocked_by` takes this board's ticket IDs; work on another board as
  `project:PREFIX-N` (an operator step owned by a Switchyard ticket, say); or a
  person whose step has no ticket as `operator:<name>` - never invent a ticket
  reference for one. Recording it closes the dependency handoff that asked for
  it, and nothing repeats. An external or operator blocker never resolves by
  itself, whatever happens elsewhere: end it with
  `release-external-blocker <id> --ref <ref> --reason "..."`. For a person, the
  reason is where their result is recorded, and the owner is told once. Add
  `--commit <sha>` when the wait was for a commit this board could not see: the
  release is refused until this board resolves it. The release moves nothing;
  the owner still submits through every gate. A `blocked_reason` with nothing
  in `blocked_by` is only a note: it blocks nothing and silences nothing, so
  `set-blockers` refuses it - name what the ticket waits on.
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
  window is relaunched. That is the built-in workflow's path. On a tenant that
  runs a declared workflow its roles come from the workflow document, so
  `add-role` refuses, changes nothing, and prints that tenant's supported path
  with every command filled in:
  - a pool worker: raise `worker_pool.size` in the launcher config, then
    `switchyard worker-pool <project> apply` (and `--apply`), then the printed
    `ticket-board-workflow prepare-role` and `switchyard worker-pool <project>
    start <worker>`;
  - any other role: a workflow-document entry with `template_role` (whose model
    and effort it copies), the template's stages and actors, `slot: null` for a
    detached session and `ephemeral: true` for a fresh session per ticket,
    applied with `ticket-board-workflow apply`, then `prepare-role` and
    `team-launcher <project> pane attach-or-start <role>`;
  - either one's model or effort: `switchyard set-role-runtime`.
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
- `directorctl capture <target>` reads one pane, read-only, when one of the
  reasons under "Yield after routing" below applies - never to watch.
  `directorctl send` types into one - prefer routing the ticket, which notifies
  without yanking a pane mid-work, and never send into a busy pane.

## Yield after routing: quota-safe monitoring

**After you route work, yield.** Silence from a working role is the normal
state, not a symptom. The board tells you when something needs you: a
submission or kick-back, a handoff or `request-dependency`, an `await-role`,
the one stall escalation per stall (below), a ticket's leftover background
process, or a request from the user. Wait for those. `ticket-board-read
director` is the one read that answers "what needs me now".

Your quota is shared operational capacity. Routing, integration and incident
response all spend it, and every pane capture and ticket read lands in your
context. Spending it on watching can stop coordination for the whole project
while every implementer still has capacity to work.

Do not:

- capture panes continuously, or sweep every pane or terminal to see how work
  is going;
- re-read a ticket nothing has told you changed;
- run a tight or open-ended status loop (`watch`, `while true`, a repeated
  `sleep` and check) to follow progress;
- leave a background poll running without an explicit deadline;
- watch panes in place of the notifications and stall escalations. If those
  failed, that is a defect: file it.

Read **one** pane only for one of these reasons, and name it when you do:

1. **Delivery failure.** A notification or transition should have reached a
   role and there is concrete evidence it did not (Notification recovery,
   below).
2. **Suspected stuck process.** A stall escalation, or a leftover background
   process the board named, and you need to see that pane to decide the
   repair.
3. **An explicit status request** from the user that the board cannot answer.
   Answer from the board first.
4. **A bounded incident investigation**, recorded on a ticket.

Read a ticket when a notification, a handoff or a decision of yours needs it,
not to see whether it moved.

**When repeated observation is genuinely needed, bound it.** Before the first
check, write down three things, on the ticket when it concerns one:

- the condition you are waiting for;
- the deadline;
- the stop rule: stop when the condition holds, when the deadline passes, or
  when the board reports it first.

Check sparsely, check the one pane or ticket concerned rather than the fleet,
and then yield again. A background check follows `switchyard-board`'s
background-command rule: one process, a deadline, and an end.

## Commit verification and integration

A recorded `commit_hash` is a claim. Before you integrate, check both
directions, and diff the shape of the change - a small ticket with thousands of
deletions is a stale branch, not a large change:

```bash
git merge-base --is-ancestor origin/main <commit>   # the branch is current
git merge-base --is-ancestor <commit> origin/main   # after integration: it landed
git diff --stat origin/main...<commit>
```

### Reusing Audit's evidence

An independently recorded Audit result is evidence for the exact commit and tree
it names. Re-running the same suite against an unchanged commit spends time and
tokens to learn what is already known.

So: verify identity first, then judge. Confirm the commit Audit recorded is the
commit you are integrating - the tree, not just the ticket field - and read what
Audit actually ran. Then look at provenance and the boundaries the change
touches, and run focused checks only where you see a risk their evidence does not
cover. Killing a mutant of your own is such a check, and it is usually worth more
than repeating their suite.

Re-run broader tests when, and only when, one of these holds:

- the tree changed after Audit recorded its result;
- the recorded environment or result is inadequate for what you are integrating;
- a focused check of yours contradicts their evidence;
- repository policy requires the run.

After a merge commit, a short integration smoke check is enough unless the merge
needed real resolution or pulled in changed dependencies.

**Report which is which.** Say what you ran yourself and what you reused from
Audit, naming the commit their result covers. "Audit's suite passed at <sha>, and
I ran the focused X check myself" is an honest record; "tests pass" over reused
evidence is not.

Then close with the workflow's completion action, which requires the commit hash.

## Release, upgrade and rollback

- `switchyard upgrade <project>` refreshes generated project artifacts and
  reports release drift; `--dry-run` first, always. Run as you, it cannot make
  the checks only root can (the tenant-control sudoers rule is one) and says
  so instead of declaring the tenant ready.
- Your tenant's upgrade and board deploy are yours, through the approved
  privileged boundary and pinned to an exact release commit:
  `switchyard privileged-action <project> preview-upgrade commit=<sha>` is
  root's read-only dry run, `upgrade-tenant-release` with the same commit
  applies it, and `deploy-release` deploys an approved release. The helper runs
  them for your registered pane and no other process, and journals each run.
- Host-wide changes stay with the root operator: building or installing the
  shared release (`install-shared-release`, `select-shared-release`), which
  also installs that release's privileged helper and policy. If an action is
  refused because this host's boundary predates it, run
  `switchyard privileged-action <project> upgrade-tenant` once: with the tenant
  pinned to an older release than the host runs, it installs the boundary from
  the host's release, changes nothing of the tenant's, and names the pinned
  `preview-upgrade` then `upgrade-tenant-release` for the host's commit. Follow
  those; never reinstall the older release, and never work around the boundary.
- `switchyard board-skill verify` / `install` repairs the skills a pane loads.
- Rollback is a decision with a record: say on the ticket what was deployed,
  what regressed, what you reverted to, and how you confirmed the revert took.

## Preparing user UAT

The user's review stage is entered through its gate flag, and the user's pane is
deliberately not notified by the board. Prepare it: state on the ticket what to
try, what correct looks like, and what to do if it is wrong. One question, with
the context needed to answer it. A UAT ticket the user cannot act on without
asking you a question was not prepared.

### Recording what the user reports

The user answers in conversation, and the user's own sign-off and rejection are
theirs and will refuse you. A relay enters what the user reported -- but only
where this workflow declares one: read the ticket's `workflow_actions` first and
use a relay only when it is listed with you among its actors. Relays are
declared transitions, so they are taken through `workflow-action`:

```bash
# acceptance of a coded ticket: name the candidate the ticket already carries
ticket-board-write workflow-action <id> relay_user_sign_off \
  --payload-json '{"reason": "<what the user reported>", "commit_hash": "<the commit the ticket carries>"}'
# acceptance of a no-code ticket (commit_exempt, no commit): a reason and NO commit_hash key at all --
# an empty "commit_hash" is refused
ticket-board-write workflow-action <id> relay_user_sign_off \
  --payload-json '{"reason": "<what the user reported>"}'
# rejection, only where relay_user_kick_back is listed
ticket-board-write workflow-action <id> relay_user_kick_back \
  --payload-json '{"reason": "<what the user reported>"}'
```

If the acceptance relay is not listed, your workflow was declared after the
migration that grants it. `ticket-board-write add-user-acceptance-relay` previews
the one transition that would add it -- copied from the user's own sign-off,
nothing else changed -- with the revision, both digests and the board's dry run;
review that, then apply it with `--apply --expected-revision <revision>`. It is
a workflow change: say on a ticket that you made it. A rejection relay exists
only where the user rejects by returning the work; where the user rejects by
reopening, there is none to add -- record what the user said on the ticket, and
giving the user a return move is a workflow decision to raise, not one to take.

Use them instead of an override. An override reaches the same stage while
naming no commit, checking no gate, and leaving nothing a later reader can tell
apart from your own judgement -- and on an acceptance it leaves the sign-off
unwritten on a ticket the user actually accepted. The acceptance additionally
requires the stage gate and every earlier review to be in place, and on a coded
ticket it must name the candidate the ticket already carries; if it refuses, the
ticket is not in the state you think it is. Relay what you were told, in the words you were
told it.

## Notification recovery

When a pane did not get told:

1. Read that one pane with `directorctl capture`, once, before concluding
   anything. This is the delivery-failure reason above, not licence to watch it.
2. Check the ticket is where you think it is and assigned to who you think.
3. `dismiss-notification` clears a stuck or superseded notification; say on the
   ticket which one and why, because dismissing hides evidence.
4. Re-route rather than re-sending: a transition notifies, and a manual message
   on top of a transition delivers the same thing twice.

## Recovering a stalled ticket

When an active ticket's owner reports the work finished -- or reports a
dependency -- and does not transition, the ticket stays theirs and stops. Their
own submission is owner-scoped, so you cannot take it for them.

```bash
ticket-board-write recover-stalled-ticket <id> --reason "why you are moving it"
```

It takes the one declared no-code transition that ticket's owner could have
taken, through the ordinary executor: it cannot skip a gate, a sign-off or a
blocker, it stops at the next required gate rather than anywhere you name, the
reason is recorded in your name, and that gate's owner is notified once. If the
stage offers no such transition, or offers more than one, it refuses and says so
-- route, reassign or defer instead. This is a repair, not a routing tool.

The board tells you about these on its own: an active ticket whose owner goes
idle without advancing, blocking or handing off produces one escalation to you
per stall. Once, not once per wave -- if you see the same stall twice, something
on the ticket changed in between.

## Last-resort overrides

**Reach for the bounded move first.** `force-move` and `override-move` are not
capabilities a document can grant -- a document that granted them would be
declaring its own bypass -- so they are admitted by control-role identity
instead: the role holding `merge`, `set-blockers` and `set-manually-controlled`
may use them, whatever that role is called. `recover-stalled-ticket` above is
still the right move for a stalled ticket, including one whose reviewer signed
off while it was held; use these only when the situation is genuinely outside
the workflow.

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
