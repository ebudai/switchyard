# Ticket Board Service

The deploy tooling auto-targets the live board unit:

- if a system `pgu-ticket-board.service` is installed, deploy/start/stop/
  restart/status operate that system unit
- otherwise the scripts fall back to the legacy `agent` user unit

Today the production board is the privileged `boardsvc` system unit on
`127.0.0.1:8770`.

The `boardsvc` cutover package lives in
`docs/ticket-board-boardsvc-peer-auth-runbook.md`. Once that system unit
exists, `scripts/ticket-board-service.sh` must restart it rather than the
shadow `--user` unit.

The live service does **not** run from a mutable git checkout anymore. It runs
from a controlled export root:

- deploy root: `/home/agent/pgu-ticketboard-live`
- active release: `/home/agent/pgu-ticketboard-live/current`
- release snapshots: `/home/agent/pgu-ticketboard-live/releases/<sha>`

This makes deploys deterministic: once the director deploys a merged `main`
revision and restarts the service, the running code stays pinned to that SHA
until the next explicit deploy.

## Install / enable

```bash
scripts/ticket-board-service.sh install
```

For a legacy user-unit install, this does:

- exports `origin/main` into `/home/agent/pgu-ticketboard-live/current`
- writes `~/.config/systemd/user/pgu-ticket-board.service`
- bakes `TICKET_BOARD_DATABASE_URL` into the unit so the service does not depend
  on the installing shell environment after reboot
- applies unrecorded SQL migrations from `scripts/ticket_board/migrations/`
  and records their filenames in
  `ticket_board.schema_migrations`
- applies `scripts/ticket_board/rbac.sql` so `ticket_board_service` and
  `ticket_board_listener` exist before the units start
- enables and starts `pgu-ticket-board.service`
- waits for `GET /api/board` to return HTTP 200 before reporting success

Runtime dependency:

- psycopg 3 is required by `scripts.ticket_board.app`
- Pillow is required by module-scope `PIL` imports in `scripts.ticket_board.app`
  and `scripts.ticket_board.server`; the prereq script installs it as
  `python3-pil` on apt systems and `python-pillow` on Arch-family systems
- on Ubuntu 24.04 / noble, `python3-psycopg` is packaged as `3.1.17-2`, below
  the `psycopg>=3.3,<4` runtime pin, so `scripts/install-switchyard-prereqs`
  creates `/opt/switchyard/venv` with system site packages visible and installs
  Psycopg there instead of using `pip install --user`
- on Arch/CachyOS the packaged `python-psycopg` already satisfies the pin, so
  `scripts/install-switchyard-prereqs` installs it with the other pacman
  packages and the system Python imports it directly; no venv and no pip are
  involved. To add it by hand:

```bash
sudo pacman -S python-psycopg
```

Debian/Ubuntu and Arch-family system Python are both PEP-668 externally
managed, so no supported path uses `pip install --user psycopg`, and
`--break-system-packages` is not used anywhere. Verify
the deployed board interpreter in two parts: import the runtime dependencies,
then run the entry point so the board module graph loads:

```bash
/opt/switchyard/venv/bin/python -c 'import psycopg, PIL; print(psycopg.__version__, PIL.__version__)'
/opt/switchyard/venv/bin/python /home/agent/pgu-ticketboard-live/current/scripts/ticket-board.py --help
```

The service runs:

- working directory: `/home/agent/pgu-ticketboard-live/current`
- command: `/opt/switchyard/venv/bin/python /home/agent/pgu-ticketboard-live/current/scripts/ticket-board.py --host 127.0.0.1 --port 8770 --unix-socket /run/pgu-ticket-board/ticket-board.sock` when the shared venv exists; otherwise `/usr/bin/python3` unless `TICKET_BOARD_PYTHON` overrides it
- logs: `/tmp/pgu-ticket-board.log`
- default DB URL: `postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_service`

## Canonical directorctl

The runtime `directorctl` is `/home/agent/bin/directorctl`. Keep
`scripts/directorctl` as the reviewed source, but do not invoke per-checkout
copies from panes or board services. After changing `scripts/directorctl`,
refresh the canonical runtime copy with:

