# Git Repository Policy Hooks

Switchyard-managed repositories use two complementary hooks:

- `update` rejects direct pushes to `refs/heads/main` unless the director sets
  `ALLOW_MAIN_PUSH=director` (or PGU's legacy `PGU_ALLOW_MAIN_PUSH=director`).
  The managed wrapper preserves and invokes any pre-existing update hook, so
  PGU's Stage-3b refresh remains intact. It translates the generic override for
  the preserved legacy PGU guard.
- `pre-commit` warns when a staged source file exceeds 1,250 lines. This is the
  deliberate soft limit retained at Eric's direction: it reports maintenance
  risk but never blocks a commit. Existing pre-commit behavior is preserved.

Worktrees share their repository's common Git directory, so one pre-commit
installation covers every worktree attached to that repository. `switchyard
new` installs policy into both the owner checkout and the shared control
repository after provisioning. The installer pins that common hook directory
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

For the bounded host-wide rollout (all `/data/git/*.git`, registered project
configs, and the two PGU checkouts), run:

```bash
sudo scripts/install-all-repository-hooks.sh
```

Use `--dry-run` first to enumerate the exact commands. Tenant homes are not
searched: their paths come from the root-owned Switchyard registry.

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
