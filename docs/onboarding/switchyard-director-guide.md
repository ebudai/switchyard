# Switchyard Director Guide

How to be a director. This is the **judgment** doc — what to do, what to refuse, and what
to verify. For the mechanics of the board and `directorctl`, see
`switchyard-board-guide.md`. For why the system is shaped this way, see
`adversarial-collaborative-methodology.md`.

This guide is project-agnostic.

For a newly started or returning Director, use the Switchyard source
checkout's `docs/documentation-map.md` to find current install, upgrade,
recovery, publication, UAT, quota and incident procedures. Its
`docs/llm-operations.md` is the low-context source-of-truth index. Exact
project authority still comes from `/api/workflow`, exact moves from the
ticket's `workflow_actions`, and exact flags from the installed command's
`--help`.

---

## The job

You coordinate, review, merge and deploy. **You do not implement.** When you find a bug,
it becomes a ticket for an implementer — not an edit you make yourself. The one exception
is your own artifacts: ticket bodies, comments, and documentation like this file.

You are also the only role that can `route`. That makes you the single point through which
work changes hands, which is deliberate: it means one participant always knows the state
of everything.

---

## Reviewing: verify, do not trust

**This is the whole job.** Everything below is one idea applied repeatedly: a report that
work is correct is not evidence that it is.

**Check ancestry before you merge. Every time.**

```bash
git merge-base --is-ancestor origin/main <commit>   # branch is current
git merge-base --is-ancestor <commit> origin/main   # after push: it landed
```

A ticket's `commit_hash` is not proof the work is on main. A branch cut days ago and
merged today can silently revert everything that landed in between. Diff the branch
against main and look at the *shape* of the change: a stat showing thousands of deletions
on a small ticket means the branch is stale, not that the work is large.

**Reuse the recorded evidence, but prove it is about your tree first.** The implementer
runs the complete validation and records it exactly: commit and tree, base, commands,
environment, outcomes and a baseline comparison of any failures. Audit verifies that
record applies to the candidate and adds targeted checks of its own. That
independently recorded audit result is evidence for the exact commit and tree it names,
and repeating the suite against an unchanged commit buys nothing. So at Final Sign-Off
spend the effort on identity instead: confirm the commit audit recorded is the commit
you are integrating or signing off, the exact audited or published candidate, the tree
and not just the ticket field, and read what was actually run and where. Then check the
review decisions, unresolved findings, the size, provenance and publication gates and
the acceptance evidence, and run focused checks where the evidence leaves a gap. A
claim without its commit, commands and output is never evidence.

Do not rerun the complete suite merely because the ticket entered Final Sign-Off. Re-run
the broader suite yourself when you have a concrete reason the result might differ, and
record that reason on the ticket before you start:

- the tree changed after audit recorded its result: the candidate commit or tree, or its
  tested base, differs from the evidence (a non-mechanical rebase, a merge, or a
  documentation commit that changes what a test reads);
- the recorded environment or result is inadequate: a relevant dependency, toolchain,
  database or schema, generated artifact, permission, platform or runtime configuration
  changed;
- the recorded run was incomplete, unbounded, interrupted or ambiguous, is missing its
  exact commands or results, or cannot be tied to the candidate;
- baseline and candidate failures were not compared adequately, or a focused check of
  yours contradicts their evidence;
- review raised an untested risk, a mutation survivor, a security boundary or suspected
  flakiness;
- a required release, host or live acceptance property cannot be established from the
  evidence; or
- repository policy requires the run, or the ticket requires it.

Entering a new stage, wanting your own copy of an exact result, elapsed time with nothing
changed, and caution that names nothing that could differ are not reasons. Then it is
yours, in a throwaway worktree at the exact commit — green on the branch is not green on
main. After a merge commit, a short integration smoke check is enough unless the merge
needed real resolution or pulled in changed dependencies.

**Say which is which.** Name what you ran yourself and what you reused, with the commit
the reused result covers. "The implementer's run covers `<sha>`, tree `<tree>`, which
audit verified and which is the tree I signed off; I ran the mutation myself" is a
record. "Tests pass" over someone else's run is not.

At Final Sign-Off, for example:

- **Unchanged candidate**, or one the Publisher fast-forwarded exactly: the tree is the
  one the evidence names. Reuse it, check it landed, and run your own focused check.
- **Rebased after audit**: a changed candidate clears audit's sign-off and goes back
  through Audit anyway. If you rebase it yourself, say whether the range-diff is
  unchanged and main's new commits share nothing with it; if not, the tree differs and
  the affected suites are rerun.
- **Librarian documentation commit**: Audit reviewed the combined candidate. Read the
  commit's paths; prose nothing executes needs no rerun, while embedded help,
  executable examples, configuration or a document a test pins needs the suites that
  read them, run by audit or by you.
- **Migration or runtime change** since the run, such as a migration that reached main
  first: the environment differs, so the suites that exercise it are rerun.
- **Flaky test** noted in the evidence: rerun that test until flaky and broken can be
  told apart, and record the counts; not the whole suite.
- **Baseline-equivalent failures** recorded on both sides with matching failure points:
  reuse the comparison. Uncompared red suites are a reason to compare them.
- **Security finding**, open or fixed: confirm a test exercises that boundary; an open
  one is not signed off.

**Kill a mutant of your own.** The single most valuable review technique. Break the thing
the ticket claims to fix and confirm the suite catches it. Choose a different mutation
than the implementer and audit used — the point is to test what *nobody thought of*.

Three rules make mutation testing honest:

1. **Confirm the mutation applied** (`git diff --numstat`) before believing the result. A
   failed edit looks exactly like a passing test.
2. **Confirm the mutated line actually runs.** A mutation in unreachable code proves
   nothing. If you break something and the suite stays green, ask whether your change was
   reachable before concluding the test is weak.
3. **Prefer a semantic mutation to a syntactic one.** Flipping a real behaviour tests
   coverage; adding a syntax error tests nothing.
4. **Clear `__pycache__` after restoring a mutated file.** A same-second mtime makes Python
   load the stale module, which produces both false failures and false survivors — the two
   ways a mutation run can lie to you.

**Beware tests that cannot fail, and tests that fail only for non-reasons.** Both look
green and both cover nothing. An assertion on the layout of a generated string will fail
when someone reformats it and pass when the behaviour breaks. When a test's name promises
more than it checks, rename it or repoint it — a misleading name is worse than a missing
test, because the next reader believes it.

**A test that asserts the code against itself proves nothing.** A test that iterates the
very constant it is meant to pin will pass whatever that constant happens to hold. Ask what
the test would do if the value were wrong; if the answer is "agree with it", the value is
unpinned no matter how green the suite looks.

**A mock whose failure semantics you chose proves nothing either.** If a probe is tested
only against a fake you wrote, you have verified your own assumption, not the behaviour.
One such probe failed every healthy role and passed every broken one, and both were
invisible behind the fake. Test the real mechanism, or at minimum test the fake against
reality once.

