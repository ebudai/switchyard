# Otto Milestone 1 Hand Standup Record

Date: 2026-08-22

Ticket: PGU-590

Goal: stand up a second project named `otto` by hand, recording every manual
step and every break. The run did not reach a running otto team. That is useful:
the current tooling still requires privileged host setup and still carries PGU
workflow and identity assumptions that prevent a clean five-stage otto board.

## Inputs

- Project: `otto`
- Project user: `otto-agent`
- UID: `1005`
- Minimal team shape attempted: `director`, one implementer, `audit`
- Implementer role used in the scratch launcher plan: `ops`

`ops` was used because the current board schema does not have a generic
`implementer` role. Allowed implementer roles are still `main`, `app`, `ops`,
`perf`, and `research`.

## Command Log

All commands below were run from `/home/agent/wt-590` unless noted.

### 1. Verify prerequisite branch state

```bash
scripts/ticket-board-read PGU-590
scripts/ticket-board-read PGU-589
git fetch origin
git rev-parse --short origin/main
git log --oneline --decorate -5 origin/main
```

Result:

- PGU-590 was already `in_progress/ops`.
- PGU-589 was `done/director`.
- `origin/main` was `89f46a6`, which includes PGU-589.

### 2. Create isolated worktree

```bash
git worktree add -b feature/pgu-590-otto-milestone-hand-standup /home/agent/wt-590 origin/main
```

Result:

```text
Preparing worktree (new branch 'feature/pgu-590-otto-milestone-hand-standup')
branch 'feature/pgu-590-otto-milestone-hand-standup' set up to track 'origin/main'.
HEAD is now at 89f46a6 PGU-589 generate per-project polkit deploy rule
```

### 3. Inspect provisioner and workflow source

```bash
sed -n '1,620p' scripts/ticket_board/project_provision.py
sed -n '293,390p' scripts/ticket_board/schema.sql
sed -n '1,260p' config/team-launcher/pgu.json
sed -n '1,260p' docs/team-launcher.md
rg -n "workflow_stages|workflow_transitions|five-stage|triage|final sign|project workflow|seed workflow|stage_default" scripts tests docs -g '!**/__pycache__/**'
```

Findings:

- `ticket-board-provision-project` renders per-project unit names, socket path,
  database name, asset path, frame path, tmpfiles, polkit rule, DB bootstrap SQL,
  and operator commands.
- There is no per-project workflow seed selector.
- `schema.sql` seeds the live PGU workflow, not the requested five-stage default.

### 4. Render otto provisioning artifacts

```bash
scripts/ticket-board-provision-project --project otto --owner-user otto-agent --json
mkdir -p /home/agent/wt-590/.manual/otto-provision
scripts/ticket-board-provision-project --project otto --owner-user otto-agent --output-dir /home/agent/wt-590/.manual/otto-provision
ls -l .manual/otto-provision
```

Plan rendered:

```json
{
  "admin_database_url": "postgresql:///otto_ticket_board?host=/var/run/postgresql&user=postgres",
  "asset_dir": "/home/otto-agent/.claude/otto-tickets-assets",
  "board_current": "/home/otto-agent/otto-ticketboard-live/current",
  "board_database_url": "postgresql:///otto_ticket_board?host=/var/run/postgresql&user=ticket_board_service",
  "board_root": "/home/otto-agent/otto-ticketboard-live",
  "board_unit": "otto-ticket-board.service",
  "database": "otto_ticket_board",
  "frame_dir": "/home/otto-agent/.claude/otto-ticket-frames",
  "listener_unit": "otto-ticket-board-notify-listener.service",
  "owner_user": "otto-agent",
  "polkit_name": "49-otto-ticket-board-deploy.rules",
  "port": 20740,
  "runtime_directory": "otto-ticket-board",
  "socket_path": "/run/otto-ticket-board/ticket-board.sock"
}
```

Generated files:

```text
49-otto-ticket-board-deploy.rules
operator-commands.sh
otto-database.sql
otto-ticket-board-notify-listener.service
otto-ticket-board.conf
otto-ticket-board.service
plan.json
```

### 5. Verify host user/access

```bash
id otto-agent
getent passwd otto-agent
ls -ld /home/otto-agent /home/otto-agent/Projects 2>&1
find /home/otto-agent -maxdepth 2 -type d -name .git 2>&1 | head -20; echo rc=${PIPESTATUS[0]}
```

Result:

