# Team Launcher

`scripts/team-launcher` starts, attaches, or reloads a project team from a JSON
config. The default PGU config is `config/team-launcher/pgu.json`. PGU starts
six visible roles in a single Konsole layout window and starts `research` as a
detached background tmux session. Each role pane starts in the shared checkout
at `/home/agent/Projects/pgu`; feature work still happens in explicit
per-ticket worktrees created outside the launcher.

The executable wrapper self-runs as the `agent` user. If another host user runs
`scripts/team-launcher`, the wrapper invokes `sudo -u agent -H` and passes the
same launcher arguments through, so callers do not need to prefix the command
manually.

Before self-elevating, the wrapper captures the invoking desktop's Wayland
socket as an absolute path in `PGU_HOST_WAYLAND_DISPLAY`. The `agent` launcher
then opens the visible Konsole window with only the Konsole subprocess forced
onto Wayland:

```bash
env QT_QPA_PLATFORM=wayland \
  WAYLAND_DISPLAY=/run/user/<eric-uid>/wayland-0 \
  konsole --layout ...
```

This avoids changing the agent process's own `XDG_RUNTIME_DIR`, which tmux and
the board services still need to keep pointing at the agent runtime. The
commands inside the Konsole layout still call `scripts/team-launcher pane ...`,
so the wrapper self-elevates those pane commands back to `agent` before tmux
attach/start when needed.

## Modes

```bash
scripts/team-launcher pgu start
scripts/team-launcher pgu attach
scripts/team-launcher pgu reload
```

`start` is intentionally idempotent. Each visible Konsole pane runs the launcher
in `pane attach-or-start <role>` mode. If `tmux has-session -t pgu-<role>`
succeeds, it attaches to that existing session. It only runs `tmux new-session`
when the role session does not exist. Roles marked `detached` are started
directly by the launcher before Konsole opens and are not assigned a visible
layout pane. Before opening Konsole, `start` fetches `origin main`, checks the
shared checkout out to `origin/main` with `--force`, then runs `git clean -fdx`.
This is intentionally destructive to uncommitted files in the shared checkout;
use a ticket worktree for implementation work. A shared checkout refresh failure
blocks every role: visible panes print the checkout error instead of launching
the CLI, while detached roles are skipped.

`attach` only attaches visible panes to existing sessions and fails if a role
session is missing. Detached roles are checked for existence but remain
detached.

`reload` kills and recreates each configured role session, then starts the
configured CLI with its recorded resume id when one exists. Before restarting a
running role, the launcher compares the config's `cli` and `model` against the
live pane. The CLI is inferred from the pane process tree, and the model is
read from the live process argv when available, falling back to the latest
SessionStart hook record. If either value differs, reload rewrites only that
role's `cli` and `model` fields atomically so the live choice becomes the new
default for future reloads. Detached roles are also reloaded, but remain
detached instead of attaching to a pane. `reload` fetches the configured
worktree ref for freshness, but does not checkout or clean the shared checkout
before recreating sessions; this preserves files in resumed panes. Resume ids
are read from
`/run/user/<uid>/pgu-ticket-board/pane-sessions/<target>.json` by default. The
SessionStart hook installer writes those files.

Reload is guarded because it is destructive. Before killing an existing tmux
session, the launcher reads `#{pane_current_command}` for that role target and
requires it to match the configured `live_commands` list, or the configured
`cli` binary name when `live_commands` is omitted. A stale config fails safe and
does not kill the pane. Use `--force` only when intentionally overriding that
guard.

Use `--dry-run` to render the Konsole layout and print the plan without opening
Konsole:

```bash
scripts/team-launcher pgu start --dry-run --layout-output /tmp/pgu-team-layout.json
```

## Config Shape

Each project config contains:

- `project`: project name, such as `pgu`.
- `layout`: Konsole JSON layout path, relative to the config file if not
  absolute.
- `session_dir`: optional directory containing per-pane resume session files.
- `repository`: optional shared checkout used as the default pane cwd and the
  checkout refreshed by `start`.
- `worktree_base`: legacy optional setting retained for existing configs. It is
  not used as the pane cwd when `repository` is set.
- `worktree_remote`: optional remote for the shared checkout, defaulting to
  `origin`.
- `worktree_branch`: optional branch for the shared checkout, defaulting to
  `main`.
- `roles`: list of role entries.

Each role entry contains:

- `role`: board/team role name.
- `slot`: zero-based terminal leaf in the Konsole layout. Required for visible
  roles and forbidden for detached roles.
- `detached`: optional boolean. When true, the role is started/reloaded as a
  background tmux session and omitted from the Konsole layout.
- `tmux_session`: session name, normally `<project>-<role>`.
- `target`: tmux target, normally `<project>-<role>:0.0`.
- `workdir`: working directory for the tmux session. Omit it for PGU roles so
  they default to the shared `repository` checkout. Duplicate role workdirs are
  allowed only when the duplicate path is the shared checkout.
- `cli`: argv array for the CLI binary.
- `model`: optional model selector. The launcher passes it with the role's
  `model_arg` at process launch so the pane starts on its configured model.
- `model_arg`: optional model flag, defaulting to `--model`.
- `effort`: optional effort selector. Claude receives it as `--effort <value>`;
  Codex receives it as `-c reasoning_effort=<value>`; Agy and Gemini omit a
  separate effort flag.
- `yolo`: optional boolean. When true, the launcher appends the appropriate
  bypass-permissions flag for the configured CLI: Agy and Claude use
  `--dangerously-skip-permissions`, Codex uses
  `--dangerously-bypass-approvals-and-sandbox` plus
  `--dangerously-bypass-hook-trust`, and Gemini uses `--yolo`.
- `extra_args`: optional additional argv entries appended after the yolo flag.
- `resume_flag`: flag used by that CLI for context resume.
- `live_commands`: optional command names accepted as the live pane process
  before destructive reload.
- `env`: optional environment variables for the CLI process.

Do not put guessed/default roles into a real project config. If a role is not
currently active or its CLI/model assignment is unknown, omit it until the
assignment is verified. The PGU config intentionally omits `perf` because that
pane is not currently running. PGU keeps `research` configured but detached so
the visible launch window remains a six-pane operator layout.

## Bootstrap

Generate a starter config for another project:

```bash
scripts/team-launcher porter bootstrap --template-output /tmp/porter.json
```

The template intentionally contains no active roles. Add roles only after their
live CLI/model assignments are known; copy or adapt the PGU layout/config when
standing up a full multi-role team.
