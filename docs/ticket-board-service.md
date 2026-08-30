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
- on Arch/CachyOS, install it for the system Python with:

```bash
sudo pacman -S python-psycopg
```

The system Python is PEP-668 externally managed, so avoid plain
`pip install --user psycopg`. For isolated verification runs, create a venv
with system packages visible, for example:

```bash
python3 -m venv --system-site-packages /tmp/pgu-ticket-board-venv
/tmp/pgu-ticket-board-venv/bin/python tests/ticket_board_postgres_backend_test.py
```

The service runs:

- working directory: `/home/agent/pgu-ticketboard-live/current`
- command: `python3 /home/agent/pgu-ticketboard-live/current/scripts/ticket-board.py --host 127.0.0.1 --port 8770 --unix-socket /run/pgu-ticket-board/ticket-board.sock`
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
namespace, or `--group all` for both groups. The adjacent group is discovered
from test file content, not a manifest: standalone tests that reference board
URLs, sockets, `ticket-board-write`, or `ticket_board.write_client` are included
automatically. Use `--fail-on-skip` when the validation environment is expected
to have every dependency installed.

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
  `pre_llm_call` writes `busy`; `post_llm_call`, `on_session_end`, and
  `on_session_finalize` are turn-end idle sources. `on_session_start` only
  seeds initial idle and the resume id, so it still needs the normal
  pane-content corroboration before delivery.

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