```bash
scripts/install-directorctl
```

`scripts/ticket-board-service.sh render-unit` emits the generic legacy
agent-user unit for comparison and local diagnosis; it is not the deploy
candidate. For a dedicated `boardsvc` deployment, compare and install the
candidate from the release snapshot,
`<release>/deploy/systemd/<service>.boardsvc`, to the live systemd unit path.
Both unit shapes must stay Postgres-only and must not pass the retired backend
selector CLI option.

## Ticket Board Validation

`scripts/ticket-board-test-suite` is the standard one-command validation set
for ticket-board changes. By default it discovers every
`tests/ticket_board_*_test.py` and `tests/ticket_board_*_test.sh`, runs all
runnable suites without fail-fast, and prints per-group and per-suite
PASS/FAIL/SKIP results plus a summary.

Use `--group adjacent` for board-adjacent tests outside the `ticket_board_*`
namespace, `--group host` for repository-hook and Switchyard installation
automation, `--group frontend` for non-browser standalone tests that import
the `scripts.ticket_board` package, `--group browser` for inline Playwright
tests that import `scripts.ticket_board`, or `--group all` for all five groups.
Launcher suites named `tests/team_launcher_*_test.py` are structurally included
in the adjacent group and need no content marker. The remaining extra suites
are discovered from test file content, not a manifest: standalone tests that
reference board URLs, sockets, `ticket-board-write`,
`ticket_board.write_client`, or the `ticket_board_ui_harness` browser harness
are included as adjacent; repository-hook imports and the
`install-all-repository-hooks` or `install-switchyard` entrypoints mark host
automation; remaining inline `scripts.ticket_board` importers are split by
whether they use Playwright/browser markers. PGU-787
measured the 14 inline browser importers at 13.14s and the full 52 inline
importer set at 21.62s, so they are included in `--group all` rather than left
as manual-only tests. Browser dependency handling is centralized in this
runner: when the Playwright Python package is unavailable, every discovered
browser-runtime suite in the selected groups is reported as `SKIP` by name and
counted in the summary instead of failing inconsistently inside individual test
files. Use `--fail-on-skip` when the validation environment is expected to have
every dependency installed; that makes a browser skip a non-zero runner result
while preserving the explicit skipped-suite summary. Tests that execute useful
unprivileged coverage but cannot execute a privileged branch print `COVERAGE REDUCED:
<reason>`; the runner reports the reduction on the PASS line and in its summary
rather than silently treating partial coverage as complete.
Direct invocation of an individual browser test is a developer convenience
path and may fail with the
raw missing dependency; use the suite runner when the skip-vs-fail convention
matters.

The browser fallback state-label map in `frontend_script_core.py` is generated
from the `workflow_stages` seed in `schema.sql`. Keep workflow stage labels in
the schema; `ticket_board_workflow_config_equivalence_test.py` pins the emitted
frontend fallback against that canonical source.

## PostgreSQL notification listener

The post-cutover notification shim runs as a separate `agent` user systemd
service. It uses PostgreSQL `LISTEN ticket_board_state_transition`, maps each
state-transition payload to the appropriate `pgu-<role>:0.0` tmux pane, and
does not deliver `eric_review` transitions.

Unlike the board web server, the notify-listener currently remains a user unit;
there is no parallel `boardsvc` system listener in production.

```bash
scripts/ticket-board-notify-listener-service.sh install
```

The listener service runs:

- working directory: `/home/agent/pgu-ticketboard-live/current`
- command: `python3 /home/agent/pgu-ticketboard-live/current/scripts/ticket-board-notify-listener`
- logs: `/tmp/pgu-ticket-board-notify-listener.log`
- default DB role: `ticket_board_listener`
- optional env file: `~/.config/pgu/ticket-board-notify-listener.env`

