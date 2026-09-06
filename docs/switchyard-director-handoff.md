# Switchyard Director Handoff

This is the Switchyard-specific handoff for the incoming director. It does not
replace `docs/onboarding/`; use onboarding for the generic director method and
this document for the project state, tenant map, and decisions that should not
be rediscovered.

## Canonical Repository And Review Boundary

[GitHub](https://github.com/ebudai/switchyard) is the canonical Switchyard
repository. Actual Switchyard source clones use
`git@github-switchyard:ebudai/switchyard.git` as `origin`; the repository-scoped
SSH key can push named branches without sudo or polkit. The provisioned SYRD
role worktrees retain unrelated seed history and must not be repointed.

1. Clone `https://github.com/ebudai/switchyard.git` into a separate source
   checkout and work on a named feature branch. The existing SYRD seed/control
   repo has unrelated provisioning history: preserve it and its worktrees;
   never repoint them blindly to Switchyard source.
2. Push only the intended feature branch to GitHub. Open a pull request with
   the ticket, exact commit, behavior change, and validation when GitHub review
   is available. Keep contribution terms in
   `CONTRIBUTING.md` intact. Do not force-push or publish private backup refs,
   unrelated topic branches, or all local refs.
3. Before audit submission, fetch the feature branch into the board's selected
   read-only source cache. Provision SYRD with an explicit `--commit-git-dir`,
   and preserve that value through upgrades. There is no account-specific SYRD
   cache default; its provisioning control repo cannot verify real source commits.
   See [commit verification](ticket-board-project-provisioning.md#switchyard-source-commit-verification).
4. Submit the exact commit to audit from the real source checkout whose
   `origin` is GitHub. The write client checks origin reachability; the server
   independently checks its configured repository. Neither check should be
   bypassed to hide a publication or cache-refresh blocker.
5. After audit, the director checks the current GitHub main is an ancestor of
   the reviewed branch head and merges that exact commit on GitHub. Configure
   branch protection separately; until GitHub enforces it, the director's
   ancestry and audit checks remain the blocking merge gate. Record the actual
   merged commit and refresh the cache. Merging is separate from deploying the
   immutable shared and board releases.

No automatic outgoing mirror job was established. The former **Close Mirror
Pull Requests** workflow and mirror-only contribution text have been removed.
Review branch protection separately; retain independent audit and director
merge authority. Never loosen local repository permissions or copy another
account's private key.

`/data/git/switchyard.git` remains operator-owned recovery storage only. It
contains historical and private refs, so do not delete it or publish its refs.
It is not an active push remote or the SYRD board verifier. Shared-release
upgrades require an explicitly selected fetch cache in `SWITCHYARD_BARE_REPO`,
or an explicit GitHub checkout via `--source-repo`; cache refresh must never
push back to GitHub. Checkout-based board deployments fetch `origin/main`,
verify that origin is GitHub, and deploy only the reviewed merged commit.
Configure a bare cache to retain remote-tracking refs, then refresh it with:

```bash
git --git-dir="$SWITCHYARD_BARE_REPO" config remote.origin.fetch \
  '+refs/heads/*:refs/remotes/origin/*'
git --git-dir="$SWITCHYARD_BARE_REPO" fetch --prune origin
```

Until branch publication and verification are available, commit locally and
write a named-branch bundle to durable storage such as
`/var/tmp/switchyard-handoff/` or the project owner's state directory. Record
its path, ref, hash, tests, and precise submission blocker on the ticket for
the director. Do not use `/tmp` for pending review artifacts: a reboot erased
five pending bundles on 2026-09-05. Do not probe operator authentication in a
retry loop; repeated failed `pkexec` attempts previously locked the owner out.

For rollback, freeze new GitHub merges and preserve any new GitHub commits in
local refs and bundles before changing the source configuration. Never rewind
GitHub main or restore an outgoing force-push job. Existing immutable release
symlinks remain the deployment rollback mechanism. Offline recovery clones
from the local cache should use `git clone --no-local` to avoid hardlink and
cross-filesystem ownership failures.

## What Switchyard Owns

Switchyard owns the coordination system around the work, not the PGU game
renderer. Its surface is the ticket board, workflow engine, pane launcher,
tenant provisioning, immutable shared install, cross-tenant references,
notification routing, update hooks, and operator handoff.

PGU-specific renderer work, galaxy rendering, capture quality, and visual QA
belong to the PGU/game team unless the ticket is explicitly about board or
launcher machinery.

## Tenant Map

- `pgu` is the ARCHIVE board at `http://127.0.0.1:8770`, prefix `PGU`. Act on
  PGU only when work is still PGU-owned or needs an archive pointer.
- `syrd` is the live Switchyard board. Its slug is `syrd`, prefix is `SYRD`,
  port is `23326`, and socket is
  `/run/syrd-ticket-board/ticket-board.sock`. New Switchyard work goes there.
- `mefp` is a live peer tenant on port `26623`.
- `otto` is a live peer tenant on port `20740`.

Port allocation for Switchyard tenants is
`18770 + (blake2s(slug, digest_size=2) mod 10000)`. PGU's `8770` is a
compatibility special case, not an output of that formula.

## Current State

Facts that move are stated as how to find them, rather than as pinned values.

    GitHub main         git ls-remote https://github.com/ebudai/switchyard.git refs/heads/main
    selected cache main git --git-dir="$TICKET_BOARD_COMMIT_GIT_DIR" rev-parse refs/remotes/origin/main
    pgu main            git --git-dir=/data/git/pgu.git rev-parse --short main
    shared install      readlink -f /opt/switchyard/current
    live board release  readlink -f /home/agent/pgu-ticketboard-live/current
    tenants             ls /etc/switchyard/projects/

Compare GitHub main, the explicitly selected cache, and release markers before
trusting source freshness. Drift among them is possible; merging is not deploying.

The repository split is complete. `pgu.git` carries no Switchyard-named paths;
PGU instruction and crash files still intentionally point at `/opt/switchyard` as the
shared tool location.

`syrd` is provisioned and live. Do not provision it twice. Before changing its
runtime, verify the registry entry, systemd units, board port, socket path,
ticket prefix, pane environment, and protected commit-repository override.

Use neutral `TICKET_BOARD_*`, `TEAM_LAUNCHER_*`, `UPDATE_HOOK_*` and `ALLOW_MAIN_PUSH`
names. Do not introduce `SYRD_*` vocabulary.

Switchyard remains AGPL-3.0 with the contribution terms in `CONTRIBUTING.md`.
SYRD-6 supersedes the former mirror-only contribution policy.

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
   Build from reviewed GitHub commits through ephemeral clones, run immutable
   releases, and use commit markers; a self-deploying checkout would make production
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

**Gate every move of Switchyard `main`.** Refresh GitHub refs and require the
reviewed PR head to descend from current `origin/main` before the director
merges. In a source checkout with `origin` pointing at GitHub, use a blocking
sequence (replace `<reviewed-sha>` with the audited PR head):

```bash
git fetch origin &&
  git merge-base --is-ancestor origin/main <reviewed-sha>
```

A nonzero result blocks the merge. Rebase or integrate current main on the
feature branch and repeat affected review/validation; never move main to a
stale tip. Confirm the PR head still matches the reviewed hash and main has
not advanced when merging. The old local `safe-merge.sh` was an operator import
gate, not a replacement for GitHub PR protections. The ancestry requirement
remains: a previous local merge proceeded after the check printed `NO` and
reverted accepted work (PGU-915).

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
    git --git-dir="$TICKET_BOARD_COMMIT_GIT_DIR" rev-parse refs/remotes/origin/main

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
7. Compare canonical GitHub main, the source cache, shared install, and live
   board release. Merging is not deploying; confirm any intended deployment by
   its behavior and `build_id`, not merely a healthy process.
8. Verify the GitHub branch/PR/audit flow and both commit-verification checks
   end to end. During migration, preserve pending bundles and record any
   credential, workflow, or cache blocker for coordinated director action.