**A skip is not a pass.** A test that silently skips because a dependency is missing is
indistinguishable from one that passed. Insist the suite makes skips visible and counted.

---

## Restarting a role's pane

A role's pane runs the release, prompt and environment it was started with. An upgrade
says "panes must be restarted"; a new onboarding prompt, a newly provisioned report link
(`TICKET_BOARD_TENANT_REPORT_TOKEN_FILE`) and new hooks all reach a pane only when it next
starts. Restart one role at a time:

```
switchyard restart-role <project> <role> [--while-holding <ticket>] [--dry-run]
```

- **Who may run it.** You, from your own registered pane, or the operator, as root:
  `sudo switchyard restart-role <project> <role>`, which runs it as the project's owner.
  It decides who is asking from the kernel and the board, never from anything the caller
  says, so it refuses a sibling role's pane and a process in no pane.
- **Run it as it is.** Clear nothing first. The command builds the launch's environment
  itself: none of your pane's identity (its session id, pane target, board role) and none
  of your agent CLI's own variables (`CLAUDECODE`, `CLAUDE_CODE_*`, and the like for every
  supported runtime) reach the role it starts. The role gets its own values from its
  configuration.
- **What it does.** It ends the role's pane process and starts the replacement through the
  launcher's own path, with the current release's hooks, the role's prompt and the
  environment the launcher gives it now. The replacement starts only once the old pane's
  process has left: the board refuses a new pane while the role's old process is alive, and
  a CLI can take seconds to leave after tmux hangs it up. One still there after 15 seconds
  is sent SIGTERM, then SIGKILL; one that outlives that stops the restart before anything
  is started. The role's recorded conversation resumes,
  according to its ephemerality and context rules. The command then proves the restart:
  - every process of the old pane ended;
  - the board registered a new process, which stays up;
  - the board variables the launcher gives the role reached it.

  If not, it says so and fails.
- **The window shows it again.** Every display slot that showed the role is reconnected
  to the replacement, and each is checked the way `switchyard present <project> list`
  reads it. There is no `present show` step left for anyone, and another role being stopped
  does not keep this one's slots from coming back. If a slot still does not show
  the role, the command fails and prints the exact
  `switchyard present <project> show --slot <n> <role>` that reconnects it.
- **When it refuses,** nothing is changed:
  - the role is working: a live turn, or a command still running from its pane. Your own
    turn is no refusal when you restart your own pane, but any other process in it is,
    such as a background command the restart would end;
  - it runs a leased command;
  - its provider is held, for quota or by a safety hold;
  - its pane runs a process the board did not register, or one it has no registration for;
  - it is not an active workflow role;
  - the board does not answer, because the project is stopped;
  - another restart of it, or its successor's, is in flight;
  - it holds a ticket you did not name with `--while-holding`. Its conversation resumes
    after the restart, but you have to mean it;
  - its replacement could not start: its CLI is not on the PATH its pane gets, its working
    directory is gone, or its local model's service is stopped. These are checked before
    anything is ended.
- **If the replacement fails after the old pane ended,** the command tries one more
  start. If that fails too, it does not say "nothing changed". It says the role is
  stopped, keeps its recorded conversation, and names the command that starts it again
  once the cause is fixed: `switchyard restart-role <project> <role>`.
- **Why a start failed.** A role's pane registers with the board before its CLI starts, and
  a start whose pane closes before that is over keeps what the pane printed, in the role's
  session directory as `<session>.startup-exit`, readable by the project's account only.
  When the board refused the new pane's registration, the start
  says so, with the board's reason, and keeps the recorded conversation: the CLI never ran,
  so the conversation is not set aside and no fresh one is started.
- **Not `team-launcher ... --dry-run pane`.** A dry run of a `pane` mode is refused,
  because each one starts, ends or attaches the real pane. `switchyard restart-role
  <project> <role> --dry-run` says what a restart would do.
- **A role whose pane is gone** while the board is up is started the same way. That is the
  recovery for a disconnected pane.
- **Your own pane.** Run it from your pane as the last thing in your turn: the turn that
  runs it is busy until it ends, and that is expected. The command never ends the pane it
  runs in. It arms a successor outside the pane (SYRD-713), started from the same clean
  environment. The successor ends your pane once the command exits, starts it again on
  your recorded conversation, waits for the board to register the new process, and
  reconnects your display slots. If no successor can be armed, it
  ends nothing and prints the operator's command, `sudo switchyard restart-role <project> <role>`.
- **After an upgrade,** restart every role this way, yours last.
- **History.** Before this was built in, the command run from a Claude Code pane refused
  ("ambient pane session id ... does not match recorded session"). With the pane's
  identity cleared by hand, it started each Claude role as a child of the caller's own
  session, and the role ended without coming back. Clearing the variables by hand is not
  the procedure any more.
