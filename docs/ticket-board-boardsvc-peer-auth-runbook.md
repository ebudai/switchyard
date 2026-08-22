# Ticket Board boardsvc Peer-Auth Cutover

PGU-197 is a prep-only package. Do not run these commands until the supervised
cutover. The goal is to run `pgu-ticket-board.service` as a dedicated `boardsvc`
OS user and let PostgreSQL peer-auth that process to the `ticket_board_service`
DB role without any board password on disk.

This package targets the PGU-198/PGU-209 single-writer board: the service runs as
OS user `boardsvc`, peer-authenticates to PostgreSQL as `ticket_board_service`,
and reads/writes tickets through the database. It intentionally does not grant
`boardsvc` access to `/home/agent/.claude/pgu-tickets`; live tickets come from
PostgreSQL. Attachment files and captured frames remain filesystem-backed under
`/home/agent/.claude/pgu-tickets-assets` and `/tmp/pgu-frames`.

## Artifacts

- `scripts/ticket-board-boardsvc-setup.sh`: root-only setup helper. It prints
  the command plan by default and only changes the host with `--apply`.
- `deploy/systemd/pgu-ticket-board.service.boardsvc`: proposed system service;
  copy to `/etc/systemd/system/pgu-ticket-board.service` during cutover.
- `deploy/systemd/pgu-ticket-board-canary.service`: fixed system canary unit;
  copy to `/etc/systemd/system/pgu-ticket-board-canary.service` during cutover.
- `deploy/tmpfiles/pgu-ticket-board.conf`: boot-time recreation for the shared
  `/tmp/pgu-frames` inbox; install it to `/etc/tmpfiles.d/`.
- `deploy/polkit/49-pgu-board-deploy.rules`: narrow deploy authorization for
  `agent`; it permits live board start/stop/restart and fixed
  `pgu-ticket-board-canary.service` start/stop so `deploy-restart` can keep its canary
  health gate without blanket sudo.
- This runbook: root command list, verification, and rollback.

## Root Commands

Set `PG_DATABASE` to the real ticket-board database name before running the
second command. The proposed unit also defaults to `PGDATABASE=pgu`; edit it if
production uses a different database.

```bash
# 1. Create the dedicated non-login service user.
id -u boardsvc >/dev/null 2>&1 || sudo useradd -r -M -d /nonexistent -s /usr/sbin/nologin boardsvc

# 2. Add local peer auth, reload Postgres, and grant deploy/assets/frames ACLs.
sudo PG_DATABASE=pgu scripts/ticket-board-boardsvc-setup.sh --apply

# 3. Install the reviewed system unit, tmpfiles entry, and deploy polkit rule.
sudo install -m 0644 deploy/systemd/pgu-ticket-board.service.boardsvc /etc/systemd/system/pgu-ticket-board.service
sudo install -m 0644 deploy/systemd/pgu-ticket-board-canary.service /etc/systemd/system/pgu-ticket-board-canary.service
sudo install -m 0644 deploy/tmpfiles/pgu-ticket-board.conf /etc/tmpfiles.d/pgu-ticket-board.conf
sudo install -m 0644 deploy/polkit/49-pgu-board-deploy.rules /etc/polkit-1/rules.d/49-pgu-board-deploy.rules
sudo systemd-tmpfiles --create /etc/tmpfiles.d/pgu-ticket-board.conf

# 4. Reload systemd and start the board under boardsvc.
sudo systemctl daemon-reload
sudo systemctl enable --now pgu-ticket-board.service
```

Before installing the unit, verify the deployed code includes the Postgres-only
board and `scripts/ticket-board.py --help` shows `--database` and `--assets`.
This prep package deliberately does not make the retired JSON store readable or
writable by `boardsvc`.

## What The Setup Does

The user step creates `boardsvc` as a system account with no home directory and
`/usr/sbin/nologin`, so it exists only to run the board service.

The PostgreSQL step discovers `pg_hba.conf` and `pg_ident.conf` with `SHOW
hba_file` / `SHOW ident_file`, appends these idempotent rules, and reloads
Postgres:

```text
local   pgu   ticket_board_service   peer map=pgu_ticket_board_service
pgu_ticket_board_service   boardsvc   ticket_board_service
```

The deploy/assets/frames step uses ACLs:

```bash
sudo setfacl -m u:boardsvc:--x /home/agent
sudo setfacl -m u:boardsvc:--x /home/agent/.claude
sudo setfacl -R -m u:boardsvc:rx /home/agent/pgu-ticketboard-live
find /home/agent/pgu-ticketboard-live -type d -exec sudo setfacl -m d:u:boardsvc:rx {} +
sudo mkdir -p /home/agent/.claude/pgu-tickets-assets /tmp/pgu-frames
sudo setfacl -R -m u:boardsvc:rwx /home/agent/.claude/pgu-tickets-assets /tmp/pgu-frames
find /home/agent/.claude/pgu-tickets-assets /tmp/pgu-frames -type d \
  -exec sudo setfacl -m d:u:boardsvc:rwx {} +
```

The tmpfiles entry recreates the shared frame inbox on boot before the board
starts:

```text
d /tmp/pgu-frames 1777 root root -
```

Do not add an ACL for `/home/agent/.claude/pgu-tickets` unless the supervised
cutover explicitly abandons the DB-backed ticket-store plan.

## Verify

Confirm the OS user cannot log in interactively:

```bash
getent passwd boardsvc
sudo -u boardsvc /usr/sbin/nologin; test "$?" -ne 0
```

Confirm peer auth maps `boardsvc` to `ticket_board_service` without a password:

```bash
sudo -u boardsvc env PGHOST=/var/run/postgresql PGDATABASE=pgu PGUSER=ticket_board_service \
  psql -Atqc "select current_user;"
# expected: ticket_board_service
```

Capture the expected real ticket count from Postgres before the restart:

```bash
EXPECTED_TICKETS="$(sudo -u boardsvc env PGHOST=/var/run/postgresql PGDATABASE=pgu PGUSER=ticket_board_service \
  psql -Atqc "select count(*) from ticket_board.tickets;")"
test "$EXPECTED_TICKETS" -gt 0
```

Confirm the board is running under the dedicated user:

```bash
systemctl status pgu-ticket-board.service
systemctl show pgu-ticket-board.service -p User -p FragmentPath
pgrep -a -u boardsvc -f 'scripts/ticket-board.py'
curl -fsS http://127.0.0.1:8770/api/board >/dev/null
curl -fsS http://127.0.0.1:8770/api/board | python3 -c 'import json,sys
data=json.load(sys.stdin)
assert data["store_backend"] == "postgres", data["store_backend"]'
BOARD_TICKETS="$(curl -fsS http://127.0.0.1:8770/api/board | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["tickets"]))')"
test "$BOARD_TICKETS" = "$EXPECTED_TICKETS"
```

Confirm `/tmp/pgu-frames` is present again even after deleting it:

```bash
sudo rm -rf /tmp/pgu-frames
sudo systemd-tmpfiles --create /etc/tmpfiles.d/pgu-ticket-board.conf
test -d /tmp/pgu-frames
test "$(stat -c '%a' /tmp/pgu-frames)" = 1777
```

Confirm the board reports explicit non-HOME runtime paths:

```bash
curl -fsS http://127.0.0.1:8770/api/board | python3 -c 'import json,sys
data=json.load(sys.stdin)
assert data["frame_dir"] == "/tmp/pgu-frames", data["frame_dir"]
assert data["asset_dir"] == "/home/agent/.claude/pgu-tickets-assets", data["asset_dir"]'
```

Confirm there is no password-bearing board credential in `agent`-readable space:

```bash
sudo systemctl cat pgu-ticket-board.service
sudo -u agent sh -lc 'env | grep -E "PGPASSWORD|DATABASE_URL" || true'
```

## Roll Back

Stop the system service and restore the previous agent user service:

```bash
sudo systemctl disable --now pgu-ticket-board.service
sudo rm -f /etc/systemd/system/pgu-ticket-board.service
sudo systemctl daemon-reload
sudo -u agent XDG_RUNTIME_DIR=/run/user/$(id -u agent) \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$(id -u agent)/bus \
  systemctl --user enable --now pgu-ticket-board.service
```

Remove the peer-auth lines added for `pgu_ticket_board_service` from
`pg_hba.conf` and `pg_ident.conf`, then reload Postgres:

```bash
sudo -u postgres psql -Atqc "select pg_reload_conf();"
```

Remove the deploy ACLs if the system service will not be retried:

```bash
sudo setfacl -x u:boardsvc /home/agent
sudo setfacl -R -x u:boardsvc -x d:u:boardsvc /home/agent/pgu-ticketboard-live
sudo setfacl -R -x u:boardsvc -x d:u:boardsvc /home/agent/.claude/pgu-tickets-assets /tmp/pgu-frames
```