`scripts/ticket_board/rbac.sql` creates `ticket_board_listener` as a login role
with read-only table access and no write-function grants. The install commands
apply that RBAC file idempotently before starting the services. Override
`TICKET_BOARD_ADMIN_DATABASE_URL` if the installer needs a non-default admin
connection; by default it targets the local `pgu` database as PostgreSQL
`user=postgres`, because creating missing roles requires `CREATEROLE`.
You can run only the RBAC bootstrap with
`scripts/ticket-board-service.sh ensure-roles` or
`scripts/ticket-board-notify-listener-service.sh ensure-roles`. The rendered
listener unit bakes
`TICKET_BOARD_NOTIFY_DATABASE_URL=postgresql:///pgu?host=/var/run/postgresql&user=ticket_board_listener`
by default; the optional env file can still override it for nonstandard local
auth.

You can run only the migration bootstrap with
`scripts/ticket-board-service.sh ensure-migrations` or
`scripts/ticket-board-notify-listener-service.sh ensure-migrations`. Both use
`TICKET_BOARD_ADMIN_DATABASE_URL` because migrations may need DDL privileges.
The underlying runner is `scripts/ticket-board-migrate`; it applies
legacy `NNN_description.sql` files plus new `pgu<num>_description.sql` files
in deterministic filename order, skips names already present in
`ticket_board.schema_migrations`, and records a migration name only after that
SQL file successfully applies. The runner does not do dependency resolution:
if two unapplied migrations genuinely depend on each other, they must be
rebased/combined before merge so the file order is already settled.

## Controlled deploy / restart

When the director wants merged board changes live, use:

```bash
scripts/ticket-board-service.sh deploy-restart
```

That command:

1. fetches the source repo
2. resolves `origin/main`
3. exports that exact SHA into `/home/agent/pgu-ticketboard-live/releases/<sha>`
4. applies unrecorded SQL migrations from that release
5. starts a pre-flight canary from that release on a scratch port and scratch
   Unix socket as `boardsvc` by default
6. only after the canary passes, repoints
   `/home/agent/pgu-ticketboard-live/current`
7. restarts the live board unit
8. waits for `GET /api/board` to return HTTP 200
9. asserts that the live `/api/board` response reports the deployed release SHA
   in `build_id`
10. when the live unit is system-scoped, stops/disables any shadow `--user`
   board unit so it cannot crash-loop on port `8770`

When a deploy helper is itself running as root but `SOURCE_REPO` is a
non-root operator checkout, Git commands against that checkout are delegated to
the checkout owner with `sudo -u <owner> -H git -C "$SOURCE_REPO" ...`. This is
required for the initial `git fetch origin`: otherwise the fetch can create
root-owned files under `.git/objects` and break the operator's later
`git pull`. The deploy scripts must not repair that by recursively chowning the
operator repository.

If the canary fails, `deploy-restart` exits nonzero without touching the live
`current` symlink or restarting the service. If the post-restart live smoke
check or live build-id check fails, it repoints `current` back to the previous
release, restarts the service again, verifies the old release is healthy, and
exits nonzero.

If `/etc/systemd/system/pgu-ticket-board.service` exists, `deploy-restart`
targets that system unit with plain `systemctl`, not `sudo`. The host should
install the reviewed rule
`deploy/polkit/49-pgu-board-deploy.rules` as
`/etc/polkit-1/rules.d/49-pgu-board-deploy.rules`. That rule allows only the
`agent` user to restart/start/stop `pgu-ticket-board.service` and to
start/stop the fixed, root-owned `pgu-ticket-board-canary.service` unit. This
lets the team run the mandatory canary health gate and then restart the live
board without Eric's active KDE session or blanket sudo.

Do not add `org.freedesktop.systemd1.manage-unit-files` to that rule to support
enable/disable. On this host, `manage-unit-files` implies both `reload-daemon`
and `manage-units`, which would silently expand the narrow grant into global
daemon-reload plus unrestricted unit management. `reload-daemon` cannot be
scoped per unit because polkit receives no unit detail for that action; an
operator must perform daemon reloads outside this deploy grant.