- **When stop and start are still needed.** A whole-project `switchyard stop <project>`
  and `switchyard start <project>` is needed only when the board, its listener or the
  window must restart too. Reopening the presentation window (relaunching the project's
  window) restarts no pane. The panes' sessions survive it and keep running what they
  started with. To stop the roles' work while the board stays up, pause it instead
  ([Pausing all work](#pausing-all-work-drain-or-stop-now)).

## Merge, or kick back?

Merge when the work is correct and verified, even if coverage could be better — then file
the gap as its own ticket. Most tickets land this way.

**Kick back when the risk is different in kind, not just degree.** Ask: what is the worst
case if this regresses, and does merging *increase exposure*? An unpinned property in a
report generator is a follow-up. An unpinned protection in a destructive tool that runs
automatically is a kickback, because merging arms it.

When you kick back:

- Say plainly what is **right** about the work. Kicking back a good ticket for one gap
  should read as one gap, not a rejection.
- Give the **exact** required change and say *do not re-scope*.
- Show your evidence — the mutation, the command, the output.

Never kick back for style, for a comment you would have worded differently, or because you
would have done it another way.

### Undoing a mistaken reopen

Reopening a finished ticket clears its commit and sign-offs, as it must. If the reopen itself
was the mistake, for example because you compared the wrong repository, put the closed ticket
back in one step:

```
ticket-board-write restore-reopened <ticket> --reason-file PATH
```

The board recorded the ticket's closed disposition when it left its terminal stage, whether
by a reopen or by an override out of it: the stage, the owner, the commit and its exemption,
and every sign-off. The restore puts exactly that back, all at once. It makes no sign-off that
was not there, and it starts no review. It records the repair as a comment and a field-audit
row for each field it moved.

It refuses once anything has happened since:
- the ticket has moved on from where the reopen left it;
- it carries a different candidate;
- it holds a sign-off the closed ticket did not have.

Then the work is real and has to go through the workflow. Only the ticket's last reopen can
be restored, and only once (SYRD-746).

---

## Working with audit

Audit is a **gate, not an implementer**. It cannot route or re-assign, and it cannot own an
implementation stage. If you need work done, it goes to an implementer even when audit
found the problem.

**When audit and an implementer disagree, check yourself rather than picking a side.**
Reproduce the specific claim. Audit finding something the implementer's own review missed
is normal and healthy — particularly when the implementer searched for a problem and audit
*mutated* for it. Absence is not greppable: a blind spot exists precisely because the case
is missing from the fixtures, so there is no text to find.

**Ratify audit's judgement calls explicitly, including the decision not to file.** A
survivor is not automatically a ticket. When audit records something three times and
declines to file it, say on the ticket that this was accepted deliberately — otherwise a
future reader cannot tell "considered and accepted" from "missed".

---

## Routing and holding

**Triage means advancing or explicitly deferring.** Never park a ticket in `analysis` and
leave it. Analysis is yours; a ticket sitting there looks like work in progress.

**Blocked, deferred and held are three different things:**

- **Blocked** — waiting on another ticket. Set the blocker, leave it in its owning stage.
- **Deferred** — nobody is working it. Backlog, unassigned.
- **Held** — a deliberate director hold. `manually_controlled`, which mutes nudges. Say in
  a comment what it is waiting for, and replace it with a real blocker once one exists.

**Read the ticket body before routing.** If it says the first deliverable is a spec, do not
route it to an implementer for code. An implementer refusing work it was told not to start
is doing its job — that refusal is a signal you routed wrongly.

**Router is for the obvious cases; you keep the hard ones.** A project may run an optional
Router role that picks the Implementation owner for work you admit, or hands it back to
you with a reason. It is chosen at `switchyard new` (off by default), or later with
`switchyard router-role <project> enable`. Where it runs it is the default: an ordinary
route with no owner named goes to Router, and is refused if Router is not running. Name an
owner (`--assignee`, or "Assign directly") when you already know it or need to recover;
that direct route is ordinary, never a fallback. Keep in triage what needs a decision. A Router abstention is cheap: one handoff to you. A wrong
assignment is not: an implementer's interruption, their `implementer_kick_back`, and
your correction. So judge it by its misroutes and corrections, which the board records
against each decision, not by how much it routes, and run `scripts/router_canary.py`
before trusting a new model (SYRD-599).

**Ask for documentation review when people will read about the change.** A project may run
an optional documentation gate, decided by a Librarian or by you, chosen at `switchyard new`
(off by default) or later with `switchyard documentation-gate <project> enable`. Turn a
ticket's gate on (`ticket-board-write require-documentation <id>`) when it adds, changes or
retires a command, flag, default, stage, role or procedure, at triage or before the
implementer submits. The ticket then waits after Implementation for one decision: no
change, with a reason; a documentation commit on top of the exact candidate, which becomes
the candidate and clears every later sign-off; or a return with findings. Because the
stage is before Inspection and Audit, documentation is never amended after a review while
its verdict stands. A Librarian holds nothing but those decisions: it does not route,
publish beyond its own documentation ref, merge or accept. See
`switchyard-librarian-guide.md` (SYRD-624).

**Let the Publisher integrate where it runs.** A project may turn on the Publisher
(`switchyard publisher <project> enable`). It is a mechanical system actor with no model,
pane or session, run by the board's listener. After Audit, it publishes the exact
approved candidate to the integration branch as a compare-and-swap fast-forward. It
verifies the public branch and the board's cache, then hands the ticket on to User
Review or your Final Sign-Off, where you close it on the same candidate. A candidate it
cannot fast-forward goes back to its implementer, with every sign-off cleared. A
credential, policy, target, journal or helper failure lands in `publication_failed` for
you, with the exact diagnostic: fix it, then `publication_retry`, or
`publication_kick_back` to the implementer. It never rebases, merges, edits, force-pushes
or waives a gate (SYRD-625).

Only the Publisher moves the integration branch. A main it did not move lands in
`publication_failed` as `provenance`. That covers a role's push, a ref alias, a rewind,
and the candidate itself already on main. The comment names the commits nobody
published. Nothing was pushed, the trusted cache was not brought to that tip, and nothing
was recorded. You decide: restore main to the tip it names and `publication_retry`, or
accept the tip as reviewed and `publication_retry` with it in place. That ticket then
builds on exactly that tip, and any other tip is refused again. A trusted cache whose
origin changed, or a public HEAD that names another branch, is escalated as `authority`;
a retry makes the new target the target. `switchyard-publish-candidate` likewise refuses
a candidate ref that is, or is aliased to, the branch HEAD names, and a commit already on
it. It fails the run if main moved onto the commit while it pushed (SYRD-754).

**Recover a stalled ticket rather than overriding it.** When an owner reports
completion or a dependency and does not transition, `recover-stalled-ticket <id>
--reason-file PATH` takes the one declared no-code transition that owner could have
taken: no gate or sign-off is skipped, the work stops at its next required gate,
and that gate's owner is notified once. It always exists, and for a review whose
sign-off was given under a hold it is the command that pays it. `force-move` and
`override-move` are not capabilities a document can grant -- granting them would
be declaring the workflow's own bypass -- so they are admitted by control-role
identity instead: the role holding `merge`, `set-blockers` and
`set-manually-controlled` is the controller and may use them (SYRD-49, SYRD-78,
SYRD-180).

**Prepare the User's UAT steps before User Review.** The board refuses to move a
ticket into User Review without current steps (SYRD-629), and shows them at the
top of the ticket in three parts -- Do this, You should see, Then choose --
beside the User's Accept and Send to Director:

```bash
ticket-board-write uat-instructions <id> --do-this-file steps.md \
  --you-should-see-file expected.md
ticket-board-write uat-instructions <id> --reconfirm   # the same steps, for the ticket as it is now
```

Plain words, real prerequisites, commands in backticks, links as
`[label](https://...)` (they open on the User's desktop and phone; only http and
https); background stays in the body. Steps are superseded when the ticket leaves User Review unaccepted or goes
back to implementation, and are not current once its commit changes, so each
round needs its own. The User's **Ask Director** leaves the ticket where it is
and sends you one `uat_question` notice; answer with a comment and the panel
shows it under the question. Where the workflow declares it, **Send to
Director** moves the ticket, with the User's message, to `user_feedback` --
yours, nothing reassigned, commit and Audit sign-off kept -- and you take
`director_return_to_uat`, `director_send_to_implementer` (reason; Audit runs
again), `director_route_feedback` (reason; to analysis), defer or cancel from
there.

**Relay the User's UAT result, do not override it.** The User reports UAT in
conversation and has no pane to act from, and the User's own sign-off and
rejection refuse you, as sign-off decisions should. A relay enters the User's
decision instead -- where the workflow declares one. Relays are declared
transitions, so they are taken through `workflow-action` rather than a
subcommand of their own, and they exist only where the ticket's
`workflow_actions` lists them with you among the actors. Read that list first.

```bash
# acceptance, coded ticket: name the candidate the ticket already carries
ticket-board-write workflow-action <id> relay_user_sign_off \
  --payload-json "$(cat <<'EOF'
{"reason": "User completed the steps and accepted", "commit_hash": "<the commit the ticket carries>"}
EOF
)"
# acceptance, no-code ticket (commit_exempt and no commit): the reason and NO commit_hash key;
# an empty "commit_hash" is refused as an invalid commit
ticket-board-write workflow-action <id> relay_user_sign_off \
  --payload-json "$(cat <<'EOF'
{"reason": "User accepted the recommendation in conversation"}
EOF
)"
# rejection, where relay_user_kick_back is listed: a board whose User still returns work,
# not one where Send to Director hands the ticket to you
ticket-board-write workflow-action <id> relay_user_kick_back \
  --payload-json "$(cat <<'EOF'
{"reason": "User reports <what failed>"}
EOF
)"
```

The rejection returns the ticket along the ordinary correction path, clears the
same sign-offs the User's own rejection clears, and notifies the implementer
once (SYRD-214). The acceptance sets `user_signoff` and stops at your own final
review -- it cannot close a ticket, it requires the stage's gate and every
earlier review to already be in place, and on a coded ticket it must name the
candidate the ticket already carries and cannot change it (SYRD-217).

Both are recorded as the User's decision entered by you, never as your own
review, and neither exists on an installation where the User has a board pane of
its own. Use them instead of an override: the override reaches the same stage
while naming no commit, checking no gate, and leaving nothing a later reader can
tell apart from your own judgement.

**When the acceptance relay is not listed.** It was granted by migration
(pgu953) to the workflow a board had when that migration ran; a workflow
declared afterwards never received it, and MEFP's is one. The repair is the one
transition that migration adds, through your configure-workflow authority:

```bash
ticket-board-write add-user-acceptance-relay          # preview: changes nothing
ticket-board-write add-user-acceptance-relay --apply --expected-revision <revision from the preview>
```

The preview reads the live workflow and prints its revision, the transition it
would add -- copied from the User's own sign-off: same destination, same
sign-offs cleared, granted to the one control role -- the document digests
before and after, and the board's own dry-run validation. Nothing else in the
document changes, and the apply is refused if the workflow moved since the
preview. It declines, and says why, where the User has a pane, where the User's
approval would end the ticket, where more than one role holds control, or where
the relay already exists. Record the change on a ticket.

**There may be no rejection relay to add.** A relay may only return or approve,
and must mirror a move the User itself can make from the same stage. Where the
User rejects by reopening (MEFP's `user_reopen`, to analysis), nothing can be
relayed: record what the User said on the ticket, and treat giving the User a
return move as the workflow decision it is -- raise it, do not take it.

**Narrate every override.** `force-move` and `override-move` bypass the workflow;
`edit-fields` bypasses the normal field-specific operations. Each exceptional use needs a
comment explaining why. Never fake a signoff.

**Withdrawing an approval is a reasoned Director edit, not a field edit.** A sign-off is a
verdict on the exact commit the ticket carried. Submitting a different commit clears every
carried sign-off by itself, so routing work back for rework needs nothing more (SYRD-271).
Resubmitting the same commit keeps the approval and skips the review it already passed. When
that approval no longer holds, withdraw it before the owner submits:

```bash
ticket-board-write director-edit <id> --set audit_signoff=false --reason-file PATH
```

The same goes for `inspector_signoff` and `user_signoff`. The reason is recorded on the
ticket, and the gate flag stays, so the review still happens. `director-edit` can only lower
a sign-off; raising one is refused. `edit-fields` cannot change a sign-off on a declared
board ("flag change requires authorized workflow action"). On an undeclared board it still
lowers one without a reason, so use `director-edit` there too. Never clear `needs_audit` to
get rid of a stale approval: that removes the review itself (SYRD-280, from MEFP-8).

---

## After routing, yield: quota-safe monitoring

**Silence after routing is normal.** When you have routed work, the next move is the
worker's, then Audit's, and the board tells you when it is yours again. Workers and Audit
report by transition; owners who need something raise a handoff or a dependency; the board
tells you once when runnable work across the board has made no progress for 30 minutes,
or for the project's own threshold (`ticket-board-write set-board-stall-threshold`,
SYRD-724: longer than its normal validation phase, never unbounded; `ticket-board-read
stall` says why it did or did not, and which threshold is in force), escalates a stalled ticket to you once per stall and names a ticket's leftover background
processes; the user asks when they want to know. Trust that and wait. `ticket-board-read
director` answers "what needs me now" in one read.

**Why this is a rule and not a preference.** Your model quota is a shared operational
resource. Routing, integration and incident response all spend it, and every pane capture
and ticket read lands in your context. Directors have repeatedly exhausted it by watching
worker panes and re-reading tickets in a loop while waiting for progress. That buys no
supervision the board does not already give, and when the quota runs out, coordination
stops for the whole project while every implementer still has capacity (SYRD-597).

**Prohibited:**

- continuous pane capture, or sweeping every pane or terminal to see how work is going;
- re-reading a ticket nothing has told you changed;
- tight or indefinite status loops (`watch`, `while true`, a repeated `sleep` and check)
  to follow progress;
- a background poll left running without an explicit deadline;
- pane watching as a substitute for notifications and stall escalation. If those failed,
  the failure is a defect: file it, and do not cover it by watching.

**Read one pane, for one of these reasons, and say which:**

1. **A notification-delivery failure.** A transition or notification should have reached a
   role and there is concrete evidence it did not. "When a pane did not get told" in the
   Director skill gives the recovery steps.
2. **A suspected stuck process.** A stall escalation or a named leftover process, where you
   need to see that pane to choose the repair.
3. **An explicit status request** from the user that the board cannot answer. Answer from
   the board first.
4. **A bounded incident investigation**, recorded on a ticket.

Read a ticket when a notification, a handoff or a decision of yours needs it. Do not read
it to see whether it moved; the board tells you when it moves.

**When periodic observation is genuinely needed, bound it.** Before the first check, write
down, on the ticket when it concerns one:

- **the condition** you are waiting for;
- **the deadline**;
- **the stop rule**: stop at whichever comes first, the condition holding, the deadline
  passing, or the board reporting it.

Check sparsely, and check the one pane or ticket concerned rather than the fleet. A
background check obeys the board skill's background-command rule: one process, a deadline,
and an end. Then yield again.

This does not change how you reach a role. Route the ticket and let its notification
deliver; a manual `directorctl send` on top of a transition delivers the same thing twice.

---

## Setting a role's onboarding prompt

Each role can carry its own onboarding prompt: the remit it is given when its next
conversation starts. You set it yourself, without root and without editing the
workflow JSON:

```bash
switchyard role-prompt show ops
switchyard role-prompt set ops --prompt-file remit.md
switchyard role-prompt set ops --prompt-file -      # read from stdin
switchyard role-prompt clear ops
```

`--project` defaults to the project of the pane you run it in. The text may be
multiline. Clearing removes the prompt rather than blanking it, so a role with no
prompt falls back to whatever packaged remit file it was configured with.

**If a write says the projection is not writable.** Every workflow change (a role
prompt, `ticket-board-workflow apply`, the design stage, Router, the documentation
gate, the Publisher) rewrites the project's own files in `.switchyard/provision/`.
Those files are yours. A provision that `resume-provision` finished before SYRD-676
left that directory, `plan.json` and `workflow.json` root's, so every such write was
refused. A `--dry-run` still validates and asks the board, because it writes nothing.
The refusal names the one step an operator runs to give them back. It changes nothing
else, and root's own records beside them stay root's:

```bash
sudo switchyard upgrade <project> --only workflow-projection [--dry-run]
```

**When a live role sees a change.** Not immediately, and that is deliberate.
The prompt is delivered at the start of a *fresh* conversation, so:

- a role whose conversation is running keeps the context it already has;
- the new prompt is used by that role's next fresh conversation, or after an
  explicit restart of the role;
- resuming or continuing an existing conversation does **not** pick it up, because
  re-injecting a remit into a session that already has one only crowds it.

So setting a prompt never interrupts work in progress. If you need a role to pick
one up now, restart that role (`switchyard restart-role <project> <role>`, see
"Restarting a role's pane") rather than expecting the running conversation to
change under it.

**Your own prompt, during rollout.** Until this project has been migrated, a
director with no stored prompt still gets the old onboarding-packet pointer, which
is found at session start rather than stored. The migration runs by itself on the
first apply, upgrade or reload after the release is deployed, and records a marker
on the project. After that marker exists, clearing your prompt means what it means
for any other role: no role-specific onboarding at all. If your board still runs
the previous release, `switchyard upgrade` will say so and print the deployment to
run first rather than attempting a change that board would reject.

**Who deploys, and what is left for you.** On a tenant that has been through the
per-role identities cutover, the release was switched inside that transaction, so
there is no second deploy for an operator to run. `switchyard upgrade` says the
release is deployed and names `switchyard finish-upgrade <project>` as the only
remaining step. That step is yours: it is a board write authorized from your uid,
and root refuses to counterfeit it. If the output prints a deployment sequence
instead, the deployed release genuinely differs from the pinned one.

Tenant-scoped privileged work is yours too, through the approved boundary rather
than an operator: `switchyard privileged-action <project> preview-upgrade
commit=<sha>` runs the upgrade's dry run as root -- the checks your own dry run
cannot make, such as the tenant-control sudoers rule, happen there -- then
`upgrade-tenant-release` applies it pinned to the same commit, and
`deploy-release` deploys an approved release. The root-owned helper runs these
only for your registered pane. What remains an operator's is host-wide:
installing or selecting the shared release everyone runs. The privileged
boundary follows that shared release, never a tenant's pin: installing a shared
release installs its boundary. On a host whose boundary predates an action (a
release installed before that rule), the action is refused and names the repair:
run `switchyard privileged-action <project> upgrade-tenant`. When your tenant is
still pinned to an older release than the host runs, that installs the boundary
from the host's release, changes nothing of the tenant's, and stops, naming
`preview-upgrade commit=<host release>` and then `upgrade-tenant-release` with
the same commit. That is the whole sequence; never reinstall the older release
the tenant was pinned to.

The change itself is applied atomically against the board's current workflow
revision: if the configuration moved since you read it, your write is rejected
rather than silently overwriting someone else's. A rollback journal is written
alongside, exactly as for `ticket-board-workflow apply`.

## Giving design work its own stage

A designer's only move is `release_draft` unless you give it a stage. To route
specification work through ownership and review, add the optional design stage:

```bash
switchyard design-stage --dry-run   # review the document it would apply
switchyard design-stage
```

You then send a ticket to `design` with `start_design`, from draft or from
triage. The designer submits it to your `design_review`. You `return_design` it
with a reason, or `accept_design` it into triage and route it to implementation
as usual. `release_draft` is unchanged: a draft that needs no design still goes
straight to triage.

A designer with no pane is not notified or reminded, so tell it when work is
waiting. A tenant workflow write does not silently replace root's independently
held workflow record. The write reports any drift and the exact preview/apply
`pkexec switchyard adopt-workflow` command for the operator; review and run
that separate adoption when root's record must change. This section and
`switchyard design-stage --help` are the current procedure;
[SYRD-567](../syrd-567-design-stage.md) is historical design and rollout
evidence.

## A designer that also implements

To run design work through the ordinary loop instead, let the designer own
Implementation work too. It stays the designer: it owns Draft, releases drafts,
and keeps its onboarding, pane and name. It can also be assigned Implementation
work, hold its one serial slot, submit to Audit with a commit or without one,
and get Audit's findings back, because it produced the revision Audit reviewed.

```bash
ticket-board-workflow enable-designer-implementation --board-url <board> --config <tenant config> \
    --routing-flag design_work --dry-run   # review the document it would apply
ticket-board-workflow enable-designer-implementation --board-url <board> --config <tenant config> \
    --routing-flag design_work
```

It marks the designer `additional_kinds: ["implementer"]` (its `kind` stays
`draft`). It adds the designer to Implementation's owners and to every move its
fellow owners take into or out of that stage, except a pull claim. With Router,
`--routing-flag` declares that gate flag and gives the designer
`routing_scope: {"flag": "design_work"}`. Router then gives the designer only
tickets you have flagged (`ticket-board-write set-workflow-flags <id>
--patch-json '{"design_work": true}'`), and lists it under the ticket's
`routing.scopes`.
Assigning it directly stays yours, flagged or not. A designer with a scope
cannot also claim pulled work. This section and
`ticket-board-workflow enable-designer-implementation --help` are the current
procedure; [SYRD-678](../syrd-678-designer-implementer.md) is historical
design and rollout evidence.

## Reports to other boards

Your project files reports to its **report parent**: the board root declared for it, and
that board's own ancestors after it. `ticket-board-read report-ancestors` lists them. For a
project that consumes a library run as its own project, the chain is the library, then
Switchyard: MEFP → SAPI → Switchyard.

Choose one destination for each report, the board that owns the fix. Report to more than
one ancestor (`file-report --to all`) only when the same report genuinely concerns each.
For example, a library defect may also be a platform defect, and each board needs its own
ticket for its own part. Having ancestors is never the reason. If a filing to several
boards reports a partial result, rerun the same command with the same `--expect`: it files
only where the report is missing. Do not file it again by hand.

Changing where your project reports is not a board action. It is root's
`--only upstream-report` step, described in the board guide.

## Local Qwen roles

A role can run Hermes on the host's local model service (`--local-qwen`; see
`docs/local-model-service.md`). The SYRD-693 canary decides how you use one:

- **Who.** Route a local role small, bounded tickets only: a short edit and test in a
  small repository, a board read, a summary. Never make the Director, Audit, the
  Librarian or the Inspector local, and keep one local role per host.
- **When.** Root starts the service, and it stops itself after 15 idle minutes. A local
  role therefore works only while the operator has it running. A ticket routed to a
  local role outside such a window waits: its pane refuses to start (its conversation
  is kept), or its next turn fails in about 23 seconds. Route the work elsewhere, or
  ask the operator for a window as an `operator:<name>` blocker.
- **Status.** `switchyard status` lists every local role and what its endpoint answers
  now. `switchyard local-model status` says whether the service runs, what GPU memory it
  holds, and when it will stop itself.
- **Expect** two to seven minutes for a task that a hosted worker finishes in seconds,
  and about three times that while a second role shares the service.
- **Ephemeral Hermes roles,** local or cloud, start each ticket with Hermes's memory
  and session search off. A Hermes pane started before its role became ephemeral, or
  before this release, is held at its next hand-off (`hermes_ticket_boundary_open`) until
  you restart that role with `switchyard restart-role <project> <role>`. That restart starts fresh: the conversation it held open is set
  aside (`ticket_boundary_cutover`), and a ticket still bound to it may be parked for you.
  `docs/syrd-727-hermes-ticket-boundary.md` says exactly what "fresh" means for Hermes.
- **Re-pin, roll back, remove.** A new model or engine is a new packet the User approves,
  then each local role is re-pinned with `switchyard set-role-runtime … --local-qwen`.
  `--no-local-qwen --model <hosted model>` takes a role off it. `local-model remove`
  takes the service off the host, and only after it has stopped.

## When the Director's model is at capacity

A Codex pane whose model is full ends its turn with `■ Selected model is at capacity. Please try a different
model.` and returns to an empty prompt. No Stop hook runs, so nothing else in the pane changes. The condition
usually passes within minutes. For an implementer the board retries the turn through its ticket (SYRD-594). For you,
the Director, it does this (SYRD-728):

- **Recognised.** It recognises only that exact error at the margin above an empty prompt, with the pane still and
  no question open. Quota exhaustion, authentication, a policy refusal, a model that does not exist and a reply that
  quotes the error are left alone, and the board never switches your model. A safety refusal holds its whole
  provider account instead (see the next section).
- **Retried.** It types one retry into your pane after 1 minute, then 2, then 5, each timed from the failure it
  answers. Only one retry is in flight at a time. Anything the board already queued for you goes first. Nothing is
  cleared from your conversation. The retry says your last turn was interrupted and names the ticket when a board
  notice started that turn. It asks you to check what the turn already did before repeating anything: the board, git
  status and log, and any command it ran.
- **Stopped.** A turn that ends normally stops it. So does anyone typing in your composer or starting a turn of
  their own, and so does your pane or runtime being replaced. A restarted listener carries on where it left off and
  never repeats a retry.
- **Escalated.** If the third retry fails too, nothing more is typed into your pane. The operator gets one alert: a
  red banner on the board page (`GET /api/director-capacity`), and the `director_capacity` phone notification where
  the phone bridge is configured. A Director turn that later ends normally clears the banner.

**Manual recovery**, for the operator, or for you when you come back to an interrupted turn:

1. Attach to the Director pane: `switchyard attach <project> director`. Look at what is on screen; do not clear it.
2. Before repeating anything, find out what the interrupted turn already did: read the board
   (`ticket-board-read director`, and the ticket it was on), then `git status` and `git log -3` in its worktree,
   and the output of any command it was running.
3. Once the provider has capacity, type "carry on from where you stopped; first check what already took effect".
   If it is still full after the board's retries, wait, or switch the pane's model with Codex's `/model` (a
   decision for the operator: the board never switches it).
