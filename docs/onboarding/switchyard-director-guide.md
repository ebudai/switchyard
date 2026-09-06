# Switchyard Director Guide

How to be a director. This is the **judgment** doc — what to do, what to refuse, and what
to verify. For the mechanics of the board and `directorctl`, see
`switchyard-board-guide.md`. For why the system is shaped this way, see
`adversarial-collaborative-methodology.md`.

This guide is project-agnostic.

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

**Reuse audit's evidence, but prove it is about your tree first.** An independently
recorded audit result is evidence for the exact commit and tree it names, and repeating
that suite against an unchanged commit buys nothing. So spend the effort on identity
instead: confirm the commit audit recorded is the commit you are integrating — the tree,
not just the ticket field — and read what audit actually ran and where. Then run focused
checks where their evidence leaves a gap. The implementer's numbers are never evidence;
only an independently recorded result is.

Re-run the broader suite yourself when the tree changed after audit recorded its result,
when the recorded environment or result is inadequate for what you are integrating, when
a focused check of yours contradicts their evidence, or when repository policy requires
the run. Then it is yours, in a throwaway worktree at the exact commit — green on the
branch is not green on main. After a merge commit, a short integration smoke check is
enough unless the merge needed real resolution or pulled in changed dependencies.

**Say which is which.** Name what you ran yourself and what you reused, with the commit
the reused result covers. "Audit's suite passed at `<sha>`, which is the tree I merged; I
ran the mutation myself" is a record. "Tests pass" over someone else's run is not.

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

**Narrate every override.** `force-move` and `override-move` bypass the workflow;
`edit-fields` bypasses the normal field-specific operations. Each exceptional use needs a
comment explaining why. Never fake a signoff.

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

**When a live role sees a change.** Not immediately, and that is deliberate.
The prompt is delivered at the start of a *fresh* conversation, so:

- a role whose conversation is running keeps the context it already has;
- the new prompt is used by that role's next fresh conversation, or after an
  explicit restart of the role;
- resuming or continuing an existing conversation does **not** pick it up, because
  re-injecting a remit into a session that already has one only crowds it.

So setting a prompt never interrupts work in progress. If you need a role to pick
one up now, restart that role rather than expecting the running conversation to
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
instead, the deployed release genuinely differs from the pinned one and an
operator runs that first.

The change itself is applied atomically against the board's current workflow
revision: if the configuration moved since you read it, your write is rejected
rather than silently overwriting someone else's. A rollback journal is written
alongside, exactly as for `ticket-board-workflow apply`.

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
