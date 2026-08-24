# Team Launcher

`scripts/team-launcher` starts, attaches, or reloads a project team from a JSON
config. The default PGU config is `config/team-launcher/pgu.json`. PGU starts
six visible roles in a single Konsole layout window and starts `research` as a
detached background tmux session. Each role pane starts in the shared checkout
at `/home/agent/Projects/pgu`; feature work still happens in explicit
per-ticket worktrees created outside the launcher.

The executable wrapper runs as the user who invoked it by default. A project
may opt into a dedicated runtime account with `run_as_user`; when that key is
set and the invoker is different, the wrapper re-execs through
`sudo -u <run_as_user> -H`. The PGU config uses `run_as_user: agent` so Eric's
production launch keeps the existing agent-owned tmux sessions, pane hook
state, and shared checkout. Projects without `run_as_user` use the invoking
user's runtime pane-state directory and `$HOME/bin` prepended to pane CLI
`PATH`. Resume session records are durable state under the runtime user's home,
not `/run`.
Real `start` and `reload` launches verify that the selected runtime user has
linger enabled and a `/run/user/<uid>` directory before starting tmux panes. If
host policy denies that setup, the launcher fails before touching panes and
prints the `sudo loginctl enable-linger <user>` command an operator must run.

When opening the visible Konsole window, the launcher uses
`PGU_HOST_WAYLAND_DISPLAY` if it is set. Otherwise it resolves the display for
`PGU_TEAM_LAUNCHER_GUI_USER`, or for the invoking user when that variable is
unset:

```bash
env QT_QPA_PLATFORM=wayland \
  QT_LOGGING_RULES=qt.qpa.wayland.warning=false \
  WAYLAND_DISPLAY=/run/user/<uid>/wayland-0 \
  konsole --layout ...
```

The commands inside the Konsole layout call `scripts/team-launcher pane ...`
as the same user, so visible panes attach to that user's tmux sessions.
Set `PGU_TEAM_LAUNCHER_BIN_DIR` to override the default `$HOME/bin` PATH
prefix, and set `PGU_TEAM_LAUNCHER_GUI_USER` or
`PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY` when the Konsole display should be resolved
from a different local desktop account or socket name.

Before any real `start`, `reload`, or direct `pane` launch, the launcher checks
the checkout containing `scripts/team-launcher` against the configured
`worktree_remote`/`worktree_branch` ref. It refuses to start when that launcher
checkout is behind the ref, because otherwise merged launcher/config fixes can
silently fail to reach the live team. `--dry-run` does not perform this fetch or
verification.

## Modes

```bash
scripts/team-launcher pgu start
scripts/team-launcher pgu attach
scripts/team-launcher pgu reload
scripts/team-launcher pgu provision-runtime
scripts/team-launcher pgu deploy-launcher --launcher-repo /home/eric/Projects/pgu
```

`start` is intentionally idempotent. Each visible Konsole pane runs the launcher
in `pane attach-or-start <role>` mode. If `tmux has-session -t pgu-<role>`
succeeds, it attaches to that existing session. It only runs `tmux new-session`
when the role session does not exist. Roles marked `detached` are started
directly by the launcher before Konsole opens and are not assigned a visible
layout pane. The visible Konsole window is then launched in the background so
`start` returns promptly to the invoking shell with a short status line instead
of blocking on the window process. Before opening Konsole, `start` fetches
`origin main`, checks the shared checkout out to `origin/main` with `--force`,
then runs `git clean -fdx`.
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
`~/.local/state/pgu-ticket-board/pane-sessions/<target>.json` by default. The
SessionStart hook installer writes those durable files, and the launcher exports
a known resume id to the CLI hook environment when recreating a pane. The old
`/run/user/<uid>/pgu-ticket-board/pane-sessions` location is only a one-time
migration source for older records; it is not repopulated after boot.
When the launcher must start a role fresh, it removes the live resume record
from the first-writer-wins path by renaming it to `<target>.json.superseded`
instead of deleting it. The new pane can still claim the live filename, while
the previous conversation id remains available for recovery and diagnosis.

Reload is guarded because it is destructive. Before killing an existing tmux
session, the launcher reads `#{pane_current_command}` for that role target and
requires it to match the configured `live_commands` list, or the configured
`cli` binary name when `live_commands` is omitted. A stale config fails safe and
does not kill the pane. Use `--force` only when intentionally overriding that
guard.

Antigravity/Gemini resume has an additional limitation. On Antigravity CLI
1.1.19, `agy --conversation <id>` is the correct flag and a cleanly-created
throwaway conversation resumed successfully in print-mode testing. An unknown
conversation id, however, only prints a warning, exits 0, and starts a fresh
conversation. To avoid treating that silent fallback as a resumed inspector, the
launcher preflights `agy`/`gemini` `--conversation` resumes against the local
Antigravity store (`~/.gemini/antigravity-cli/conversations/<id>.db` or
`brain/<id>`). If the store entry is missing, it starts fresh with an explicit
warning instead of passing a doomed `--conversation` flag.

That preflight does not prove model context survived. The PGU-602 reboot showed
an inspector conversation where agy found the conversation, logged a successful
resume and redraw, but the model still reported no prior context. After a real
reboot or hard interruption, treat agy/gemini inspector context as unproven
until the pane itself is asked a context-retention question. Claude `--resume`
and Codex `resume <id>` did not show this failure in the same reboot.