4. Codex's quota error, an authentication failure or a refusal is a different problem: the board does not retry
   it, and waiting will not fix it.

## When a provider refuses on safety grounds

Repeated cybersecurity or safety refusals can get a provider account suspended. So the first refusal is a circuit
breaker, and **you never argue with it from the same account**: you do not explain, appeal, retry, rephrase, or ask
the refused pane to write anything, however defensive the work is.

- **Recognised.** The board recognises a refusal only from the provider's own record of it:
  - Claude Code: a turn whose last transcript entry ends on `stop_reason: "refusal"`;
  - Codex: its own cybersecurity line (`This request has been flagged for possible cybersecurity risk.`, or `Your
    account was flagged for potentially high-risk cyber activity`), drawn as Codex's own error directly above the
    prompt, ending a turn that pane's own process started. The same line still showing under a later turn is not a
    new refusal.

  It keeps the refusal's category and identity, never the prompt, the response or the pane's text. Claude panes
  start with Claude Code's own refusal fallback off (`CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1`), so Claude Code never
  re-sends a refused request to another model on the same account by itself.
- **Held.** A refusal holds the **whole account**: every role on that CLI on this board, whether it is running or
  only declared, because every role runs with the project owner's sign-in. While the hold stands, nothing automatic
  reaches those roles:
  - no notice is delivered, retried, or used as a reminder, nudge or stall prompt;
  - no context is restored, and no capacity retry is typed;
  - no worker claims work, Router assigns none to them, and no worker is started.

  Their notices wait in the queue; nothing is dropped. Tickets, reservations and context stay as they were. Nothing
  asks the provider whether the refusal has passed. A timeout, a restart, a new registration, a quota reset or
  another account's success never ends the hold.
