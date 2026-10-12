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

**Recover a stalled ticket rather than overriding it.** When an owner reports
completion or a dependency and does not transition, `recover-stalled-ticket <id>
--reason "..."` takes the one declared no-code transition that owner could have
taken: no gate or sign-off is skipped, the work stops at its next required gate,
and that gate's owner is notified once. It always exists, and for a review whose
sign-off was given under a hold it is the command that pays it. `force-move` and
`override-move` are not capabilities a document can grant -- granting them would
be declaring the workflow's own bypass -- so they are admitted by control-role
identity instead: the role holding `merge`, `set-blockers` and
`set-manually-controlled` is the controller and may use them (SYRD-49, SYRD-78,
SYRD-180).

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
  --payload-json '{"reason": "User completed the steps and accepted", "commit_hash": "<the commit the ticket carries>"}'
# acceptance, no-code ticket (commit_exempt and no commit): the reason and NO commit_hash key;
# an empty "commit_hash" is refused as an invalid commit
ticket-board-write workflow-action <id> relay_user_sign_off \
  --payload-json '{"reason": "User accepted the recommendation in conversation"}'
# rejection, where relay_user_kick_back is listed
ticket-board-write workflow-action <id> relay_user_kick_back \
  --payload-json '{"reason": "User reports <what failed>"}'
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
ticket-board-write director-edit <id> --set audit_signoff=false --reason "<why the approval no longer holds>"
```

The same goes for `inspector_signoff` and `user_signoff`. The reason is recorded on the
ticket, and the gate flag stays, so the review still happens. `director-edit` can only lower
a sign-off; raising one is refused. `edit-fields` cannot change a sign-off on a declared
board ("flag change requires authorized workflow action"). On an undeclared board it still
lowers one without a reason, so use `director-edit` there too. Never clear `needs_audit` to
get rid of a stale approval: that removes the review itself (SYRD-280, from MEFP-8).

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
waiting. Root's recorded workflow is not refreshed by this or any workflow
write until SYRD-561 and SYRD-562 land. Details:
[SYRD-567](../syrd-567-design-stage.md).

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
