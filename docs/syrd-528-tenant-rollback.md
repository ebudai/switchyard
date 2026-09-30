# SYRD-528: an upgrade's way back is the tenant's, not the host's

## What was wrong

Before replacing anything, an upgrade writes root's rollback note. The note
recorded the host's shared pointer (`/opt/switchyard/current`) as "previous".
From the note, `release_rollback_commands` printed:
```
sudo ln -sfn <that release> /opt/switchyard/current
sudo switchyard upgrade <project> --source-repo <that release> --deploy-ref <its commit> --publish-remote <url>
```

A tenant upgrade restages the TENANT: its tooling, its grant and its board.
It never moves the shared pointer; only the operator's
`install-shared-release` does. So:
- **The first command repointed every tenant on the host,** to undo
  something this upgrade had not done.
- **In the supported order it named the release being left.** The operator
  installs the shared release, then the Director upgrades, so MEFP's way back
  from 7fe0aee was 7fe0aee.
- **If the host moved during the upgrade,** it went to the host's old release,
  never the tenant's own.

The note already held the tenant's earlier release (`previous_staged_commit`,
from its staged tooling marker), and nothing used it.

Reproduced on main 4e91233 with the real functions in a sandbox:
- host installed first: `ln -sfn …/<new>` plus `--deploy-ref <new>`;
- host switched during the upgrade: the host's old commit, with no mention of
  the tenant's.

## Three different things, told apart

| | what it is | changed by a tenant upgrade? |
|---|---|---|
| host shared release | `/opt/switchyard/current`, every tenant's launcher | no: `install-shared-release`, and undone by `install-shared-release --rollback` |
| tenant release | the release its staged tooling and grant came from, and its root pin | yes |
| board build | the tenant's deployed `current` (`.pgu-deploy-sha`) | yes, with forward-only database migrations |

The note now also records `previous_board_commit`, read through the
launcher's `_current_tenant_release`. When it cannot be read, it is `""`,
never a guess.

## The way back now

`release_rollback_commands` still returns a list. Lines starting with `#` are
what an operator must know, not commands:
- **The host's release** is named, as every tenant's and not part of this way
  back.
- **When root holds the tenant's earlier release,** meaning
  `<shared root>/releases/<commit>` exists with a marker naming that commit, it
  prints the admin-authenticated catalogue action. It prints the dry run
  first, then the action, preceded by what that does to the board:
  ```
  # this redeploys <project>'s board at <commit> (it was on <build>). Database migrations the newer release applied are not reversed, and running the older board against them is not verified: confirm before applying
  switchyard privileged-action <project> select-shared-release commit=<commit> --dry-run
  switchyard privileged-action <project> select-shared-release commit=<commit>
  ```
  `select-shared-release` is unchanged. It resolves the commit against root's
  own release cache (`trusted_release_root`), takes no caller path, and keeps
  the recorded publication remote and commit cache (see SYRD-529).
  `publish_remote` is still accepted, and changes nothing.
- **When no way back can be proved,** it says so and prints no command:
  - no earlier release was recorded;
  - root holds no installed release of that commit, or its marker names
    another;
  - the tenant was already on the target.

**Unchanged:**
- when the note is written (before the first replacement);
- a retry of the same upgrade keeping the note from when the host was whole;
- a dry run recording nothing;
- every pin and release check.

## Tests that encoded the defect

- **`release_rollback_boundary_test`** (SYRD-378's move guard):
  - its read counts gain the three new launcher reads;
  - the `Host` fixture gains stand-ins for them;
  - the expected note gains `previous_board_commit`;
  - its commands case pinned the `ln -sfn` output verbatim, and now pins the
    behaviour above.
- **`release_bootstrap_rollback_test`** asserted an `ln -sfn` line, and now
  requires the tenant's own release. Its fixture wrote the staged marker at
  `<root>/staged`, but the upgrade reads `<staging root>/<project>`, so the
  note's staged commit was always empty. It is renamed `demo`.
- **`shared_release_boundary_test`**: its read list for
  `release_rollback.py` gains `_read_switchyard_release_marker`.

## Evidence

- **`tests/tenant_rollback_note_test.py`.** It runs the real
  `record_release_rollback` and `release_rollback_commands` in a sandbox:
  shared install root, root's provision root, a staging root, and a board
  root reached through the config's pane launcher. Every printed command is
  read by the real privileged-action parser and catalogue. Main's code, run
  in a child from a git worktree, reproduces the defect. The cases are:
  - host installed first;
  - host switched during the upgrade, with distinct host, tenant and board
    commits;
  - a retry after a failed phase;
  - the tenant's release not installed, or its marker disagreeing;
  - no prior proof;
  - a dry run.

  It passes both under `env -i` and in a role pane.
- **Mutation, with `tests/bounded_run.py mutate`, against the three rollback
  suites: 10 of 10 killed.** Each kill was confirmed from its assertion line.
  The mutants cover:
  - the host's release as the target;
  - naming a release root does not hold, or with any marker;
  - no missing-proof branch;
  - the board consequence unsaid;
  - no dry run;
  - the board build not recorded;
  - "already on it" unsaid;
  - the host release unnamed;
  - the host pointer back in the way back.
- **Sweep against 4e91233.** 59 suites were run under `env -i`, with refusing
  stubs for claude, codex, agy and konsole; no stub was reached. They are the
  SYRD-529 upgrade, release and privileged-action set plus the rollback
  suites. 46 pass on both trees and 13 fail identically.
  `atomic_files_boundary_test` and `privileged_front_door_test` fail on main
  and match case for case.

Not done here: no live rollback. Whether an older board runs against a
database a newer release has migrated is stated as unverified, not claimed.
