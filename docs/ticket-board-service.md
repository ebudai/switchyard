# Ticket Board Service

The ticket board runs as an `agent` user `systemd` service on `127.0.0.1:8770`.

Prep artifacts for the future supervised cutover to a dedicated `boardsvc`
system service with PostgreSQL peer auth live in
`docs/ticket-board-boardsvc-peer-auth-runbook.md`. That package is review-only
until Eric runs the privileged cutover steps.

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

What this does:

- exports `origin/main` into `/home/agent/pgu-ticketboard-live/current`
- writes `~/.config/systemd/user/pgu-ticket-board.service`
- enables and starts `pgu-ticket-board.service`

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
- command: `python3 /home/agent/pgu-ticketboard-live/current/scripts/ticket-board.py --host 127.0.0.1 --port 8770`
- logs: `/tmp/pgu-ticket-board.log`

The source of truth for the live agent-user unit is
`scripts/ticket-board-service.sh render-unit`. The future dedicated `boardsvc`
system unit source is `deploy/systemd/pgu-ticket-board.service.boardsvc`; the
boardsvc runbook copies that file to `/etc/systemd/system/pgu-ticket-board.service`.
Both units must stay Postgres-only and must not pass the retired backend
selector CLI option.

## PostgreSQL notification listener

The post-cutover notification shim runs as a separate `agent` user systemd
service. It uses PostgreSQL `LISTEN ticket_board_state_transition`, maps each
state-transition payload to the appropriate `pgu-<role>:0.0` tmux pane, and
does not deliver `eric_review` transitions.

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
with read-only table access and no write-function grants. Eric still needs to
provision its password or equivalent local auth before starting the service. As
an interim deployment escape hatch, the env file can set
`TICKET_BOARD_NOTIFY_DATABASE_URL` to any read-only-capable board connection
string.

## Controlled deploy / restart

When the director wants merged board changes live, use:

```bash
scripts/ticket-board-service.sh deploy-restart
```

That command:

1. fetches the source repo
2. resolves `origin/main`
3. exports that exact SHA into `/home/agent/pgu-ticketboard-live/releases/<sha>`
4. repoints `/home/agent/pgu-ticketboard-live/current`
5. rewrites the unit
6. restarts the service

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

The old polling `director_watchdog.py` is retired. Ticket notification delivery
is split between PostgreSQL trigger/cron state and the
`pgu-ticket-board-notify-listener.service` user service. The listener handles
real-time `LISTEN` delivery, replay reconciliation after reconnect, and
NUDGE-message gating against live pane activity.

The listener no longer screen-scrapes pane output. Each agent CLI must run
`scripts/ticket-board-pane-idle-hook` from its lifecycle hooks so the listener
can read a per-pane state file from
`$PGU_TICKET_BOARD_PANE_STATE_DIR` (default:
`/run/user/<agent-uid>/pgu-ticket-board/pane-state`). Missing hook state fails
closed and defers delivery.

Hook commands may pass `--target pgu-<role>:0.0`, set `PGU_PANE_TARGET`, or let
the writer resolve the target from `TMUX_PANE`. The required state transitions
are:

- Claude Code: `Notification` with matcher `idle_prompt` writes `idle`;
  `UserPromptSubmit` and `Stop` write `busy`.
- Codex: `Stop` (or legacy `notify` event `agent-turn-complete`) writes
  `idle`; `UserPromptSubmit` writes `busy`; permission prompts write `blocked`
  if the hook surface exposes them.
- Gemini: `AfterAgent` writes `idle`; `BeforeAgent` writes `busy`. Do not use
  Gemini `Notification` for idle because it is alert/permission-oriented.

The director pane also uses tmux `client_activity` as a no-clobber latch. When
the hook state transitions to `idle`, the listener snapshots the director
client's activity timestamp. If human input advances it before delivery, the
notification is held through thinking pauses until the next `busy` -> `idle`
hook cycle. An abandoned draft releases after the listener's
`--director-composing-timeout-seconds` timeout.
