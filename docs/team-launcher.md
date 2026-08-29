# Switchyard Launcher

`switchyard` is the public project entrypoint. With no arguments it prints
`new...` followed by configured projects. `switchyard new` starts new-project
setup, `switchyard stop <project>` stops only that project's configured tmux
pane sessions, and `switchyard My Project Name` joins all arguments, matches an
existing project case-insensitively by display name or slug, and runs today's
idempotent start/resume behavior. Leading verbs are reserved, so a project
literally named `new`, `upgrade`, or `stop` collides with the command namespace.

`scripts/team-launcher` remains the compatibility wrapper used by existing PGU
pane commands. It starts, attaches, or reloads a project team from a JSON config.
The default PGU config is `config/team-launcher/pgu.json`. PGU starts six
visible roles in a single Konsole layout window and starts `research` as a
detached background tmux session. Each role pane starts in the shared checkout
at `/home/agent/Projects/pgu`; feature work still happens in explicit per-ticket
worktrees created outside the launcher.

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

## Installing the Entry Point

The shared checkout keeps the real `switchyard` implementation. Install only a
system-path trampoline, so launcher self-deploys continue to update the command
the next time it runs and Eric does not need traverse access to `/home/agent`.
The privileged host step for Eric is:

```bash
sudo env SWITCHYARD_SOURCE_PATH=/home/agent/Projects/pgu/switchyard SWITCHYARD_INSTALL_PATH=/usr/local/bin/switchyard /home/agent/Projects/pgu/scripts/install-switchyard --apply
command -v switchyard
sudo env sh -c 'command -v switchyard'
```

`/usr/local/bin` is deliberately used because it is on the host's normal shell
`PATH` and should also be present in sudo's `secure_path`. When `sudo -V`
reports a `secure_path`, the installer refuses a system install path that is not
listed there; when sudo reports no such setting, it proceeds. The installed
wrapper lives outside `/home/agent`; when invoked without sudo, it re-execs the
live checkout entrypoint through `sudo`, so traversal into the shared checkout
happens only after privilege is acquired.

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
scripts/team-launcher pgu stop
switchyard upgrade otto
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

`stop` kills each configured role's named tmux session as the project runtime
user and leaves board/listener systemd services alone. It never runs
`tmux kill-server`; absent role sessions are reported as already stopped, so a
second `switchyard stop <project>` or `scripts/team-launcher <project> stop`
is a clean no-op.

`switchyard upgrade <project>` updates safe generated project artifacts and
checks the provisioned tenant board release. For generated projects whose pane
launcher comes from `<project>-ticketboard-live/current`, it reports the old
deployed release, resolves the target release ref (default `origin/main`), and
prints the privileged `ticket-board-service.sh deploy` command an operator can
run to advance the tenant's `current` symlink. Running it again after that
deploy reports the release unchanged. The command deliberately does not restart
the board service or any panes; panes must be restarted after a release update
to pick up hook installer, hook binary, or pane launcher changes because hook
installation runs at pane launch.

Resume records are preflighted against each CLI's local transcript store before
the launcher passes them to the CLI. Claude uses the recorded
`payload.transcript_path` when present, otherwise the cwd-scoped
`~/.claude/projects/<cwd-key>/<id>.jsonl` path. Codex uses the recorded
`payload.transcript_path` when present, otherwise
`~/.codex/sessions/**/*-<id>.jsonl`. Agy uses
`~/.gemini/antigravity-cli/conversations/<id>.db` or `brain/<id>`;
`~/.gemini` is its storage path, not a supported launcher CLI name. If the local
store entry is missing, the launcher moves the stale record aside, starts fresh,
and prints an explicit warning instead of passing a doomed resume id.

That preflight does not prove model context survived. The PGU-602 reboot showed
an inspector conversation where agy found the conversation, logged a successful
resume and redraw, but the model still reported no prior context. After a real
reboot or hard interruption, treat agy inspector context as unproven
until the pane itself is asked a context-retention question. Claude and Codex
resume attempts can also still fail after a store preflight; if the launched
tmux session exits immediately, the launcher falls back to a fresh session and
reports the superseded id.

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
successful local resume. When reproducing agy resume behavior from inside
a team pane, isolate the hook environment or the hook can write this pane's live
state through the inherited pane target/session environment: run with
`env -u TMUX -u TMUX_PANE`, set `TICKET_BOARD_PANE_TARGET`,
`TICKET_BOARD_PANE_STATE_DIR`, and `TICKET_BOARD_PANE_SESSION_DIR` to disposable
values, and set `TICKET_BOARD_CALLER_ROLE` explicitly. The board write client
refuses writes when neither `--caller-role` nor `TICKET_BOARD_CALLER_ROLE` is
set.

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
- `cli`: argv array for the CLI binary. The only supported CLI names are
  `claude`, `codex`, and `agy`; invalid names, including the retired `gemini`
  launcher name, are rejected while parsing project artifacts or prompt answers,
  before provisioning or launch mutates the project.
