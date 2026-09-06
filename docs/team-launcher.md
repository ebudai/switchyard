# Switchyard Launcher

`switchyard` is the public project entrypoint. With no arguments it prints
`new...` followed by configured projects. `switchyard --help` lists the public
verbs: `new`, `register`, `upgrade`, `add-role`, `set-vcs-close-role`, `stop`,
`status`, and `validate-models`. `switchyard My Project Name` joins all
arguments, matches an existing project case-insensitively by display name or
slug, and runs today's idempotent start/resume behavior. A bare project name is
the start/attach form; there is no separate `switchyard <project> attach` verb.
Leading verbs are reserved, so a project literally named `new`, `register`,
`upgrade`, `add-role`, `set-vcs-close-role`, `stop`, `status`, or
`validate-models` collides with the command namespace.

The installed `/usr/local/bin/switchyard` trampoline does not elevate top-level
discoverability commands: no arguments, `--help`, and `--version`. Bare project
names enter the Python launcher directly when the target is reachable; after
registry resolution, cross-owner projects re-exec through the same root policy
before reading tenant-owned config or starting panes. Privileged public
operations are classified by the live target when it is reachable, so adding a
new privileged verb to the source checkout does not require reinstalling the
trampoline. If the target cannot be reached, the trampoline falls back to its
install-time classification list, which is enough to fail politely for known
privileged verbs. The wrapper preflights sudo non-interactively first, so a
non-sudo agent gets a clear failure instead of an unanswerable password prompt.
A sudo-capable human at a TTY still gets the normal sudo prompt for privileged
commands.

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
state, and shared checkout. Pane subcommands delegate through that runtime user
before touching tmux, pane hook state, or resume session files, including
detached roles, `attach-role`/`detach-role`, and the viewer path. For runtime
director-controlled display slots, see [runtime presentation](runtime-presentation.md).
In auto layout
mode, KDE/plasma desktops use the separate Konsole layout; known non-KDE
desktops and undetected desktops use the tmux viewer. `--layout separate` still
forces the Konsole layout explicitly. Viewer launches use an internal no-attach
pane mode so parent-launched visible role sessions are ensured without attaching
tmux before the viewer is built. Projects
build viewer windows in a project-scoped `<project>-viewer` tmux session, so
starting or restarting one project does not replace another project's viewer.
Pre-upgrade sessions named only `viewer` are left alone because the old name has
no project identity and cannot be safely adopted or killed automatically. Visible
windows are titled with the project's display name when `project_name` is set in
the config, and fall back to the project slug otherwise. Viewer mode sets tmux
`set-titles` and `set-titles-string` on the project-scoped viewer session, so
the title survives role pane restarts and tmux detach/attach cycles. Konsole
mode passes the same title with `--qwindowtitle` and writes it into every
materialized layout leaf's tab `Title`, so the window title stays static instead
of following whichever role pane has focus. Projects
without `run_as_user` use the invoking user's runtime
pane-state directory and `$HOME/bin` prepended to pane CLI `PATH`; projects
with `run_as_user` prepend that runtime user's `$HOME/bin`, not the invoking
user's private bin directory. Resume session records are durable state under
the runtime user's home, not `/run`.
Real `start` and `reload` launches verify that the selected runtime user has
linger enabled and a `/run/user/<uid>` directory before starting tmux panes. If
host policy denies that setup, the launcher fails before touching panes and
prints the `sudo loginctl enable-linger <user>` command an operator must run.

Project startup also requires an explicit desktop policy. Use
`switchyard new --desktop-policy headless` for no clipboard, or supply a recorded
Wayland consent file. Existing projects use `switchyard upgrade <project>
--desktop-policy ...` before starting new role processes. The same pre-launch
path installs persistent scoped access and verifies tenant-safe environment;
see [desktop access](desktop-access.md). Upgrading the policy does not restart
working roles. This policy concerns worker clipboard access; the window display
selection below still controls the invoking desktop's Konsole window.

When opening the visible Konsole window, the launcher uses
`PGU_HOST_WAYLAND_DISPLAY` if it is set. Otherwise it resolves the display for
`PGU_TEAM_LAUNCHER_GUI_USER`, or for the invoking user when that variable is
unset:

```bash
env QT_QPA_PLATFORM=wayland \
  QT_LOGGING_RULES=qt.qpa.wayland.warning=false \
  WAYLAND_DISPLAY=/run/user/<uid>/wayland-0 \
  konsole --separate --qwindowtitle "<project name>" --layout ...
```

The commands inside the Konsole layout call `scripts/team-launcher pane ...`
as the same user, so visible panes attach to that user's tmux sessions.
Set `PGU_TEAM_LAUNCHER_BIN_DIR` to override the default `$HOME/bin` PATH
prefix. The launcher deliberately prepends only `$HOME/bin`, not
`$HOME/.local/bin`; pane hooks are installed and referenced by absolute paths.
Set `PGU_TEAM_LAUNCHER_GUI_USER` or
`PGU_TEAM_LAUNCHER_WAYLAND_DISPLAY` when the Konsole display should be resolved
from a different local desktop account or socket name.

## Installing the Entry Point

Switchyard is installed as immutable host-level releases under
`/opt/switchyard`, with `/opt/switchyard/current` as the active symlink and
`/usr/local/bin/switchyard` as a small wrapper to
`/opt/switchyard/current/switchyard`. Run the installer from a fresh clone of
the bare repository, not from the active release or a standing operator checkout,
so a bad release cannot remove the tool used to fix it and recovery does not
depend on traversing a user's home directory. The privileged host step is:

```bash
tmpdir="$(mktemp -d)"
git clone /data/git/switchyard.git "$tmpdir"
sudo env \
  SWITCHYARD_SOURCE_REPO="$tmpdir" \
  SWITCHYARD_SOURCE_REF=<audited-commit> \
  SWITCHYARD_SHARED_INSTALL_ROOT=/opt/switchyard \
  SWITCHYARD_INSTALL_PATH=/usr/local/bin/switchyard \
  "$tmpdir/scripts/install-switchyard" --apply
command -v switchyard
sudo env sh -c 'command -v switchyard'
```

The installer exports the requested commit into
`/opt/switchyard/releases/<commit>`, writes `.switchyard-release.json`, canaries
the release directly with `switchyard --version`, and then repoints `current`.
Rollback is deliberately just:

```bash
sudo ln -sfn /opt/switchyard/releases/<previous-sha> /opt/switchyard/current
```

That plain symlink operation does not depend on the active release. The
installed wrapper embeds fallback help and version text so `switchyard --help`
and `switchyard --version` still work when `current` is missing or broken;
mutating commands fail with a recovery command that clones
`/data/git/switchyard.git` and runs that clone's installer. `SWITCHYARD_TARGET`
is accepted only for non-privileged
direct execution. Privileged commands and root execution always use the compiled
`/opt/switchyard/current/switchyard` target, so the override cannot become a
root code-execution path.

`/usr/local/bin` is deliberately used because it is on the host's normal shell
`PATH` and should also be present in sudo's `secure_path`. When `sudo -V`
reports a `secure_path`, the installer refuses a system install path that is not
listed there; when sudo reports no such setting, it proceeds. The installed
wrapper lives outside every user's home.
The wrapper preserves `PYTHONDONTWRITEBYTECODE=1` across that privileged
re-exec, and the Python entrypoints also set `sys.dont_write_bytecode`, so
running privileged Switchyard commands from an operator checkout does not leave
root-owned `__pycache__` files next to imported source modules.

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
switchyard validate-models otto
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
of blocking on the window process. New role sessions are created with the tmux
window name set to the board role (`director`, `main`, `app`, `ops`, `audit`,
`inspector`, `research`), so the status bar keeps a deterministic role label
even after the CLI updates its pane title. The launcher also sets `mouse on`
and `history-limit 200000` on every tmux session it creates, including the
viewer session, so tenant panes can scroll and select text without relying on
an owner's personal `.tmux.conf`. Before opening Konsole or the tmux viewer,
`start` fetches the configured worktree ref. If no role tmux sessions are
running, it also refreshes
the project checkout: shared-checkout projects are checked out to the ref with
`--force` and cleaned with `git clean -fdx`, while control-repository projects
prepare each role worktree. If any role session is already running, `start`
keeps the existing window-reopen/attach behavior but avoids resetting files
under live panes: shared-checkout projects fetch only, and control-repository
projects refresh only roles with no live tmux session. This means `switchyard
pgu` will normally fetch without force-refreshing `/home/agent/Projects/pgu`,
because pgu's panes are normally running; update that shared checkout explicitly
when you need it current. Destructive refresh is intentionally limited to
checkouts with no live panes; use a ticket worktree for implementation work.
A shared checkout refresh failure blocks every role: visible panes print the
checkout error instead of launching the CLI, while detached roles are skipped.