```text
uid=1005(otto-agent) gid=1005(otto-agent) groups=1005(otto-agent),990(kvm),942(adbusers)
otto-agent:x:1005:1005::/home/otto-agent:/bin/bash
drwx------ 1 otto-agent otto-agent 234 Aug 22 17:34 /home/otto-agent
ls: cannot access '/home/otto-agent/Projects': Permission denied
find: '/home/otto-agent': Permission denied
rc=1
```

Break:

- `agent` cannot create or inspect `/home/otto-agent/Projects/otto`.
- The checkout step requires either logging in as `otto-agent` or root-mediated
  directory creation/clone.

### 6. Check root/polkit availability

```bash
sudo -n true; echo sudo_rc=$?
scripts/team-launcher otto provision-runtime --runtime-user otto-agent; echo rc=$?
cd /home/agent/wt-590/.manual/otto-provision
sudo install -d -m 0755 '/home/otto-agent/otto-ticketboard-live'; echo rc=$?
```

Results:

```text
sudo: a password is required
sudo_rc=1
```

```text
team-launcher: failed to enable linger for 'otto-agent': Could not enable linger: Access denied as the requested operation requires interactive authentication. However, interactive authentication has not been enabled by the calling program.; run `sudo loginctl enable-linger otto-agent` and retry
rc=1
```

```text
sudo: a terminal is required to read the password; either use the -S option to read from standard input or configure an askpass helper
sudo: a password is required
rc=1
```

Break:

- The agent pane cannot perform the required root steps.
- The generated runbook is correct in shape, but it is not executable by the
  current ops pane without Eric/root involvement.

Root-required steps from the generated runbook:

```bash
sudo install -d -m 0755 '/home/otto-agent/otto-ticketboard-live'
sudo env TICKET_BOARD_PROJECT='otto' SOURCE_REPO='/home/agent/wt-590' BOARD_ROOT='/home/otto-agent/otto-ticketboard-live' DEPLOY_REF=origin/main TICKET_BOARD_SKIP_MIGRATIONS=1 '/home/agent/wt-590/scripts/ticket-board-service.sh' deploy
sudo setfacl -R -m u:boardsvc:rx '/home/otto-agent/otto-ticketboard-live'
sudo find '/home/otto-agent/otto-ticketboard-live' -type d -exec setfacl -m d:u:boardsvc:rx {} +
sudo install -d -m 0775 -o 'otto-agent' -g 'otto-agent' '/home/otto-agent/.claude/otto-tickets-assets' '/home/otto-agent/.claude/otto-ticket-frames'
sudo setfacl -m u:boardsvc:--x '/home/otto-agent'
sudo setfacl -m u:boardsvc:--x '/home/otto-agent/.claude'
sudo setfacl -R -m u:boardsvc:rwx '/home/otto-agent/.claude/otto-tickets-assets' '/home/otto-agent/.claude/otto-ticket-frames'
sudo install -m 0644 'otto-ticket-board.service' '/etc/systemd/system/otto-ticket-board.service'
sudo install -m 0644 'otto-ticket-board.conf' '/etc/tmpfiles.d/otto-ticket-board.conf'
sudo install -m 0644 '49-otto-ticket-board-deploy.rules' '/etc/polkit-1/rules.d/49-otto-ticket-board-deploy.rules'
sudo systemd-tmpfiles --create '/etc/tmpfiles.d/otto-ticket-board.conf'
sudo -u postgres psql -X -v ON_ERROR_STOP=1 -f 'otto-database.sql'
sudo TICKET_BOARD_ADMIN_DATABASE_URL='postgresql:///otto_ticket_board?host=/var/run/postgresql&user=postgres' '/home/otto-agent/otto-ticketboard-live/current/scripts/ticket-board-migrate'
sudo psql -X -v ON_ERROR_STOP=1 'postgresql:///otto_ticket_board?host=/var/run/postgresql&user=postgres' -f '/home/otto-agent/otto-ticketboard-live/current/scripts/ticket_board/rbac.sql'
sudo systemctl daemon-reload
sudo systemctl enable --now otto-ticket-board.service
sudo install -d -m 0755 -o 'otto-agent' -g 'otto-agent' '/home/otto-agent/.config/systemd/user'
sudo install -m 0644 -o 'otto-agent' -g 'otto-agent' 'otto-ticket-board-notify-listener.service' '/home/otto-agent/.config/systemd/user/otto-ticket-board-notify-listener.service'
sudo loginctl enable-linger 'otto-agent'
sudo -u 'otto-agent' env XDG_RUNTIME_DIR=/run/user/$(id -u 'otto-agent') systemctl --user daemon-reload
sudo -u 'otto-agent' env XDG_RUNTIME_DIR=/run/user/$(id -u 'otto-agent') systemctl --user enable --now otto-ticket-board-notify-listener.service
curl -fsS http://127.0.0.1:20740/api/board >/dev/null
```