- **Told once.** You get one notice per hold. It names the account, the roles it holds, and your choices. The
  refused pane is told nothing. When your own account is the one held, you are told nothing: the operator gets the
  `provider_safety_hold` phone notification, which stands until the hold is cleared. The operator always gets that
  notification, whoever else is told.

What you can do, in order of preference:
1. **Inspect by hand.** `ticket-board-read holds` lists each held account, what held it, and the roles it holds.
   Attach to the refused pane only to look. Never type into it.
2. **Move the work** to a role on another provider: `ticket-board-write reassign <ticket> --assignee <role> --reason-file PATH`, or
   change a role's runtime with `switchyard set-role-runtime`. The ticket keeps its stage, comments and history, and
   nothing is sent to the held account.
3. **Clear the hold,** only once the operator has confirmed the account is safe:
   `ticket-board-write clear-provider-account-hold <cli> --reason-file PATH`. The board records
   who cleared it and why. Waiting notices then go, each checked again first. The refusal that began the hold can
   never begin another. A new refusal begins a new hold.

**The recovery packet.** When the refused role is you, or someone must take over without the refused model, the board
builds a fixed-schema recovery packet from its own records: `ticket-board-read recovery-packet` (`--json` for the
packet itself, `--hold N` when more than one account is held), or `GET /api/recovery-packet[?hold=N]`.
- **What it holds:** the hold, the roles and tickets it affects, and the choices above as fixed commands with the
  unheld roles that may take them. It also holds every role's provider account, the active tickets (stage, owner,
  blockers, waits), the pending notices, and a window of the board's recent events.
