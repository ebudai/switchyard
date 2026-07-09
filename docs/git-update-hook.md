# Git Update Hook

PGU's bare repo uses a server-side `update` hook for push-time policy:

- reject direct pushes to `refs/heads/main` unless the director override is set
- evaluate only files changed by the pushed tip revision and create at most one
  persistent ticket per oversized file

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