Root is needed for:

- creating directories under another user's `0700` home;
- granting `boardsvc` ACL traversal and write access;
- installing systemd, tmpfiles, and polkit files under `/etc`;
- creating the PostgreSQL database and applying schema/RBAC as `postgres`;
- `systemctl daemon-reload` and enabling the system unit;
- enabling linger and installing/starting the owner user's listener unit.

### 7. Check whether a non-root database path exists

```bash
psql -X -v ON_ERROR_STOP=1 'postgresql:///postgres?host=/var/run/postgresql' -c 'SELECT current_user, current_database();'; echo rc=$?
createdb -h /var/run/postgresql otto_ticket_board_probe; echo rc=$?
psql -X -v ON_ERROR_STOP=1 'postgresql:///otto_ticket_board?host=/var/run/postgresql&user=ticket_board_service' -c 'SELECT 1'; echo rc=$?
```

Results:

```text
current_user | current_database
-------------+-----------------
agent        | postgres
rc=0
```

```text
createdb: error: database creation failed: ERROR:  permission denied to create database
rc=1
```

```text
psql: error: connection to server on socket "/var/run/postgresql/.s.PGSQL.5432" failed: FATAL:  database "otto_ticket_board" does not exist
rc=2
```

Break:

- `agent` can connect to the default `postgres` database but cannot create an
  otto database.
- `otto_ticket_board` does not exist yet.

### 8. Verify workflow seed against live schema

```bash
psql -X -v ON_ERROR_STOP=1 'postgresql://ticket_board_service@/pgu?host=/run/postgresql' -c "SELECT current_user, current_database(); SELECT name, display_label, rank, owner_roles FROM ticket_board.workflow_stages ORDER BY rank; SELECT from_stage, to_stage, action_name, allowed_roles, owner_scoped, director_override FROM ticket_board.workflow_transitions WHERE from_stage IN ('draft','analysis','in_progress','audit','director_review') ORDER BY from_stage, to_stage, action_name LIMIT 40;"
```

Result:

```text
draft           | Draft          | 0  | {}
backlog         | Backlog        | 1  | {}
analysis        | Triage         | 2  | {director}
in_progress     | Implementation | 3  | {main,app,ops,perf,research}
inspection      | Inspection     | 4  | {inspector}
audit           | Audit          | 5  | {audit}
dat             | DAT            | 6  | {director}
user_review     | UAT            | 7  | {director}
director_review | Final Sign-Off | 8  | {director}
done            | Done           | 9  | {}
cancelled       | Cancelled      | 10 | {}
```

Follow-up query:

```bash
psql -X -v ON_ERROR_STOP=1 'postgresql://ticket_board_service@/pgu?host=/run/postgresql' -c "SELECT false AS has_generic_five_stage WHERE EXISTS (SELECT 1 FROM ticket_board.workflow_stages WHERE name IN ('triage','implementation','final_signoff')); SELECT count(*) AS stage_count FROM ticket_board.workflow_stages; SELECT name FROM ticket_board.workflow_stages ORDER BY rank;"
```

Result:

```text
has_generic_five_stage
----------------------
(0 rows)

stage_count
-----------
11

draft
backlog
analysis
in_progress
inspection
audit
dat
user_review
director_review
done
cancelled
```

Break:

- The requested five-stage default is not seedable through the provisioner.
- The schema also hard-checks stage names, so replacing PGU's workflow with
  `draft -> triage -> implementation -> audit -> final_signoff` is not just data
  insertion. It needs schema/function/API support for project-specific stages or
  a canonical generic state vocabulary.

### 9. Build and dry-run minimal launcher plan

Scratch config used:

```json
{
  "layout": "otto-konsole-layout.json",
  "project": "otto",
  "repository": "/home/otto-agent/Projects/otto",
  "session_dir": "/home/otto-agent/.local/state/otto-ticket-board/pane-sessions",
  "worktree_branch": "main",
  "worktree_remote": "origin",
  "roles": ["director", "ops", "audit"]
}
```

The actual scratch JSON included full CLI/model/env fields. Each role had:

- `TICKET_BOARD_PROJECT=otto`
- `TICKET_BOARD_SOCKET=/run/otto-ticket-board/ticket-board.sock`
- `TICKET_BOARD_URL=http://127.0.0.1:20740`
- `TICKET_BOARD_CALLER_ROLE=<role>`

Command:

```bash
scripts/team-launcher otto start --config /home/agent/wt-590/.manual/otto-launcher/otto.json --dry-run --layout-output /home/agent/wt-590/.manual/otto-launcher/rendered-layout.json
```

Result:

```json
{
  "mode": "attach-or-start",
  "project": "otto",
  "run_as_user": "agent",
  "worktree_ref": "origin/main",
  "roles": [
    {
      "role": "director",
      "target": "otto-director:0.0",
      "tmux_session": "otto-director",
      "workdir": "/home/otto-agent/Projects/otto"
    },
    {
      "role": "ops",
      "target": "otto-ops:0.0",
      "tmux_session": "otto-ops",
      "workdir": "/home/otto-agent/Projects/otto"
    },
    {
      "role": "audit",
      "target": "otto-audit:0.0",
      "tmux_session": "otto-audit",
      "workdir": "/home/otto-agent/Projects/otto"
    }
  ]
}
```

Break:

- The launcher dry-run is structurally valid.
- Because the command was invoked by `agent`, it reports `run_as_user: agent`.
  The scratch config intentionally omits `run_as_user`; a real otto launch must
  be invoked as `otto-agent` so runtime dirs, tmux sessions, and session state
  are owned by `otto-agent`.
- Adding `run_as_user: otto-agent` would make the wrapper immediately call
  `sudo -u otto-agent -H`, which this pane cannot satisfy.
- The implementer is still called `ops`; a generic `implementer` pane cannot
  move tickets through the current write API because the schema/RBAC/action
  matrix do not define it.

### 10. Reboot/resume proof

Not attempted.

Reason:

- The board was not stood up.
- PGU-590 explicitly says not to reboot without timing agreed with Eric.
- No reboot coordination was present in this run.

## PGU/User Assumptions That Leaked

- Ticket IDs are still constrained to `PGU-N`:
  `tickets.id CHECK (id ~ '^PGU-[0-9]+$')`.
- Parent IDs are still constrained to `PGU-N`.
- The write client still sends `X-PGU-Caller-Role`.
- The default socket fallback is still `/run/pgu-ticket-board/ticket-board.sock`.
- The README still describes PGU defaults and PGU ticket JSON naming.
- Board roles are still PGU team roles: `main`, `app`, `ops`, `perf`,
  `research`, `audit`, `inspector`, `director`, `user`.
- The workflow has PGU-specific optional gates: `inspection`, `dat`,
  `user_review`.
- The source schema header still says `PGU ticket-board PostgreSQL schema`.
- Team launcher docs still mention PGU-specific runtime fallback directories,
  although generic env names now exist.

## Manual Fixes Required Before Otto Can Run

1. Decide whether `otto` should really use `/home/otto-agent/Projects/otto`.
2. As root or `otto-agent`, create the otto checkout.
3. Run the generated provisioning runbook from `.manual/otto-provision` or a
   regenerated equivalent.
4. Add project-specific workflow seeding support, or define that all projects
   use the current internal state names while only labels are generic.
5. Add/allow a generic implementer role, or document that the minimal generic
   team must still pick one of `main/app/ops/perf/research`.
6. Launch as `otto-agent`, not `agent`, after linger/runtime is enabled.
7. Only after the otto board and panes are live, create a proof ticket and move
   it through implementation -> audit -> final sign-off while checking delivery.
8. Coordinate a reboot window with Eric, then verify session resume.

## Automation Gaps for the Package

- Privileged setup is not encapsulated into a single audited root helper.
- Provisioning renders commands, but does not execute/checkpoint them with
  resumable status.
- The provisioner cannot initialize a project-specific workflow.
- The schema cannot currently represent the requested five-stage state names.
- Role provisioning is not generic enough for "implementer/auditor/director".
- The launcher bootstrap template emits an empty roles list and retains the live
  PGU session-dir default; a new project needs hand-authored role/env entries.
- There is no "validate this project can be launched as its intended user"
  preflight that checks home permissions, repo path access, linger, runtime dir,
  socket path, and board DB existence in one report.

## Status

PGU-590 did not produce a running otto board or team. It produced the intended
measurement record and identified the first hard blockers:

- no non-interactive root path from the ops pane;
- no access to `otto-agent` home from `agent`;
- no `otto_ticket_board` database;
- no project-specific five-stage workflow seed;
- no generic implementer role.
