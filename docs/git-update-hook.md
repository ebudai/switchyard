# Git Update Hook

PGU's bare repo uses a server-side `update` hook for push-time policy:

- reject direct pushes to `refs/heads/main` unless the director override is set
- create one ticket per unique `file + line_count` when a pushed commit
  introduces or grows a tracked source file past the soft line limit

Install or refresh the live hook:

```bash
scripts/install-update-hook.sh
```

The hook is intentionally server-side. Panes cannot skip it with `--no-verify`,
and it runs in the same bare-repo location already used for the main-branch
guard.

The file-size path is warning-only: pushes still succeed, but the board gets an
open ticket with the offending path, observed line count, commit, and ref.
