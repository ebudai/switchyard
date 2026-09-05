# Switchyard Director Handoff

This is the Switchyard-specific handoff for the incoming director. It does not
replace `docs/onboarding/`; use onboarding for the generic director method and
this document for the project state, tenant map, and decisions that should not
be rediscovered.

## The Import Boundary — Read This First

This is the single fact that will shape your working day, and the one most likely
to look like a misconfiguration you should fix. It is not. It is deliberate.

**Agents cannot push to `/data/git/switchyard.git`.** A push from an agent fails:

    remote unpack failed: unable to create temporary object directory

The bare repo is owned by the operator. Every tenant installs from it, so nothing
that runs unattended may write to it. Eric confirmed this twice, in these words:
*"don't loosen it, polkit for that is fine."* Do not make the repo group-writable,
do not add an agent to its group, do not "fix" the permissions.

The consequence is a workflow you must plan around:

1. an implementer commits in its own worktree and cannot push
2. it writes a named-branch `git bundle` under
   `/var/tmp/switchyard-handoff/` and posts the path, refspec and sha on the
   ticket. Do not use `/tmp`: it is tmpfs, and a reboot on 2026-09-05 erased
   five pending bundles. The work was recoverable only because the worktrees
   survived (PGU-907 through PGU-913).
3. read the bundle's ref name with `git bundle list-heads <bundle>`, then
   **you** import it as the repo owner:

       pkexec --user eric git -C /data/git/switchyard.git \
         fetch <bundle> '+<source-ref>:refs/heads/<branch>'

   `pkexec` is intermittent and its failure can be completely silent even with
   polkit and the owner's session healthy. On 2026-09-05 it failed repeatedly
   with no output between successful imports. Do not poll it or keep retrying:
   that locked the owner out for nine minutes on 2026-09-03. The cause remains
   uncharacterised (PGU-912). The fallback is
   `/var/tmp/switchyard-handoff/switchyard-merge.sh`, run by the repo owner with
   no sudo. That script is generated for a particular handoff or batch: read it
   before use and regenerate it when the queued bundles change.
4. only then can the implementer submit to audit; the write client refuses
   `submit_to_audit` until the commit is on origin. That check runs before the
   board request and uses the caller's current working directory, so a Switchyard
   commit must be submitted from a Switchyard checkout, not from a tenant project
   or the old PGU checkout.
5. after audit, before moving `main`, run the blocking ancestry gate described
   in *Operating Rules*. Use `/var/tmp/switchyard-handoff/safe-merge.sh`; do not
   substitute a command sequence that merely prints the check.

**Every round trip therefore stalls on an operator being physically present.**
`pkexec` normally needs a human to approve a prompt, and its owner-run fallback is
also deliberately not available to unattended agents. On 2026-09-02 that meant
thirteen merges, roughly thirty privileged imports, and six director holds.

Two traps that follow from it:

- **The watchdog reads this import-wait stall as "implementer may be stuck" and
  escalates.** In this specific case it is almost always wrong. Read the pane before
  acting: an implementer that has finished, bundled, and said so is not stuck, and
  telling it to try harder wastes its time. There is no board state meaning "waiting
  on a human" — see *Action Naming* below.
- **Never use `pkexec` as a liveness probe.** Repeated failed authentications lock the
  operator out of their own machine. This happened, for nine minutes, because a
  background watch polled it every two minutes overnight.

Finally: prefer `git clone --no-local /data/git/switchyard.git` for handoff and
recovery examples. Plain clones into `/home` and `/tmp` work on this machine because
they cross from `/data` to another filesystem and Git copies objects instead of
hardlinking them. Forced local clones can fail across that boundary with
`Invalid cross-device link`; clones into `/data` can also run into protected-hardlink
ownership rules. `--no-local` makes the intended copy behavior explicit.

## What Switchyard Owns

Switchyard owns the coordination system around the work, not the PGU game
renderer. Its surface is the ticket board, workflow engine, pane launcher,
tenant provisioning, immutable shared install, cross-tenant references,
notification routing, update hooks, and operator handoff.

PGU-specific renderer work, galaxy rendering, capture quality, and visual QA
belong to the PGU/game team unless the ticket is explicitly about board or
launcher machinery.

## Tenant Map

- `pgu` is the ARCHIVE board at `http://127.0.0.1:8770`, prefix `PGU`: 833
  tickets as of the final pass, 710 done, 98 cancelled, 25 non-terminal. Act
  on PGU only when work is still PGU-owned or needs an archive pointer.