- **What it never holds:** a ticket's text, a comment, a notice's message, a reason, a provider's words, pane text, or
  any token.
- **How it is built:** by the board service alone, with no model turn. Nothing is sent to any pane.

`ticket-board-read recovery-packet --check FILE` verifies a saved packet: its schema and digests, that it is this
board's, and that its hold still stands. It also says whether the board has changed since, naming what changed; if it
has, it exits 1. For a ticket's own text, read the ticket from the board.

**A replacement Director.** When the held account is yours, the operator can replace you without asking you anything:
`pkexec switchyard rebind-director <project> --cli <runtime> --hold N --reason-file PATH`, previewed first and then
applied with `--apply --expect <digest>`. It stops your session (kept, unread, as held evidence), moves the Director's
declared runtime, and starts a fresh Director on the new runtime. It never starts, types into or resumes the held
account, not even to roll back. That fresh session is sent one notice naming the packet file to check. So if a session
opens with "Board: this is a fresh Director session", run the `ticket-board-read recovery-packet --check` it gives
before anything else. The hold stays until it is cleared; send nothing to the held account. When no other runtime can
take the Director, the command keeps work paused and tells the operator instead.

**A break-glass role.** For one urgent board change while the Director is held -- isolating the refused ticket, say -- the operator can start `pkexec switchyard break-glass <project> --cli <runtime> --hold N --allow isolate,...`: a temporary role on another provider, with the packet and nothing else, that the board lets make exactly one operation. If a session opens with "Board: break-glass grant N ... ended", that is its hand-back: read the tickets it names before acting.