`attach` only attaches visible panes to existing sessions and fails if a role
session is missing. Detached roles are checked for existence but remain
detached.

`pane attach-role <role> --slot <n>` surfaces a detached/headless role in a
named free layout slot without rebuilding the whole window layout and without
restarting neighbouring roles. The command updates only that role's config
entry from `detached: true` to the named `slot`, then starts or attaches that
role's own tmux session. If the config update is not permitted, the command
fails before starting the role session. If the tmux session is not already live,
attach-role requires a recorded resume id and refuses rather than starting
fresh; a failed start or unverified resume rolls the config entry back to
detached. If the named slot is occupied or outside the configured layout, the
launcher refuses and says which role owns the slot; it does not evict a
neighbour or grow the grid implicitly. A single launcher window may contain at
most six visible panes, so `attach-role` also refuses to surface a seventh
visible role until another role is detached. `pane detach-role <role>` moves a
visible role back to headless by removing its slot and marking it detached, then
detaches any live tmux clients from that role session without killing the
session. The role's durable session record is not cleared by either operation.
Use detached roles for workers the director rarely interacts with directly; they
continue running as tmux sessions and can be surfaced later with `attach-role`.

When a launcher `pane` command is run from inside another role's pane, inherited
pane identity is treated as caller state, not target state. The launcher strips
pane target, caller-role, pane-session, and pane-state environment
from the spawned role process, then supplies the target role's own values. If an
ambient pane-session id disagrees with the target role's recorded session id,
the launcher refuses before killing or starting a tmux session. Until a fixed
launcher is deployed, use this workaround when running pane commands from inside
a pane:

```bash
env -u PGU_PANE_SESSION_ID -u TICKET_BOARD_PANE_SESSION_ID \
    -u TICKET_BOARD_PANE_SESSION_DIR -u TICKET_BOARD_PANE_STATE_DIR \
    -u PGU_TICKET_BOARD_PANE_SESSION_DIR -u PGU_TICKET_BOARD_PANE_STATE_DIR \
    -u TICKET_BOARD_PANE_TARGET -u PGU_PANE_TARGET \
    -u TICKET_BOARD_CALLER_ROLE \
    team-launcher <project> pane start <role>
```

For real one-shot CLI probes, also clear `TMUX` and `TMUX_PANE` and point
`TICKET_BOARD_PANE_STATE_DIR` and `TICKET_BOARD_PANE_SESSION_DIR` at throwaway
directories. Current installed hooks pass absolute state/session directories,
so clearing `TICKET_BOARD_PANE_TARGET` and `PGU_PANE_TARGET` is the part that
prevents a probe hook from writing live pane state; the throwaway directories
protect probe setups that still resolve paths from environment:

```bash
TICKET_BOARD_PANE_STATE_DIR="$(mktemp -d)" \
TICKET_BOARD_PANE_SESSION_DIR="$(mktemp -d)" \
env -u TMUX -u TMUX_PANE -u PGU_PANE_TARGET -u TICKET_BOARD_PANE_TARGET \
    -u PGU_PANE_SESSION_ID -u TICKET_BOARD_PANE_SESSION_ID \
    agy --help
```

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
`switchyard status` uses the same no-fetch style for runtime-copy visibility:
after the project liveness table, it reports the invoking checkout, project
checkout, visible role worktrees, and tenant board release when those paths can
be seen. When the invoking launcher is under `/opt/switchyard`, it reports the
shared release commit marker instead of demanding a `.git` checkout. It reports
freshness, staleness, or an unknown reason; it never pulls, resets, or repairs a
checkout.

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
  `claude`, `codex`, `agy`, and `hermes`; invalid names, including the retired
  `gemini` launcher name, are rejected while parsing project artifacts or prompt
  answers, before provisioning or launch mutates the project. The configured
  CLI must be executable by the project's owner user, not just installed
  somewhere on the host. First-run presence/auth/model probes, interactive
  setup commands, and pane launches all invoke CLIs with the same explicit
  non-login owner search path: the owner's `~/bin`, the owner's
  `~/.local/bin`, then the stable system path.
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
  `--dangerously-bypass-hook-trust`; Hermes uses `--yolo`.
  Hermes panes also receive launcher startup flags `--accept-hooks` and
  `--pass-session-id` so the installed pane-state hooks can run non-interactively
  and the agent can see its own resumable session id.