- `syrd` is the planned LIVE Switchyard board. Its slug is `syrd`, prefix is
  `SYRD`, port is `23326`, and socket should be
  `/run/syrd-ticket-board/ticket-board.sock`. New Switchyard work goes there
  after cutover.
- `mefp` is a live peer tenant on port `26623`.
- `otto` is a live peer tenant on port `20740`.

Port allocation for Switchyard tenants is
`18770 + (blake2s(slug, digest_size=2) mod 10000)`. PGU's `8770` is a
compatibility special case, not an output of that formula.

## Current State

Facts that move are stated as how to FIND them, not as values. Values here were
true on 2026-09-02 and are marked as such.

    switchyard main     git --git-dir=/data/git/switchyard.git rev-parse --short main
    pgu main            git --git-dir=/data/git/pgu.git rev-parse --short main
    shared install      readlink -f /opt/switchyard/current
    live board release  readlink -f /home/agent/pgu-ticketboard-live/current
    tenants             ls /etc/switchyard/projects/
    mirror              https://github.com/ebudai/switchyard

CHECK THOSE FOUR AGAINST EACH OTHER BEFORE YOU TRUST ANY OF THEM. On 2026-09-02 all
three deploy surfaces had drifted from main at once: `/opt/switchyard/current` pointed
at `81dc45f81ec0`, which was 18 commits behind main at audit time; the live board
predated the commit-verification work merged that morning (so verification was merged
but NOT RUNNING); and the mirror was one commit behind. Drift is the normal state, not
an incident; merging is not deploying.

The repository split is complete. `pgu.git` carries no Switchyard-named paths;
PGU instruction and crash files still intentionally point at `/opt/switchyard` as the
shared tool location.

`syrd` is not provisioned: no registry entry, no systemd unit, nothing on port 23326.
Do not provision it twice. Before any cutover action, verify registry, systemd units,
board port, socket path, ticket prefix and pane environment.

Use neutral `TICKET_BOARD_*`, `TEAM_LAUNCHER_*`, `UPDATE_HOOK_*` and `ALLOW_MAIN_PUSH`
names. Do not introduce `SYRD_*` vocabulary.

The mirror is AGPL-3.0 with contribution terms preserving the owner's licensing
options. It is read-only; the local bare repo is canonical and GitHub PRs auto-close.

## Migration Rule

Migrate only non-terminal PGU tickets whose remaining work belongs to
Switchyard. Terminal tickets stay on PGU. Review-stage tickets normally finish
their current review on PGU. Borderline tickets move only when the next action
would naturally belong to the dedicated Switchyard team.

For each migrated ticket, create the `SYRD-N` record with
`origin_project="pgu"` and `external_source_ref="pgu:PGU-NNN"`. Add a final
comment on the PGU original pointing to the new `SYRD-N`, then close the PGU
ticket as superseded elsewhere. This keeps both directions resolvable and
leaves existing `PGU-NNN` references untouched.

## Decisions To Preserve

1. The shared install is immutable releases, not a writable checkout.
   Reason: shared code in `/opt/switchyard` must be auditable and repeatable.
   Build from the bare repo through ephemeral clones, run immutable releases,
   and use commit markers; a self-deploying checkout would make production
   state depend on whoever last ran git in `/opt`.

2. Workflow vocabulary moves toward FK-over-CHECK, and the rollout is gated.
   Reason: stages and roles are tenant data, but changing that integrity model
   affects routing, notifications, admin tools, and migration. Gating keeps a
   schema cleanup from becoming an unbounded workflow change.

3. "No notification" must be explicit.
   Reason: a new stage must not inherit `NULL` routing and silently notify
   nobody. Silence is a real routing choice, so it needs to be visible in the
   workflow data.

4. PGU remains the historical archive.
   Reason: bodies, comments, audits, and handoff notes are dense with
   references such as `PGU-820`. Leaving the archive live preserves those links
   without a risky rewrite of hundreds of historical records.

5. A VCS close role is not an implementer.
   Reason: the close role owns source-control closure and provides separation
   from implementation. If it is also an `in_progress` owner, it can review or
   close work it wrote itself and it also picks up serial-focus behavior meant
   for implementers.

6. An assignee must be an owner of the stage it sits in.
   Reason: the board is the routing engine. If a role can sit in a stage it
   does not own, notifications, serial focus, and accountability all lie about
   who is expected to act.