The canary unit runs as
`boardsvc`; `deploy-restart` only writes
`/home/agent/pgu-ticketboard-live/canary.env` to point that fixed unit at the
candidate release and scratch port/socket. If the host rule or canary unit is
absent, `systemctl` fails with its normal authorization error.

Other privileged system actions, including system unit installation and
`daemon-reload`, are intentionally outside that rule. For the system-unit path,
`deploy-restart` compares the candidate release unit with the installed unit
before it applies migrations, runs the canary, or repoints `current`. If the
unit content differs, or if systemd reports `NeedDaemonReload=yes`, the deploy
fails loudly and tells the operator to install the candidate unit and run
`systemctl daemon-reload`. Code-only deploys keep using the whitelisted restart
path without attempting a global daemon reload.

Plain `restart` only restarts the currently deployed SHA. It does **not** update
code.

## Operations

```bash
scripts/ticket-board-service.sh status
scripts/ticket-board-service.sh restart
scripts/ticket-board-service.sh stop
scripts/ticket-board-service.sh start
scripts/ticket-board-service.sh logs
```

## Notification Delivery

The old polling watchdog path is retired. Ticket notification delivery is split
between PostgreSQL trigger/cron state and the
`pgu-ticket-board-notify-listener.service` user service. The listener handles
real-time `LISTEN` delivery, replay reconciliation after reconnect, and
NUDGE-message gating against live pane activity.

The listener no longer screen-scrapes pane output. Each agent CLI must run
`scripts/ticket-board-pane-idle-hook` from its lifecycle hooks so the listener
can read a per-pane state file from
`$PGU_TICKET_BOARD_PANE_STATE_DIR` (default:
`/run/user/<agent-uid>/pgu-ticket-board/pane-state`). Missing hook state fails
closed and defers delivery. The listener logs the panes still missing hook
state on startup.

Hook commands must pass `--target pgu-<role>:0.0` or set
`TICKET_BOARD_PANE_TARGET` / `PGU_PANE_TARGET`; the writer does not infer a
target from ambient tmux state. The required state transitions are:

- Claude Code: `SessionStart` writes initial `idle` and records the resume
  session id; `Notification` with matcher `idle_prompt` writes `idle`;
  `UserPromptSubmit` and `Stop` write `busy`.
- Codex: `SessionStart` records the resume session id when Codex emits it, but
  interactive Codex 0.150.1 can defer that event until the first prompt instead
  of firing it during idle startup. `Stop` writes `idle`; `UserPromptSubmit`
  writes `busy`; permission prompts write `blocked` if the hook surface exposes
  them.
- Agy: hook config is stored under the Gemini config tree and still emits
  `gemini.*` source labels. `SessionStart` writes initial `idle` and records the
  resume session id; `PreInvocation` writes `busy`; `PostInvocation` and `Stop`
  write `idle`. Do not use Gemini `Notification` for idle because it is
  alert/permission-oriented.
- Hermes: hook config is stored in `~/.hermes/config.yaml` and emits
  `hermes.*` source labels. Hermes has no separate runtime status timer that
  the listener parses today; it is intentionally hook-state-only.
  `pre_llm_call` writes `busy` and is also the Hermes active-work context
  injection point: the hook emits flat `{"context": "..."}` with the current
  `in_progress` ticket identity, body, implementation scope, and audit
  constraints. `post_llm_call`, `on_session_end`, and `on_session_finalize` are
  turn-end idle sources. `on_session_start` only seeds initial idle and the
  resume id, so it still needs the normal pane-content corroboration before
  delivery and does not provide active-work context for Hermes.

On Claude/Codex-style `SessionStart`, the hook also injects active-work context
when the CLI reports a lifecycle source of `compact`, `resume`, or `clear` and
the pane's role already owns an `in_progress` ticket. Each source gets distinct
opening wording so a cleared pane is not told it resumed or compacted. Hermes
does not honor this `hookSpecificOutput.additionalContext` shape; use its
`pre_llm_call` flat context path instead.

A fresh `director` `SessionStart` gets a short pointer to the project's
onboarding packet before any ticket work begins. The hook emits that pointer
only when it can verify an installed `docs/onboarding/` packet, and it resolves
the project root from the generated pane environment before falling back to the
pane cwd. A launcher-provided resume session id suppresses the pointer so normal
director resumes are not nagged.