- `extra_args`: optional additional argv entries appended after the yolo flag.
- `fresh_session_per_ticket`: optional boolean, defaulting to false. When true,
  `start`/`reload` ignore a recorded session id for that role, move the previous
  record to the `.superseded` sidecar, and launch a fresh CLI session. Use it
  only for roles where carrying context between tickets is more dangerous than
  losing continuity; it is a role policy, not a CLI policy.
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
designer CLI (claude/codex/agy/hermes) [claude]:
director CLI (claude/codex/agy/hermes) [claude]:
Include audit role [Y/n]:
audit CLI (claude/codex/agy/hermes) [claude]:
Conventional implementer roles:
  main: core/domain implementation and integration
  ops: environment, services, tooling, and infrastructure
  app: application/UI work
  research: investigation, design support, and unknowns
  perf: measurement and performance work
Implementer roles (comma-separated) [main, ops]: app, code-review
app CLI (claude/codex/agy/hermes) [codex]:
code-review CLI (claude/codex/agy/hermes) [codex]:
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
project directory, initializes it as a git repo on the configured worktree branch
when it is not already one, creates the required initial commit, provisions the
board, runs first-run auth checks for the selected CLIs, and launches the
configured panes. It also copies the source checkout's project-agnostic
`docs/onboarding/` packet into the new project at `docs/onboarding/`, stamping
each copied file with the source commit and skipping any existing target files
without clobbering operator edits. `switchyard upgrade` refreshes only copies
that still match their stamped source commit, installs missing onboarding files,
and reports each file as refreshed, installed, or skipped. If the installed
snapshot commit is newer than the invoking checkout, refresh is skipped rather
than downgrading the tenant copy. `upgrade` also warns when the artifact source
checkout is behind its configured remote ref, because generated files reflect
the checkout being run, not the remote tip. Files with local edits or no
provenance header are left untouched. Existing git repos are left
untouched. Pass `--no-git-init` only when the project path already has a
usable repo with a `HEAD`; otherwise `new` refuses instead of launching a
project with broken role worktrees. The default agent user is `<slug>-agent`; a
typed agent-user value is used verbatim. If that user already exists, `new`
warns and asks for explicit confirmation, defaulting to No, before
reusing that account. Pass `--allow-existing-owner-user` to skip that
existing-user confirmation and reuse the account without prompting. In
non-interactive runs, `new` refuses an existing owner user unless that flag is
present. `--yes` is unrelated to this check; it skips the project-plan
confirmation. `director` is a fixed workflow role. `designer` is optional; when
omitted, `new` skips the design phase and does not create a designer pane,
designer onboarding file, or initial design document. `audit` is optional; when
omitted, the generated workflow skips the Audit stage and implementers submit
directly to Final Sign-Off. Implementer role names are supplied as a
comma-separated list and may contain hyphens. The default implementer roles are
`main` and `ops`; conventional names also include `app`, `research`, and `perf`,
but custom names are accepted when they match the role-name rules. The selected role names are
written to both the board provisioning plan and the generated launcher config,
and each role's selected CLI is recorded in the `switchyard.project.v1`
artifact. The artifact still must not preconfigure workflow stages or
transitions.