7. Hook rollout must never guard repositories without a Switchyard workflow.
   Reason: a push-blocking hook only belongs where the workflow it names
   exists. The two platform repositories are named explicitly by
   `PLATFORM_WORKFLOW_REPOSITORIES`; tenant repositories are derived from the
   Switchyard registry, so unrelated operator projects stay unguarded.

8. Privileged operations must not write as root into an operator checkout.
   Reason: root-owned files in a user's repository break their git workflow
   later, far from the command that caused it. Fetch as the repository owner
   and suppress bytecode across sudo with an explicit environment.

9. Repoint deploy sources before removing old paths.
   Reason: board deploys need a readable source after extraction. Moving them
   to `switchyard.git` first made PGU path removal routine instead of an outage.

## Operating Rules

**Gate every move of Switchyard `main`.** Since 2026-08-31 the history has been
strictly linear and merging has been a fast-forward ref move. A stale branch
does not conflict in that model; it silently drops everything that landed after
its base. `git rev-list --count origin/main..HEAD` can still report one and does
not detect the loss. Before every merge, require:

    git merge-base --is-ancestor main <sha>     # exit 0 only

Use `/var/tmp/switchyard-handoff/safe-merge.sh`. It refuses a failed ancestry
check and exits non-zero. This gate exists because the director saw the check
print `NO` and merged anyway on PGU-915, reverting PGU-913; PGU-909 had already
measured the same hazard across four pending branches. A check that can be read
past is not a gate.

Use the board tools, not direct JSON edits, for ticket changes. Route work
through tickets and preserve origin metadata for cross-tenant records. Do not
self-deploy shared releases. Do not create notification behavior by omission.
Do not move review obligations across tenants without carrying findings,
blockers, commit hashes, and sign-off state explicitly.

## Deploying Changes

Treat the shared install and the live PGU board as separate deploy surfaces.
`/opt/switchyard/current` is the shared release and needs root to update. The
board service runs code from `/home/agent/pgu-ticketboard-live/current`; deploy
that agent-owned release, without sudo, from a Switchyard checkout:

    scripts/ticket-board-service.sh deploy-restart

Installing the shared release and restarting the board unit does not deploy
board code. PGU-906 was declared live after that sequence and an HTTP 200, but
the expected payload field was still absent because the board release remained
old. Verify the changed behavior and the board `build_id`, not merely process
health.

Prefer the board clients under
`/home/agent/pgu-ticketboard-live/current/scripts/`. The client under
`/opt/switchyard/current` can remain usable while silently lagging several
tickets behind; PGU-913 exposed that only because the live client had a new verb
the shared client lacked. Check rather than assume:

    readlink -f /opt/switchyard/current
    readlink -f /home/agent/pgu-ticketboard-live/current
    git --git-dir=/data/git/switchyard.git rev-parse main

## Commitless Deliverables

Use `submit-to-audit-without-commit` when the ticket's deliverable is not a diff:

    ticket-board-write submit-to-audit-without-commit PGU-N --reason "<what was delivered>"

That includes spikes and investigations whose result is a finding, verification
passes, and operator actions whose change lives in a database, systemd unit or
tenant configuration. PGU-823 is an operator-action example, PGU-828 a live
verification, and PGU-752 an umbrella whose implementation was carried by
separately audited tickets. Do not borrow another ticket's hash: it resolves,
looks authoritative and sends audit into the wrong diff. PGU-913 added this path
after that happened three times. `request-commit-exempt` is different: it sends
unfinished or unimplementable work back to `analysis` and unassigns it.

## Awaiting Another Role

Use `await-role` when the next action belongs to another agent role and there is
no blocker ticket to name. The escalation now works as one mechanism: it reaches
the director queue (PGU-906), survives an acknowledgement comment (PGU-915),
does not relabel a returning ticket as new (PGU-912), and is not contradicted by
a turn-end reminder (PGU-916).

The marker expires after four hours, so a genuine stall behind a forgotten flag
resurfaces. It clears on any state or assignee change, whoever makes it, and on
any other ticket edit made by the awaited role; a comment alone is not action,
and `clear-awaiting-role` always works. Never await your own role. The normal
write path rejects it, and a same-actor marker written by another path would
clear immediately under the ticket-activity rule.

## Known Post-Handoff Direction

Do not replace commit exemptions during this handoff. Eric's recorded direction
from 2026-09-05 is to introduce ticket types later, with the commit-hash
requirement attached only to types whose deliverable should be a repository
change. This is explicitly post-handoff work; preserve the direction so the next
director neither re-derives it nor builds a conflicting mechanism.

