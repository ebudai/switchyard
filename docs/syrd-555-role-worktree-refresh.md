# SYRD-555: a launch never discards a role worktree's work

## What happened

Otto, 2026-10-04 21:03:15 EDT, after a PostgreSQL restore. With no role session
live, `switchyard otto` refreshed all six role worktrees:
`git reset --hard origin/main` and then `git clean -fdx`.
- Two roles had their packet branches checked out. The reset moved those
  branches, so two finished packets left them (`a8dcaed`, 21 files;
  `824c579`, 56 files).
- `-x` deleted every git-ignored `local.properties` and every build cache.
- The only safeguard printed a warning and always went on. It never compared
  the branch with the ref.

The packets survived only because otto's own rules push packet branches before
submission.

## The rule

Before a stopped role's worktree is refreshed, the launcher reads it. It leaves
the worktree exactly as it is when the refresh would lose:
- **commits on the checked-out branch** that the ref does not have
  (`git rev-list --count <ref>..HEAD`);
- **commits a detached HEAD holds alone**, reachable from no local or
  remote-tracking branch and not from the ref;
- **tracked changes**;
- **untracked files**.

Ignored files are not counted as work to refuse over: they are never deleted.

**The role still starts, on its kept tree.** Work in progress is normally ahead
of origin/main, so refusing the role itself would stop it at every launch until
its work merged. That was otto's situation: finished packets that had not
merged yet. A role whose session is live is not refreshed at all, as before.

A worktree that cannot be read (a git error, or a commit count that is not a
number) refuses too. That role does not start, as with today's check failures.

**What the operator sees:**
- while worktrees are prepared, one line per kept worktree, naming:
  - the branch and the commit count ("branch pack/8 has 1 commit not in
    origin/main");
  - the paths;
  - the remedy;
- at the end of the launch, after the session report, a summary of every
  worktree kept and why. It is printed only when something was kept.

## The refresh

`git checkout --detach <ref>`. It never runs `reset --hard`, so no branch is
ever moved and a branch keeps its commits. It also runs no clean: a worktree
that is refreshed has no untracked files, because otherwise it would have been
kept.

## The override

`switchyard start <project> --discard-worktree-changes <role>` (repeatable). It
applies to the named stopped roles, for that launch only:
- **What it discards.** Tracked changes (`checkout --detach --force`) and
  untracked files (`git clean -fd`, never `-x`).
- **What it allows.** HEAD may leave a branch that is ahead; the branch keeps
  its commits.
- **What it never touches.** Ignored files stay. A detached HEAD whose commits
  are on no branch is still refused: give it a branch first
  (`git -C <worktree> branch <name>`).
- **Unknown roles.** A role the project does not have stops the launch before
  any worktree or role is touched.

The flag is on `switchyard start`, which has its own argument parser. The bare
`switchyard <project>` form reads all of its arguments as the project name.

## Not changed

The legacy shared-checkout path (a `repository` with no control repository)
still runs `checkout --force` and `clean -fdx` behind a warning. The ticket and
the incident are about role worktrees. That path can follow as its own ticket.

## Tests

`tests/role_worktree_refresh_test.py` runs the launcher's refresh against real
git: a project repository, its bare control repository and role worktrees
created by `ensure_control_role_worktrees`. It covers:
- an ahead branch that is kept while the other role refreshes;
- untracked plus ignored files: refused, then discarded with the override, with
  `local.properties` and build output intact;
- tracked edits;
- the override moving HEAD off an ahead branch while the branch keeps its
  commit;
- a detached commit on no branch, kept with or without the override, then
  refreshed once it has a branch;
- a clean worktree refreshed by `checkout --detach` alone;
- a running role left alone;
- the printed remedy parsed by the real `switchyard start` parser;
- the plumbing from `switchyard start` through `launch_project` and P5 to the
  worktrees, and the summary as the launch's last lines.