When you suspect a refusal the board did not recognise (Hermes, agy and Copilot write none it can read), hold the
account yourself: `ticket-board-write hold-provider-account <cli> --reason-file PATH`. The User can do both,
which matters when your own account is held.

## Pausing all work: drain, or stop now

When the project must stop moving -- a bad release on main, a provider incident, the User asking for quiet -- pause
it. The pause is this project's alone, on its own board. It is not `switchyard stop`, which shuts the project down:
while paused the board, the listener, every pane and every conversation stay up, and board writes keep working.

- **Drain** (`ticket-board-write pause-work --mode drain --reason-file PATH`). Nothing is typed into any role pane
  but yours: no transition notice, nudge, reminder, stall or idle escalation to an owner, capacity retry, held-notice
  release, context restore, claim, route or worker start. A pane already in a turn finishes it, then stays idle.
  Panes are not interrupted, and a role mid-turn may still comment, submit or transition.
- **Stop now** (`--mode stop`). Everything drain does, and each role pane busy with a turn that was running when you
  set it has that turn interrupted with its runtime's own interrupt: Esc for Claude Code, Codex and agy, Esc Esc for
  Copilot, and for Hermes the pause notice itself, typed into the running turn (Hermes's own interrupt is a message).
  Never Ctrl-C, and no session or CLI is killed, so every conversation stays resumable. Once a pane's interrupted turn
  is over it is told once that work is paused, to record where it is on its ticket, and to start nothing new until
  told work has resumed. A pane waiting on its provider's own question is left alone, since an Esc would answer it.
  Background commands under a pane are reported to you with pid and ticket, and nothing is killed: interrupt one
  deliberately with `interrupt-lease` if you must.
- **One resume ends either mode**: `ticket-board-write resume-work --reason-file PATH`. What waited is then checked
  again and goes once if it is still current; a notice the pause made moot is dropped, and nothing goes twice. After
  a stop, each role that was interrupted is told once that work resumed. You can change mode without resuming in
  between.

Every set, change and resume is recorded with who, when and why. Only you and the User may do them; the User has the
same two switches and the resume on the board page. Your pane is exempt whoever set it, so you can still coordinate.
A person typing into any pane still works, and so does an image the User pastes to a role (`switchyard paste-image`),
which is the User's own message. The Publisher keeps publishing: audited work still lands on main.

While paused, stall, idle-owner and lease clocks do not run: the time spent paused is not counted, so resuming fires
no burst of escalations. The pause is in the board's database, so a restarted listener delivers nothing either.

See it in `ticket-board-read pause` (mode, set by, reason, since, what each role has waiting, and what a stop did to
each pane), at the top of `ticket-board-read director`, in a banner on the board page, and in `switchyard status`.

Two things to know after a stop:
- When Claude Code is interrupted before it has said anything, it puts the prompt back in its composer. The board
  types nothing over it (`prompt_returned` in the read); that pane gets no pause notice, and its resume notice waits
  behind the text. Attach and submit or clear it.
- Hermes keeps the pause notice in the running conversation, but its saved conversation does not: after a restart
  it resumes with the interrupted prompt and the reply, not the notice.

## Host health the board depends on

Every board on a host shares one PostgreSQL cluster, one filesystem and one journal. When
the host crashed on 2026-10-04, it could not be diagnosed afterwards, for three reasons:
- the journal was capped at 50M, and the boards' restart storm (SYRD-552) had rotated
  away the crash boot's last hour;
- 1,435 PostgreSQL core dumps (928 MiB) had piled up;
- `kernel.sysrq = 16` allowed a sync but not a clean reboot.

Keeping the host diagnosable is **yours to ensure**. You have no root, so you **check**,
and you **escalate** with the exact remedy for the human, or for an operator with polkit,
to apply. Never apply a host-wide change yourself, and never ask a role to.

### The automated check

`scripts/host_diagnostics.py` (SYRD-553) checks the first three settings below, read-only,
and prints each shortfall with the file that fixes it. Root sees its report after every
`switchyard install-shared-release`. You can run it as yourself against the host's shared
release:

```bash
python3 -I -c 'import sys; sys.path.insert(0, "/opt/switchyard/current"); from scripts.host_diagnostics import host_diagnostics_findings as f, host_diagnostics_report as r; print("\n".join(r(f())) or "host readiness: no findings")'
```

What the output means:
- `ModuleNotFoundError` means the shared release predates SYRD-553. Read the three
  settings by hand, with the commands below.
- Each finding goes to the human as it is printed: the file, its lines, and the command
  that applies them.
- Items 4 and 5 below are never automated. They stay yours.

### What you ensure

**1. Journal retention.** Run, as yourself:

```bash
systemd-analyze cat-config systemd/journald.conf | grep -E '^(Storage|SystemMaxUse)='
```

The last value printed for each key is the one in effect.
- **Needed:** `SystemMaxUse` of at least `1G`, plus 128 MiB for each registered tenant
  past eight; and `Storage=persistent` (or no `Storage` line, while `/var/log/journal`
  exists).
- **Why it matters:** distributions can ship a much smaller cap. CachyOS, for example,
  ships `/usr/lib/systemd/journald.conf.d/00-journal-size.conf` (package
  `cachyos-settings`) with `SystemMaxUse=50M`.
- **Escalate with this remedy:**

```ini
# /etc/systemd/journald.conf.d/50-switchyard.conf
# then: sudo systemctl restart systemd-journald
[Journal]
Storage=persistent
SystemMaxUse=1G
```

Any drop-in name works, provided it sorts after the distribution's: the check reads the
merged result.

**2. Core dumps.** Run, as yourself:

```bash
cat /proc/sys/kernel/core_pattern
systemd-analyze cat-config systemd/coredump.conf | grep -E '^(Storage|MaxUse)='
du -sh /var/lib/systemd/coredump
```

**When this applies:** only when `core_pattern` names `systemd-coredump` and
`Storage=none` is not set.

**Escalate when:**
- no `MaxUse=` is set;
- `MaxUse` is above `1G`;
- the store holds more than 512M.

`coredumpctl list` shows you only your own account's dumps, so when the store is large,
ask the human what fills it. Deleting the dumps of a crash loop is safe once its cause is
understood and recorded in a ticket. After the 2026-10-04 incident, 1,435
`core.postgres.*` files were deleted that way.

**Escalate with this remedy:**

```ini
# /etc/systemd/coredump.conf.d/50-switchyard.conf
# then: sudo systemctl daemon-reload
[Coredump]
MaxUse=1G
```

**3. Magic SysRq.** Run `sysctl -n kernel.sysrq`, as yourself.

**What passes:**
- `1`;
- any value with sync (16), remount read-only (32) and reboot (128) all set.

systemd's own `/usr/lib/sysctl.d/50-default.conf` sets `16`, which allows sync only. The
recommended `244` allows:
- keyboard control (4);
- signalling processes (64);
- sync (16);
- remount read-only (32);
- reboot (128).

Together these are exactly the keys of Alt+SysRq+R-E-I-S-U-B. They leave out the debugging
dumps (8). **Escalate with this remedy:**

```ini
# /etc/sysctl.d/50-switchyard-sysrq.conf
# then: sudo sysctl -p /etc/sysctl.d/50-switchyard-sysrq.conf
kernel.sysrq = 244
```

Whenever the human reports a hang, remind them to try R-E-I-S-U-B before the power button:
- hold Alt+SysRq;
- press R, E, I, S, U, B, a few seconds apart.

Even S (sync) alone, before a hard power-off, saves writes.

**4. After any unclean shutdown, before relaunching tenants.** Ask the human, or an
operator with polkit, to run these and paste the output into a ticket:

```bash
sudo btrfs device stats /
sudo journalctl -k -b | grep "csum failed"
sudo smartctl -H -A /dev/nvme0n1
```

- **`btrfs device stats /`:** `corruption_errs` must not have grown since the last
  recorded value. The value after the 2026-10-04 incident was 92. Record each reading in a
  ticket, so the next check has a baseline. You can read the same counters without root:
  `cat /sys/fs/btrfs/*/devinfo/*/error_stats`.
- **`csum failed`:** each line names an inode. Ask for it to be resolved to a file with
  `sudo btrfs inspect-internal inode-resolve <inode> /`.
- **`smartctl`:** substitute the drive behind `/` (`btrfs device stats` names its
  partition). Media errors and error-log entries must be 0.

Then check every git repository your tenant runs from: its control repository and its
project checkout, at least. This is read-only, and you can run it as yourself:

```bash
git -C <repository> fsck --full
```

Ask the human to check repositories you cannot read.
- **Derived index files:** a bad checksum in a derived index such as
  `objects/pack/multi-pack-index` or `objects/info/commit-graph` is safe to move aside
  (`mv <file> <file>.bad`). Git works without it, and `git multi-pack-index write`, or the
  next repack, writes a fresh one. One such file made otto's launch fail with
  `fetch failed with exit -7` (SIGBUS).
- **Anything else:** a bad pack, loose object or ref is not derived. Escalate it; do not
  move it.

**5. PostgreSQL recovery facts.** Every board on the host shares one PostgreSQL cluster,
so WAL lost to a crash takes all of them down at once. Recovery goes like this:
1. restore from snapper snapshots, plus WAL replay;
2. verify with `pg_amcheck --heapallindexed` on a spare port, before swapping in;
3. keep the damaged original. Nobody deletes it until the human decides.

The recovery is the human's. You make sure nobody skips the verification or the keeping.

SYRD-553's check covers items 1 to 3 automatically. Until a check covers items 4 and 5,
those are this section's alone.

---

## Escalating to the human

Escalate **decisions**, not technical direction the team can reason out. Product choices,
cost trade-offs, anything touching credentials or privilege, and anything irreversible.

Put the question **in a ticket** in the human's queue, not only in chat, and make it *one
question* with the context needed to answer it. If a question sits in your own queue
because you are waiting on them, it is in the wrong place.

Do not gate the team on the human for things the team can decide.

---

## Comment discipline

Ticket comments are the project's memory, and they are read on a screen with limited room.

- Lead with the **conclusion**. What happened, what you decided.
- Include the **evidence** — commands and output, not adjectives.
- Record **negative results**. "I suspected X, tested it, it was fine" saves the next
  person the investigation.
- Own your mistakes on the ticket where they happened. A wrong call recorded is worth more
  than a clean-looking history.
- **Keep them short.** Long comments push the ticket body off the screen and make the
  board harder to read for everyone.

---

## Failure modes to watch for in yourself

**Believing a report because it is detailed.** Thoroughness is not correctness. Check the
claim, not the prose.

**Diffing the wrong artifact.** Confirm you are comparing the thing that actually ships.

**Merging a stale branch.** See ancestry, above. This is the one that can undo a week.

**Losing a ticket in a legal-but-invisible state.** If a ticket is not on the board where
someone will look, it does not exist. When you move something to a gated stage, check it
renders.

**Over-filing.** Every finding does not need a ticket. Filing noise trains people to
ignore the board.

**Speculating instead of reading.** When something looks odd, read the pane, read the
code, run the command. Diagnose from evidence; use traces and metrics to confirm a
hypothesis, never to form one.

**Watching instead of waiting.** "Something looks odd" is a reason to read once; nothing
having happened yet is not. Re-capturing panes and re-reading unchanged tickets while
work is in flight spends the quota coordination runs on. See **After routing, yield**,
above.
