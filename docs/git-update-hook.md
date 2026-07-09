# Git Update Hook

PGU's bare repo uses a server-side `update` hook for push-time policy:

- reject direct pushes to `refs/heads/main` unless the director override is set
- evaluate only files changed by the pushed tip revision and create at most one
  persistent ticket per oversized file
- refresh `/tmp/pgu-stage3b-demo` when `refs/heads/main` changes the render
  surface (`galaxy*.h`, `galaxy*.inl`, `main.cpp`, `renderer*.h`,
  `shaders/**`, `camera_controller.h`)

Install or refresh the live hook:

```bash
scripts/install-update-hook.sh
```

The hook is intentionally server-side. Panes cannot skip it with `--no-verify`,
and it runs in the same bare-repo location already used for the main-branch
guard.

The file-size path is warning-only: pushes still succeed, but the board gets an
open ticket with the offending path, observed line count at the pushed tip,
commit, and ref. Persistent markers live under the bare repo's hooks directory
so the same oversized file does not keep reopening tickets on later pushes.

The stage3b demo refresh path is also warning-only. It maintains a dedicated
main-synced checkout, rebuilds it incrementally, reinstalls the `/tmp` demo
bundle, and appends actions to the hook log under the bare repo's hooks
directory.