Session ids are stored under `$TICKET_BOARD_PANE_SESSION_DIR` (default:
`~/.local/state/pgu-ticket-board/pane-sessions`) using the same per-pane file
naming as hook state. This durable directory is the maintained source of truth;
`/run/user/<uid>/pgu-ticket-board/pane-sessions` is legacy seed input only and
is not repopulated after boot. A hook can create the record when none exists,
but replacing an existing session id is guarded for every source:
`SessionStart`, `PreInvocation`, `PostInvocation`, `Stop`, and equivalents. A
different incoming id is accepted only when it matches the launcher-provided
`TICKET_BOARD_PANE_SESSION_ID` or when the hook invocation passed `--session-id`
explicitly. A transient CLI launched inside a pane inherits that pane's
target/session environment, but it cannot replace that pane's durable resume id
with a different conversation id.
Before the launcher deliberately starts a pane fresh instead of resuming, it
clears that pane's old session record; the newly launched CLI can then claim the
empty record first. Payloads without a session id still update pane state and do
not create or rewrite a session file. The launcher uses these files to pass
resume ids when recreating panes without losing context.

For notification delivery, idle hook records are not all equally authoritative.
Only turn-end sources from the pane's configured runtime can establish idle on
their own. Session-lifecycle sources such as `SessionStart`, foreign-runtime
sources inherited through pane target/session environment, and other
non-turn-end idle records must be corroborated by a working-timer/content probe.
Hermes is a known hook-state-only runtime in this table; a Hermes-configured
role should not report `foreign_runtime_*` merely because its hook source is
`hermes.*`. A successful probe with stable pane content is idle evidence; a
failed or otherwise inconclusive probe still treats the pane as busy and records
a specific trace reason instead of collapsing the decision into `hook_idle`.

If a queued notification targets a tmux pane that no longer exists, the listener
does not keep retrying it as a busy pane and does not silently drop it. The row
stays in `ticket_board.ticket_notification_queue` with `dead_lettered_at` set
and `terminal_reason='tmux_target_missing'`, is excluded from future claims, and
has a `dead_letter` trace event. A director can explicitly clear a visible
terminal or otherwise stuck notification through the supported write API:

```bash
scripts/ticket-board-write dismiss-notification <notification-id> --reason "pane was detached"
scripts/ticket-board-write dismiss-notification --ticket-id PGU-772 --target-role perf --kind transition --reason "pane was detached"
```

Install the hook writer and persistent CLI hook config entries with:

```bash
scripts/ticket-board-install-pane-hooks install
```

That command copies the standalone writer to
`~/.local/bin/ticket-board-pane-idle-hook` and writes managed entries to:

- Claude: `~/.claude/settings.json`
- Codex: `~/.codex/hooks.json`
- Agy/Gemini config tree: `~/.gemini/config/hooks.json`

Project provisioning passes `--seed-codex-hook-trust-if-new` so a fresh owner
does not need a redundant Codex `/hooks` approval for hooks Switchyard just
wrote. Manual reinstalls should omit that flag; if Codex hooks or trust records
already exist, hook trust remains a manual `/hooks` re-approval.

The hook cutover order is strict:

1. Run `scripts/ticket-board-install-pane-hooks install`.
2. Restart every live pane CLI so it reloads its hook config. Claude and Gemini
   should seed hook-state files immediately. Codex may not emit `SessionStart`
   until the first prompt, so launcher startup reporting warns when Codex has
   only launcher-seeded state and no fresh `codex.*` hook state.
3. Verify all hook-state files exist:

```bash
scripts/ticket-board-install-pane-hooks verify-state
```

4. Only after `verify-state` passes for every pane, deploy or restart the
   listener:

```bash
scripts/ticket-board-notify-listener-service.sh deploy-restart
```

Do not deploy the listener first. Until each pane writes hook state, the
listener intentionally treats that pane as busy and holds notification delivery.

