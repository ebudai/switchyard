# SYRD-575: a fresh host's first project stages root's artifacts

## What the VM showed

Fresh Arch VM acceptance on 2026-10-10 (SYRD-574, main 84cedf1) printed this at
the first `switchyard new`:

```
switchyard: could not stage uat574 privileged artifacts for root: [Errno 2] No such file or directory: '/etc/switchyard/provision'. Run `switchyard upgrade uat574` as root before installing its units.
```

The evidence is kept in `~switchyard-agent/syrd-574-uat/` (the run log, line
369). Provisioning went on, but root's own copies of the units, grants and SQL
were not staged, and the remedy offered was a full upgrade.

## Why

`ensure_privileged_provision_dir` creates and checks root's provision
directory, `/etc/switchyard/provision/<project>`. It walks the target's parents
but acts only on those inside the provision root. The root's own parent,
`/etc/switchyard`, was outside the walk. On a host that already had a project,
something else had made `/etc/switchyard` long before. On a fresh host nothing
had, so `mkdir /etc/switchyard/provision` failed. Every root writer under the
provision root goes through this function, so `switchyard new`, upgrade's
refresh and the board-authority install all share the gap.

## The change

Before the existing walk, the function now looks at the root's own parents:

- **The anchor.** The nearest parent that exists is judged before anything is
  made. That is `/etc/switchyard` on a host with a project, and `/etc` on a
  fresh one. It is refused if it:
  - is a symlink;
  - is not a directory;
  - belongs to anyone but root, or the caller's real uid through the
    suites' documented provision-root override. On a host the caller is root,
    since `sudo` and `pkexec` set the real uid as well;
  - is writable by group or others.

  A refusal names the path and the reason, and ends "Nothing was written", which
  is true: nothing has been made yet. A sticky directory such as `/tmp` is
  shared by design, so its owner is not asked. Inside a user namespace, root's
  `/tmp` is owned by nobody.
- **Missing parents.** Each missing parent below the anchor is made by root,
  0755, so it is root's. That is the mode `install -d -m 0755 -o root -g root` gives
  `/etc/switchyard/publish`, and tenants read `/etc/switchyard/projects`. The
  umask is 022 for the `mkdir`, so a parent never exists at a narrower mode,
  even for an instant an interruption could land in. Each one is read back
  after it is made, and refused if what is there is not the directory just
  made.
- **The rest is unchanged:** the walk from the provision root down, SYRD-176's
  root-only target, and its repairs.

Only the anchor is judged. Parents above it (`/`, `/etc` on a host with a
project) are not, and neither are the provision root's other ancestors, so a
host's existing layout is never second-guessed.

Dry runs never reach this. `switchyard new` stages root's copies only when it
executes as root, and the command's boundary suite records every case: only the
two executing-root cases call the staging.

## Tests

`tests/root_staging_parent_test.py` runs each case as root in a fresh user and
mount namespace whose `/etc` is an empty, root-owned tmpfs: the fresh host,
without touching this host's `/etc`. It uses the real
`install_privileged_artifacts` through the real default root,
`/etc/switchyard/provision`, with no override. Main c99bfb8 is run the same way
from a git archive.

The cases:
- **On main.** A fresh host fails with the VM's message, word for word, and
  nothing is made. A symlinked `/etc/switchyard` is followed, and root's plan
  is written where the link points. A world-writable `/etc/switchyard` is
  staged under.
- **Fresh host.** `/etc/switchyard` and the provision root are made root's and
  0755, the project directory root-only, and the artifacts staged. This holds
  under umask 077 too.
- **Existing safe tree.** An existing `/etc/switchyard`, its `projects`, the
  provision root and another project's plan are left exactly as they were.
  Only the new project is added.
- **Unsafe parent.** A symlinked, world-writable or foreign-owned
  `/etc/switchyard` is refused, and so is a file in its place. Nothing is
  written, through the link or inside. A fresh host under a world-writable
  `/etc` is refused at `/etc`, before `/etc/switchyard` is made.
- **Swapped while made.** A parent swapped for a link as it is made is caught
  by the read-back.
- **Repeated.** Staging again changes only what was rendered anew.
- **Interrupted and retried.** The first attempt is interrupted after
  `/etc/switchyard` is made. The retry finds it safe and finishes.
- **Dry runs.** Of the `switchyard new` runs its boundary suite records, only
  the two that execute as root reach the staging.

## Comparison with main

82 suites ran serially under `env -i` with refusing model and konsole stubs:
every suite that touches the provision root, the staging or `switchyard new`,
on this tree and on main fad3070, before the rebase onto c99bfb8, whose
changes touch none of this ticket's files. 58 pass on both, and every red here
is red on main with the same message. `publication_boundary_upgrade_test`, for one, fails
on both trees installing role tooling into `/usr/share/polkit-1`.

Nine suites take the root branch by faking an effective uid of 0 while their
directories belong to the caller. They first failed against "root or the
effective uid", which is why the owner rule reads the real uid. On a host that
makes no difference. With it, all nine match main.

## Mutation

10 mutants of the new code, run with `tests/bounded_run.py mutate`:
- parents never made;
- each refusal skipped in turn (symlink, not a directory, foreign owner,
  writable);
- every directory treated as sticky;
- every parent judged instead of only the anchor;
- the caller's umask kept;
- no read-back;
- parents made before judging.

All 10 are killed, each by the check written for it. The last at first died on
the umask check instead. That showed no case refused with a parent still to
make, which is what the fresh-host-under-unsafe-`/etc` case now covers.

## Scope

This is source only. No tenant and no host's `/etc` was changed, and every
case ran in a namespace. The SYRD-574 VM is left as the User Review records
require.