## Agent Instruction Files

The extracted Switchyard repo should have its own `AGENTS.md`, `CLAUDE.md`, and
`GEMINI.md`, not PGU renderer instructions. Keep `AGENTS.md` as the shared
source of truth for board commands, branch/submit rules, neutral environment
names, immutable installs, routing, notification policy, and no root-owned
writes. Keep `CLAUDE.md` and `GEMINI.md` as thin role overlays.

## Installing Switchyard

One command, from a fresh clone:

    git clone https://github.com/ebudai/switchyard && cd switchyard && sudo ./install

`--dry-run` prints every command it would run and changes nothing; run that first if
you want to see what it does before granting root. It installs no agent CLIs and
asks nothing; `--yes` is accepted for compatibility. Installing claude, codex, agy,
or hermes is the user's job, for the project's owner user; switchyard detects what
is present and walks sign-in at project creation.

**Authentication is manual and always last.** Every agent CLI needs a browser sign-in
or an API key, and that cannot be scripted — see PGU-889, which established that one
sign-in cannot front the others and that the only automatable paths are API keys,
which forfeit the subscription economics the project depends on. The installer's final
line names what you must still do.

## What The Tests Cannot See

`scripts/ticket-board-test-suite --group all` is the gate, and on 2026-09-02 it was
green at 148 while four real defects were live. Both blind spots are structural, and
both are consequences of correct decisions:

1. **The tests fake vendor commands.** They must — we cannot run four third-party
   installers in CI. So no test can see that a vendor installer prompts, which is why
   `--yes` was not actually unattended until PGU-890.
2. **Every developer machine has a git checkout at the deploy path. Every user gets an
   exported release.** So no test saw that `switchyard new` aborted outright on a
   correctly-installed machine until PGU-891.

Four of that day's defects were found by the operator running the real thing on a real
machine. Two of them the reviewer and I had looked directly at and passed.

The lesson, which generalises beyond this project: **ask what rule produced the output,
not whether this instance looks right.** A rule can be interrogated; an instance can
only be recognised, and plausible-looking output defeats recognition. The reviewer had
run the exact failing case, seen `Run \`claude\` and sign in`, and recorded it as
correct — because it looked reasonable, and nobody asked what was choosing that name.

## Action Naming

Recorded as observation for a future decision, not as a proposal. Almost none of the
friction on this board is mechanism; nearly all of it is vocabulary. Each of these
names describes what the system *does* rather than what the user *means*:

- `manually_controlled` — names a mechanism. The state actually needed is "held pending
  an operator action". Because the name did not say so, it was used as a nudge-mute, and
  before PGU-876 it *broke* the tickets it was applied to.
- `await-role` — cannot express "waiting on a human". It accepts only agent roles and
  refuses the caller's own, so the most common blocked state here has no name.
- `request-commit-exempt` — an implementer verb the director, who is its natural
  approver, cannot call.
- `defer` — moves a ticket to backlog, so "blocked" and "deprioritised" collapse into one
  word for two states needing opposite responses.

Renaming actions on a board with 890+ tickets of history is its own project with its own
migration risk. This is here so the decision is made from evidence.

## First-Day Checklist

1. Read `docs/onboarding/`, then this handoff.
2. Verify the live tenant map and that `syrd` is either absent or exactly once
   provisioned.
3. Verify `SYRD` prefix, port `23326`, socket path, board health, and pane
   role environment.
4. Verify notification routing for every workflow stage, including any explicit
   no-notification stage.
5. Verify the shared-install release marker and update path.
6. Review the final migration list immediately before cutover; do not trust an
   old inventory.
7. Confirm the three deploy surfaces agree with `main`: the shared install, the
   separately deployed live board release, and the mirror. On 2026-09-02 all
   three had drifted at once. Merging is not deploying; for board code, run
   `scripts/ticket-board-service.sh deploy-restart` and verify the behavior and
   `build_id`.
8. Do one import end to end before you need to: take a reviewed implementer
   bundle, run `git bundle list-heads` on it, import it from
   `/var/tmp/switchyard-handoff/`, run the blocking ancestry gate through
   `safe-merge.sh` after audit, and watch the ticket move. Try `pkexec` once; if
   it fails silently, stop and prepare the narrow `switchyard-merge.sh` for the
   repo owner. Learning that flow under time pressure on your first real ticket
   is the worst way to learn it.