`switchyard add-role <project> <role> --cli <cli>` adds another implementer role
after provisioning. `switchyard add-role <project> <role> --audit --cli <cli>`
adds an auditor role instead. Auditor additions widen the Audit stage owners,
audit sign-off/kick-back role lists, board assignee/caller constraints, and
notification target constraint; they do not widen implementation ownership or
implementer operations. By default the command creates a visible role and starts
only that role's tmux session. If the project's layout template is a recognised
generated layout, the command regenerates it for the new visible count. If the
layout is hand-maintained or otherwise unrecognised, the command does not
overwrite it silently: the target slot must already exist, or the command
refuses before mutating config, SQL artifacts, systemd state, or worktrees. The
refusal names the missing layout capacity and the two choices: use `--detached`
to add the role headless, or pass `--relayout` to replace the current layout
with a fresh generated layout. `--relayout` keeps the generated contiguous slot
order, so omit `--slot` unless you are naming the next append slot exactly. For
example, `switchyard add-role mefp audit_gpt --audit --cli agy --detached` adds
a second auditor that can receive Audit-stage tickets, participate in round-robin
audit assignment, and sign off only the audit tickets assigned to it.

`add-role` cannot insert a new pane into an already-running Konsole window. It
starts the new role's tmux session, but newly added visible slots appear after
the project window is relaunched. The command also refuses when the role would
become the seventh visible role in the automatic window. Use `--detached` for
the added role, or detach another visible role first, when the project already
has six visible panes.

`switchyard set-vcs-close-role <project> <role>` changes an existing
provisioned project's terminal close role without re-provisioning. The role must
already exist, so add it first with `add-role` when needed. The command updates
the generated plan, board unit, and workflow SQL, applies the workflow change to
Postgres, and restarts the tenant board so both the HTTP gate and SQL transition
table agree. The built-in `pgu` project rejects this command by design.

Generated Konsole layouts use a single horizontal row for up to four visible
roles, so small teams get tall side-by-side panes. Five or more visible roles
fall back to a balanced row-major grid that spreads panes across rows with a
maximum row width of four. Automatic project creation caps the visible Konsole
window at six panes. When a provisioned role list is longer than six, the first
six roles get slots in the layout and the overflow roles are written as
`detached: true`; the provision output names those auto-detached roles and
reminds the operator to use `attach-role` later or detach another role first.
This is a display cap, not a project role cap. The project config's `layout`
path remains the override for shapes up to the six-pane automatic window limit:
edit or replace that JSON file for a project-specific window shape without
changing the provisioning prompts. True multi-window display needs explicit
window-plus-slot role placement and a director-invoked command to open the
additional role set; the launcher does not split a team across windows
automatically.

The first-run auth phase runs before panes launch. It probes each distinct
selected CLI, detached-role folder trust, and Codex hook trust as the project
owner, then prints one upfront setup manifest before it executes any
interactive command. The manifest states the owner user once, counts missing
CLIs, login steps, folder-trust steps, and Codex hook approvals, and lists the
affected roles for context. CLI login and Claude/agy folder trust are
interactive setup steps that Switchyard can run today; Codex hook approval is a
manual security approval and remains deliberately outside automation. Folder
trust is scoped to the workdir, so it recurs for each new project even when the
owner user is reused.

If any configured CLI is missing for the owner user, `switchyard <project>` and
the final launch step of `switchyard new` stop before opening panes. The
recommended policy is option (c): refuse to launch and tell the operator what
must be installed for which owner user. The top-level `./install` command
intentionally runs vendor CLI installers as the invoking operator, not as root,
and a newly provisioned tenant owner may not exist yet when that install ran.
The vendor commands are per-user shell installers with interactive auth and
setup behavior, so silently running them as a just-created project owner or
pretending they are system-wide is a bigger host policy decision than the
launcher should make during pane startup.

An existing owner user whose login shell no longer exists is reported in that
same first-run manifest, with a `sudo usermod -s <installed-shell> <owner>`
remedy. Switchyard only reads the passwd entry and checks whether the configured
shell is executable; it does not repair existing accounts automatically. That
keeps the existing-user promise intact for operators who deliberately changed a
tenant account shell.

