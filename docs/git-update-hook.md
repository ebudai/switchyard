# Git Repository Policy Hooks

Switchyard-managed repositories use two complementary hooks:

- `update` rejects direct pushes to `refs/heads/main` unless the director sets
  `ALLOW_MAIN_PUSH=director` (or PGU's legacy `PGU_ALLOW_MAIN_PUSH=director`).
  The managed wrapper preserves and invokes any pre-existing update hook, so
  PGU's Stage-3b refresh remains intact. It translates the generic override for
  the preserved legacy PGU guard.
- `pre-commit` warns when a staged source file exceeds 1,250 lines. This is the
  deliberate soft limit retained at Eric's direction: it reports maintenance
  risk but never blocks a commit. It also performs a cheap
  `git worktree list --porcelain` count on every commit and warns when the
  repository has more than 300 registered worktrees. Only after that total
  threshold is crossed does it run the slower cleanup planner to report how
  many worktrees are actually reclaimable, with a separate soft limit of 100
  reclaimable clean merged worktrees. The warning distinguishes total
  directory size from reclaimable cleanup candidates, reports dirty worktrees
  as protected unfinished work, names
  `scripts/ticket_board/worktree_cleanup.py --execute` as the cleanup command,
  and is rate-limited to once per day per repository. Existing pre-commit
  behavior is preserved.

Worktrees share their repository's common Git directory, so one pre-commit
installation covers every worktree attached to that repository. `switchyard
new` installs policy into both the owner checkout and the shared control
repository after provisioning. A fresh source clone gets the same policy from
the top-level `sudo ./install`, which installs it into that checkout after the
rest of the install succeeds -- previously a fresh clone was left without the
hook until a project was provisioned. It is installed as the human who ran the
installer rather than as root, so the hooks and the local Git configuration stay
theirs to change; a `--dry-run` install reports the command and installs nothing.

`switchyard upgrade` repairs the same policy for a project's repositories,
reinstalling hooks that are missing or stale. The repair is idempotent -- running
it over an already-correct checkout leaves the hook byte-identical and does not
accumulate preserved copies -- and it is warning-only, so a repair that cannot
run warns rather than failing the upgrade. The installer pins that common hook directory
in the repository's local `core.hooksPath`; a caller's global Git configuration
therefore cannot bypass or redirect repository policy.

Install or repair policy explicitly with:

```bash
scripts/repository_hooks.py --project example --repository /path/to/repo
```

Use `--local-only` for a working-repository warning or `--server-only` for a
receive-side main guard. The local helper imports path policy from
`report_file_size_limit.py`; that module remains the optional push reporter and
the shared scanner rather than orphaned code.

For the bounded host-wide rollout (platform workflow repositories, registered
project configs, and any explicitly listed shared working checkouts), run:

```bash
sudo scripts/install-all-repository-hooks.sh
```

Use `--dry-run` first to enumerate the exact commands. Tenant homes are not
searched: their paths come from the root-owned Switchyard registry. Shared
working checkouts are not guessed; set `SWITCHYARD_SHARED_CHECKOUTS` to a
colon-separated list when you want warning-only local hooks installed there.
The output labels each target as `FILE-SIZE WARNING ONLY`, `MAIN GUARD`,
`REGISTERED WORKFLOW`, or `NO MAIN GUARD`. Bare repositories without a
Switchyard workflow are deliberately left without a blocking guard; the
rejection message must never point an unmanaged project at a workflow it does
not have.

The privileged installer grants Git trust only for the repository currently
being operated on by passing `-c safe.directory=<exact repository>` to that Git
process. It never writes `safe.directory` globally and never uses the wildcard
form. `tests/repository_hooks_foreign_owner_test.py` performs a complete
foreign-owner installation when run as root. Without root it cannot create or
mutate a foreign-owned fixture, so it exercises Git's real dubious-ownership
rejection and the narrow access fix against an existing foreign-owned bare
repository; writable installation and rerun idempotency remain covered by the
standard repository-hook regression.

## Legacy PGU installer

PGU's historical installer maintains its Stage-3b-aware server-side `update`
hook and the local warning:

- reject direct pushes to `refs/heads/main` unless the director override is set
- refresh `/tmp/pgu-stage3b-demo` when `refs/heads/main` changes the render
  surface (`galaxy*.h`, `galaxy*.inl`, `main.cpp`, `renderer*.h`,
  `shaders/**`, `camera_controller.h`)

Install or refresh the live hook:

```bash
scripts/install-update-hook.sh
```

The main guard is intentionally server-side, so panes cannot skip it with
`--no-verify`. File-size feedback is intentionally local and warning-only.

The stage3b demo refresh path is also warning-only. It maintains a dedicated
main-synced checkout, rebuilds it incrementally, reinstalls the `/tmp` demo
bundle, and appends actions to the hook log under the bare repo's hooks
directory.
