# Team Launcher

`scripts/team-launcher` starts, attaches, or reloads a project team from a JSON
config. The default PGU config is `config/team-launcher/pgu.json`.

## Modes

```bash
scripts/team-launcher pgu start
scripts/team-launcher pgu attach
scripts/team-launcher pgu reload
```

`start` is intentionally idempotent. Each Konsole pane runs the launcher in
`pane attach-or-start <role>` mode. If `tmux has-session -t pgu-<role>` succeeds,
it attaches to that existing session. It only runs `tmux new-session` when the
role session does not exist.

`attach` only attaches to existing sessions and fails if a role session is
missing.

`reload` kills and recreates each configured role session, then starts the
configured CLI with its recorded resume id when one exists. Resume ids are read
from `/run/user/<uid>/pgu-ticket-board/pane-sessions/<target>.json` by default.
The SessionStart hook installer writes those files.

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
- `roles`: list of role entries.

Each role entry contains:

- `role`: board/team role name.
- `slot`: zero-based terminal leaf in the Konsole layout.
- `tmux_session`: session name, normally `<project>-<role>`.
- `target`: tmux target, normally `<project>-<role>:0.0`.
- `workdir`: working directory for the tmux session.
- `cli`: argv array for the CLI binary.
- `model`, `model_arg`: optional model selector appended to `cli`.
- `yolo`: optional boolean. When true, the launcher appends the appropriate
  bypass-permissions flag for the configured CLI: Claude uses
  `--dangerously-skip-permissions`, Codex uses
  `--dangerously-bypass-approvals-and-sandbox`, and Gemini uses `--yolo`.
- `extra_args`: optional additional argv entries appended after the model.
- `resume_flag`: flag used by that CLI for context resume.
- `live_commands`: optional command names accepted as the live pane process
  before destructive reload.
- `env`: optional environment variables for the CLI process.

Do not put guessed/default roles into a real project config. If a role is not
currently active or its CLI/model assignment is unknown, omit it until the
assignment is verified. The PGU config intentionally omits `perf` because that
pane is not currently running.

## Bootstrap

Generate a starter config for another project:

```bash
scripts/team-launcher porter bootstrap --template-output /tmp/porter.json
```

The template intentionally contains no active roles. Add roles only after their
live CLI/model assignments are known; copy or adapt the PGU layout/config when
standing up a full multi-role team.
