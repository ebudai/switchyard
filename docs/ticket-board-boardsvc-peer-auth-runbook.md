# Ticket Board boardsvc Peer-Auth Cutover

PGU-197 is a prep-only package. Do not run these commands until the supervised
cutover. The goal is to run `pgu-ticket-board.service` as a dedicated `boardsvc`
OS user and let PostgreSQL peer-auth that process to the `ticket_board_service`
DB role without any board password on disk.

## Artifacts

- `scripts/ticket-board-boardsvc-setup.sh`: root-only setup helper. It prints
  the command plan by default and only changes the host with `--apply`.
- `deploy/systemd/pgu-ticket-board.service.boardsvc`: proposed system service;
  copy to `/etc/systemd/system/pgu-ticket-board.service` during cutover.
- This runbook: root command list, verification, and rollback.

## Root Commands

Set `PG_DATABASE` to the real ticket-board database name before running the
second command. The proposed unit also defaults to `PGDATABASE=pgu`; edit it if
production uses a different database.

```bash
# 1. Create the dedicated non-login service user.
id -u boardsvc >/dev/null 2>&1 || sudo useradd -r -M -d /nonexistent -s /usr/sbin/nologin boardsvc

# 2. Add local peer auth for boardsvc -> ticket_board_service and reload Postgres.
sudo PG_DATABASE=pgu scripts/ticket-board-boardsvc-setup.sh --apply

# 3. Install the proposed system unit and start the board under boardsvc.
sudo install -m 0644 deploy/systemd/pgu-ticket-board.service.boardsvc /etc/systemd/system/pgu-ticket-board.service
sudo systemctl daemon-reload
sudo systemctl enable --now pgu-ticket-board.service
```

The setup helper also applies ACLs so `boardsvc` can traverse `/home/agent` and
read/execute `/home/agent/pgu-ticketboard-live` while preserving the existing
`agent` ownership model for deploy exports.

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

The deploy-tree step uses ACLs:

```bash
sudo setfacl -m u:boardsvc:--x /home/agent
sudo setfacl -R -m u:boardsvc:rx /home/agent/pgu-ticketboard-live
find /home/agent/pgu-ticketboard-live -type d -exec sudo setfacl -m d:u:boardsvc:rx {} +
```

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

Confirm the board is running under the dedicated user:

```bash
systemctl status pgu-ticket-board.service
systemctl show pgu-ticket-board.service -p User -p FragmentPath
pgrep -a -u boardsvc -f 'scripts/ticket-board.py'
curl -fsS http://127.0.0.1:8770/api/snapshot >/dev/null
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
```