The director pane also uses cursor/composer checks as a no-clobber latch. The
listener treats unavailable cursor state as busy. `directorctl` is a final
guard only for a positively detected non-empty human composer: markerless
agent/tool output is delivered to the director, and a still-busy composer waits
only for the bounded retry count before delivering with a warning so
pane-to-director escalations cannot starve forever.

## Local write authority: what is enforced, and what is not

Read this before relying on a caller role for anything security-relevant. An
earlier revision of this section claimed more than the code delivers; the
claims below are the ones that survived adversarial review (SYRD-39).

### Enforced: which Unix account may reach this board

- The socket is mode `0660` inside a `0750` runtime directory, both group-owned
  by the tenant's group, which the unit grants the board service as a
  supplementary group. It was previously `0666` in a world-traversable
  directory, so any local account -- including another tenant's -- could
  connect.
- With `TICKET_BOARD_TENANT_USER` configured the board additionally refuses peer
  uids that are neither the tenant owner nor its own account.
- A board restart repairs an already-deployed socket's ownership and mode, so
  provisioning and `switchyard upgrade` both carry the repair before roles
  launch. When the group is unset or unknown the board logs a warning and leaves
  the socket alone, so a half-configured deployment is visible rather than
  quietly unprotected.
- HTTP writes keep their own separate token authentication.

This is a real boundary because it rests on Unix uids, which the kernel
enforces.

### NOT enforced: which role a local caller is

**Role separation between the roles of one project is not enforced, and cannot
be while every role runs as the same Unix uid.** Do not design anything on the
assumption that a caller role proves who is calling.

The board binds a caller role to a *role session* -- the pane process a role's
commands descend from -- so that a role's authority survives its one-shot
`ticket-board-write` helpers instead of evaporating between commands. That is a
**durability and stability mechanism, not an authorization boundary.** Two
independent weaknesses, both reproduced, keep it from being one:

1. **The first binding is still the caller's word.** The board derives the
   session key from the process tree, but the role attached to that key is
   whatever the peer supplied while the session and role were both vacant.
   Nothing trusted maps a session to its configured role.
   `ticket-board-claim-role` narrows the window at pane start but uses the same
   unauthenticated endpoint, and every board restart drops all bindings while
   the panes keep running, which reopens the window for every live pane. A
   normal deploy is enough to recreate it.

2. **The session key itself is forgeable from inside any pane.** The pane root
   is found by looking for an ancestor whose parent's `/proc/<pid>/stat` `comm`
   begins with `tmux`. A process sets its own `comm` with
   `prctl(PR_SET_NAME)`, so one `prctl` plus one `fork` manufactures a fresh,
   unused "pane" identity without entering another pane, without driving tmux
   and without `ptrace`.

The board cannot close either one on its own: `/proc/<pid>/fd` is unreadable
across uids, so it cannot authenticate the real tmux server, and every other
input it can read is writable by any process of the tenant uid. Under one shared
uid, every role is the tenant.

The regressions in `tests/ticket_board_pid_identity_test.py` pin both
weaknesses deliberately, so neither can be quietly reintroduced or quietly
described as fixed.

### The minimum isolation change

Give each role its own Unix identity: a separate account per role, each with its
own tmux server and its own membership of the socket group. Then `SO_PEERCRED`'s
uid alone separates roles, the kernel enforces it, and both `comm` forgery and
cross-pane tmux driving stop mattering.

A cgroup-based variant reaches the same place without new accounts, but only if
each role's tmux server runs under a **root-owned** cgroup -- a system unit per
role rather than a user-delegated scope -- because systemd delegates the user
slice to the user, who may then move processes between their own cgroups.
`/proc/<pid>/cgroup` is world-readable, so a root-owned per-role cgroup would be
authoritative to the board.

Either option is an infrastructure change: new accounts or units, per-role tmux
servers and sockets, and a migration for existing tenants. Neither is a code
change inside the board.

Environment variables, prompt text, tokens readable by the same uid, and the
no-impersonation rule in the role skill are **not** enforcement and must never
be presented as isolation.