- `model`: optional model selector. The launcher passes it with the role's
  `model_arg` at process launch so the pane starts on its configured model.
- `model_arg`: optional model flag, defaulting to `--model`.
- `effort`: optional effort selector. Claude receives it as `--effort <value>`;
  Codex receives it as `-c reasoning_effort=<value>`; Agy omits a separate
  effort flag.
- `yolo`: optional boolean. When true, the launcher appends the appropriate
  bypass-permissions flag for the configured CLI: Agy and Claude use
  `--dangerously-skip-permissions`; Codex uses
  `--dangerously-bypass-approvals-and-sandbox` plus
  `--dangerously-bypass-hook-trust`.
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
The write client no longer derives a caller role from the surrounding tmux pane;
transient shells must pass `--caller-role` or set `TICKET_BOARD_CALLER_ROLE`
before performing ticket writes.

Do not put guessed/default roles into a real project config. If a role is not
currently active or its CLI/model assignment is unknown, omit it until the
assignment is verified. The PGU config intentionally omits `perf` because that
pane is not currently running. PGU keeps `research` configured but detached so
the visible launch window remains a six-pane operator layout.

## New Project

Run new-project setup through the zero-parameter surface:

```bash
sudo switchyard new
```

`new` prompts for project name first, then derived-but-editable slug, agent
name, project path, and the team roles:

```text
Project name: Otto Scheduler
Slug [otto_scheduler]:
Agent user [otto_scheduler-agent]:
Project path [/home/otto_scheduler-agent/Projects/otto_scheduler]:
Include designer role [Y/n]:
designer CLI (claude/codex/agy) [claude]:
director CLI (claude/codex/agy) [claude]:
Include audit role [Y/n]:
audit CLI (claude/codex/agy) [claude]:
Implementer roles (comma-separated): app, code-review
app CLI (claude/codex/agy) [codex]:
code-review CLI (claude/codex/agy) [codex]:
```

The default project path follows the project name, not the machine slug; editing
the slug to `otto` still defaults to `Projects/otto_scheduler`. The path itself
is editable for existing checkouts or shared locations. If an existing path is
not readable and writable by the project agent user, `new` refuses before
creating users or files. If `new` is not run with sudo/root privileges, it fails
before creating files or users. The project name `new` is reserved for the
command; a project literally named "New" must be opened by its slug from the
project list.

After confirmation, `new` creates the selected agent user, creates the derived
project directory, provisions the board, runs first-run auth checks for the
selected CLIs, and launches the configured panes. The default agent user is
`<slug>-agent`; a typed agent-user value is used verbatim. If that user already
exists, `new` warns and asks for explicit confirmation, defaulting to No, before
reusing that account. Pass `--allow-existing-owner-user` to skip that
existing-user confirmation and reuse the account without prompting. In
non-interactive runs, `new` refuses an existing owner user unless that flag is
present. `--yes` is unrelated to this check; it skips the project-plan
confirmation. `director` is a fixed workflow role. `designer` is optional; when
omitted, `new` skips the design phase and does not create a designer pane,
designer onboarding file, or initial design document. `audit` is optional; when
omitted, the generated workflow skips the Audit stage and implementers submit
directly to Final Sign-Off. Implementer role names are supplied as a
comma-separated list and may contain hyphens. The selected role names are
written to both the board provisioning plan and the generated launcher config,
and each role's selected CLI is recorded in the `switchyard.project.v1`
artifact. The artifact still must not preconfigure workflow stages or
transitions.

The first-run auth phase runs before panes launch. It probes each distinct
selected CLI as the project owner, invokes that CLI's own login command when it
is unauthenticated, reports a missing CLI as not installed for that owner user,
and then walks per-worktree trust prompts. Trust is never pre-seeded by editing
CLI config files. Codex hook-trust entries are also checked against the
installed pane-hook commands; stale trust hashes are reported as warnings with
the affected role/event so the operator can refresh trust deliberately instead
of silently launching panes with inert hooks.

The same provision path consumes generated and hand-written artifacts:

```bash
sudo switchyard new --from /path/to/porter.project.json --yes
```

The artifact's `repository` is the project checkout opened by the generated team
panes. The generated director role receives onboarding paths in its environment;
the director owns the unprivileged half after the board exists: seed the board,
launch panes, onboard the team, and reshape stages or roles later if needed.