The PGU-613 follow-up diagnostics separated three cases:

- A clean throwaway agy 1.1.19 print-mode conversation resumed with
  `--conversation <id>` and the model returned a sentinel from the previous
  turn.
- A deliberately unknown id exited 0 after printing a warning, then ran with
  `conversationID=""` and created a new conversation.
- The failed PGU-602 inspector id had both
  `conversations/<id>.db` and `brain/<id>` present, the database contained
  step rows, the brain directory contained transcript files, and the 07:04 log
  showed `GetConversationDetail`, `Resuming conversation`, `Streaming
  conversation`, and `Full redraw completed`; despite that, the model answered
  `NO PRIOR CONTEXT`.

So the launcher can guard against missing-store silent fallback, but the real
inspector failure is a lower-level agy context propagation failure after a
successful local resume. When reproducing agy/gemini resume behavior from inside
a team pane, isolate the hook environment or the hook can write this pane's live
state through tmux's inherited `TMUX_PANE`: run with `env -u TMUX -u TMUX_PANE`
and set `TICKET_BOARD_PANE_TARGET`, `TICKET_BOARD_PANE_STATE_DIR`, and
`TICKET_BOARD_PANE_SESSION_DIR` to disposable values.

`provision-runtime` is the non-launching project-user setup check. It enables
linger for the configured `run_as_user`, or for the invoking user when the
project has no dedicated runtime account, and waits for `/run/user/<uid>` to
appear. Use `--runtime-user <user>` to provision a user before a project config
exists or when standing up a sandbox account directly:

```bash
scripts/team-launcher porter provision-runtime --runtime-user otto-agent
```

The normal `start`/`reload` launcher freshness check uses `git ls-remote` and
local read-only comparisons, so it does not write fetched objects into the
launcher checkout during the hot-path probe. If the remote tip is not already in
the local object database the stale warning reports "at least 1 commit" behind;
the exact count is available only when the object already exists locally.

`deploy-launcher` is the explicit launcher deploy step. It fetches the
configured ref in the launcher checkout, checks out the configured branch,
fast-forwards it to the ref, and verifies it is no longer behind before a later
restart uses it:

```bash
scripts/team-launcher pgu deploy-launcher --launcher-repo /home/eric/Projects/pgu
```

Run it only during an approved restart window for that project. It updates the
checkout that future launcher invocations will execute; it does not restart or
reload any panes by itself. Add `--clean-launcher` only when intentionally
discarding untracked files from that launcher checkout.

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
- `run_as_user`: optional dedicated local user for this project. Omit it for
  ordinary invoking-user teams; set it only when an existing deployment must
  remain on a specific user-owned tmux/runtime namespace.
- `session_dir`: optional directory containing per-pane resume session files.
- `board_url`: optional project ticket-board HTTP URL. Defaults to
  `http://127.0.0.1:8770` for PGU, and otherwise to the same deterministic
  per-project port used by the ticket-board provisioner.
- `board_socket`: optional project ticket-board Unix write socket. Defaults to
  `/run/<project>-ticket-board/ticket-board.sock`.
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

The launcher injects the board wiring into every role process:
`TICKET_BOARD_PROJECT`, `TICKET_BOARD_URL`, `TICKET_BOARD_SOCKET`,
`TICKET_BOARD_CALLER_ROLE`, and `TICKET_BOARD_CALLER_ROLE_MAP`. These generated
values override stale per-role entries so every pane writes to the configured
project board through the write client instead of falling back to PGU defaults.

Do not put guessed/default roles into a real project config. If a role is not
currently active or its CLI/model assignment is unknown, omit it until the
assignment is verified. The PGU config intentionally omits `perf` because that
pane is not currently running. PGU keeps `research` configured but detached so
the visible launch window remains a six-pane operator layout.

## New Project

For a new project, first write the design document and post-design project
artifact:

```bash
scripts/team-launcher porter design \
  --design-output-dir /tmp/porter-design \
  --repository /home/otto-agent/Projects/porter \
  --owner-user otto-agent \
  --ticket-prefix PORT
```

`design` does not create users, start panes, touch tmux, provision a board, or
run privileged commands. It writes `<slug>-design.md` plus
`<slug>.project.json`. The artifact records the post-design answers: code
location, remote, default branch, worktree policy, slug, ticket prefix, owner
user and capability grants, push policy, and project gates. It deliberately
does not ask for stages or extra implementer roles; new projects start with the
default five-stage workflow and are specialized later by the live director.

Then generate a complete project config and provisioning plan from that
reviewed artifact:

```bash
scripts/team-launcher porter new \
  --from /tmp/porter-design/porter.project.json \
  --source-repo /home/agent/Projects/pgu \
  --new-output-dir /tmp/porter \
  --dry-run
```

The dry run writes a launchable config, Konsole layout, board provisioning SQL,
systemd units, and `operator-commands.sh` without running privileged steps. Review
the generated artifacts, then rerun with `--execute` when the project should be
provisioned.

`--source-repo` is the checkout that exports the board and launcher tooling for
provisioning. The artifact's `repository` is the project checkout opened by the
generated team panes, and it must already exist. Dry-run rendering can happen
before the owner user exists; `--execute` still requires the owner user to
exist until user creation is implemented in `new`.