During `switchyard new`, the hook installer seeds Codex hook trust only when
the owner has no existing `~/.codex/hooks.json` and no existing Codex hook
trust records. That covers hooks Switchyard just authored as part of project
creation. If Codex trust records already exist, or any Codex hooks file already
existed before install, seeding is refused and the normal manual `/hooks`
report remains. Claude and agy folder trust is never pre-seeded because it
trusts project-repository contents, not Switchyard-authored hook config. Codex
hook-trust entries are also checked against the installed pane-hook commands.
Codex hook trust is stored per owner user and hook entry, not per pane role, so
missing or stale hashes are reported as distinct approvals needed with affected
Codex roles listed only as context. A new project with no trust records is
reported separately from a hook-content change that requires re-approval.
During `switchyard new`, the same phase also validates every configured role
model with that CLI's one-shot prompt mode before panes start. A failed model
probe happens after the project is created and registered, but before any panes
launch; fix the model in the generated config and rerun `switchyard <project>`.
The warning reports the role, CLI, configured model, and provider error; the
launcher never substitutes a different model. The same check can be run later
with `switchyard validate-models <project>`. Routine `switchyard <project>`
starts do not validate models because the probe is an API call per role and
team startup should not depend on transient provider availability.
Hermes is probed with `hermes config check`, not `hermes auth list`, because
pooled credentials can appear in `auth list` while Hermes still has no resolved
API key and opens its setup prompt. Env/config-resolved API keys are the
runnable shape the launcher accepts. Agy failures include suggestions from
`agy models` when that catalogue is available; Claude, Codex, and Hermes report
that no model list is available and use only the one-shot validation result.

By default every role, including Hermes roles, resumes from the recorded session
id when one is available and passes that id through
`TICKET_BOARD_PANE_SESSION_ID`. A director can opt a specific role into
`fresh_session_per_ticket` when the role should not inherit previous ticket
context. That policy is deliberately explicit: Hermes `/clear` is available for
interactive use, but this ticket did not establish a reliable automatic
ticket-boundary reset protocol that can be delivered to a pane mid-flight and
verified as equivalent to launching without a recorded session id. Until such a
protocol exists, automatic reset means opting the role into a fresh process
session.

Hermes also exposes a built-in `session_search` tool backed by
`$HERMES_HOME/state.db`. Because that database contains indexed content from
every Hermes session under the same home, Switchyard gives each Hermes role its
own durable `HERMES_HOME` under the project state directory. The launcher
symlinks the owner user's shared Hermes auth/config entries, including
`.env`, `auth.json`, `auth.lock`, `config.yaml`, hooks, skills,
`shell-hooks-allowlist.json`, and `shell-hooks-allowlist.json.lock`, into the
per-role home so existing credentials and hook configuration still work. A lock
file is shared whenever the JSON file it guards is shared; otherwise several
Hermes panes would take role-local locks while writing one shared file. Runtime
and memory state, including `state.db`, `sessions`, `logs`, `cache`, and
`memories`, stays private to the role-local home. Claude, Codex, and agy
continue using their existing resume stores and arguments.

The PGU-839 measurement pass used disposable homes. Hermes v0.15.2 can retrieve
prior-session content through the built-in `session_search` tool from
`$HERMES_HOME/state.db`; a shared database found a sentinel message from a
different worker session, while the same search against another role's isolated
home returned zero results. Codex 0.151.0 exposed explicit `resume` by id/name,
`--last`, and picker search over its `CODEX_HOME`/`~/.codex` session records,
but no automatic prior-session search tool in a fresh run. Claude 2.1.251
exposed explicit `--resume`, `--continue`, and resume search over
`~/.claude/projects`, but no fresh-session search tool equivalent to Hermes.
Agy 1.1.22 exposed explicit `--conversation` and `--continue` over its
`~/.gemini/antigravity-cli` conversation store, but no fresh-session search
tool. The isolation change is therefore scoped to Hermes and leaves the other
runtime command lines unchanged.

The same provision path consumes generated and hand-written artifacts:

```bash
sudo switchyard new --from /path/to/porter.project.json --yes
```

The artifact's `repository` is the project checkout opened by the generated team
panes. The generated director role receives onboarding paths in its environment;
the director owns the unprivileged half after the board exists: seed the board,
launch panes, onboard the team, and reshape stages or roles later if needed.
