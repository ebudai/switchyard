# Otto Milestone 2 Standup Runbook

Date: 2026-08-22

Tickets: PGU-590, PGU-601

Goal: stand up the second project board named `otto` for real, using the
Milestone 1 findings and the fixes that landed in PGU-597, PGU-598, and
PGU-599.

This document replaces the Milestone 1 failure log with the working install
guide. The remaining split is intentional:

- Team/ops prepares and reviews artifacts without root.
- Eric runs the single privileged command block.
- Team verifies isolation, workflow traversal, and notifications.

## Current Design

- Project: `otto`
- Project user: `otto-agent`
- Minimal board roles: `designer`, `director`, `audit`, `ops`, `app`, `main`,
  plus `user` for draft release compatibility.
- Database: `otto_ticket_board`
- HTTP port: `20740`
- System board unit: `otto-ticket-board.service`
- User notification unit: `otto-ticket-board-notify-listener.service`
- Unix write socket: `/run/otto-ticket-board/ticket-board.sock`
- Asset directory: `/home/otto-agent/.claude/otto-tickets-assets`
- Frame inbox: `/home/otto-agent/.claude/otto-ticket-frames`

The database still uses the internal state keys supported by the shared board
schema:

| Visible Stage | Internal Key |
| --- | --- |
| Draft | `draft` |
| Triage | `analysis` |
| Implementation | `in_progress` |
| Audit | `audit` |
| Final Sign-Off | `director_review` |

PGU-598 seeds those five visible stages for non-`pgu` projects. PGU-599 makes
the implementation-stage owner role data-driven, and PGU-655 makes the turnkey
defaults `designer`, `director`, `audit`, `ops`, `app`, and `main`, with
Implementation owned by the programmer roles `main` and `app`; `main` is first
by convention for the default walkthrough.

## Preconditions

These commits must be in the checkout Eric uses to run the block:

```bash
git log --oneline -5 origin/main
```

Required commits:

- `6d9cd4c` or later for project ticket prefixes and generic ticket headers.
- `70f2f0e` or later for per-project workflow seeding.
- `608ae70` or later for the generic implementation role.

Confirm the user exists and remains private:

```bash
id otto-agent
getent passwd otto-agent
ls -ld /home/otto-agent
```

Expected: `/home/otto-agent` may be `0700`; that is correct. The team should
not try to write into it directly.

## Team Prep

Run this from the merged checkout, as the normal team user:

```bash
cd /home/agent/Projects/pgu
git fetch origin main
git status --short
mkdir -p /tmp/otto-board-provision
rm -f /tmp/otto-board-provision/*
scripts/ticket-board-provision-project \
  --project otto \
  --owner-user otto-agent \
  --output-dir /tmp/otto-board-provision
sed -n '1,260p' /tmp/otto-board-provision/plan.json
sed -n '1,260p' /tmp/otto-board-provision/operator-commands.sh
sed -n '1,220p' /tmp/otto-board-provision/otto-workflow.sql
```

Review points:

- `plan.json` has `port: 20740`, database `otto_ticket_board`, and socket
  `/run/otto-ticket-board/ticket-board.sock`.
- `draft_roles` is `["designer"]`.
- `implementer_roles` is `["app", "main"]`.
- `assignee_roles` is `["unassigned", "designer", "ops", "app", "main", "audit", "director", "user"]`.
- `caller_roles` is `["director", "designer", "ops", "app", "main", "audit", "user"]`.
- `otto-workflow.sql` contains exactly five rows in `workflow_stages` and seven
  rows in `workflow_transitions`.
- The generated system unit exports `TICKET_BOARD_DRAFT_ROLES`,
  `TICKET_BOARD_IMPLEMENTER_ROLES`, `TICKET_BOARD_ASSIGNEES`, and
  `TICKET_BOARD_CALLER_ROLES` matching those role lists.

## Eric Privileged Block

Run this as Eric from a shell with sudo access. It regenerates artifacts from
the current checkout, then runs the reviewed operator commands from that
artifact directory.

```bash
set -euo pipefail

cd /home/agent/Projects/pgu
git fetch origin main
git checkout main
git merge --ff-only origin/main

PROVISION_DIR="$(mktemp -d /tmp/otto-board-provision.XXXXXX)"

scripts/ticket-board-provision-project \
  --project otto \
  --owner-user otto-agent \
  --output-dir "$PROVISION_DIR"

cd "$PROVISION_DIR"
bash operator-commands.sh
```

The generated `operator-commands.sh` performs the privileged work:

- creates `/home/otto-agent/otto-ticketboard-live`;
- deploys the board release from the current source checkout;
- grants `boardsvc` read/execute access to the release and read/write access to
  otto assets/frame directories;
- installs the systemd, tmpfiles, and polkit artifacts;
- creates `otto_ticket_board`;
- runs migrations;
- applies `otto-workflow.sql`;
- applies `rbac.sql`;
- starts `otto-ticket-board.service`;
- installs and starts the `otto-agent` user notification listener;
- verifies `http://127.0.0.1:20740/api/board`.

## Post-Install Verification

Run these after Eric's block returns successfully.

```bash
curl -fsS http://127.0.0.1:8770/api/board >/dev/null
curl -fsS http://127.0.0.1:20740/api/board >/dev/null

psql -X -v ON_ERROR_STOP=1 \
  'postgresql:///otto_ticket_board?host=/var/run/postgresql&user=ticket_board_service' \
  -c "SELECT current_user, current_database();"

psql -X -v ON_ERROR_STOP=1 \
  'postgresql:///otto_ticket_board?host=/var/run/postgresql&user=ticket_board_service' \
  -c "SELECT name, display_label, rank, owner_roles FROM ticket_board.workflow_stages ORDER BY rank;
      SELECT from_stage, to_stage, action_name, allowed_roles
      FROM ticket_board.workflow_transitions
      ORDER BY from_stage, to_stage, action_name;"
```

Expected workflow stages:

```text
draft           Draft           {designer}
analysis        Triage          {director}
in_progress     Implementation  {main,app}
audit           Audit           {audit}
director_review Final Sign-Off  {director}
```

Expected isolation:

- PGU remains on port `8770`; otto is on `20740`.
- PGU database is `pgu`; otto database is `otto_ticket_board`.
- PGU socket is `/run/pgu-ticket-board/ticket-board.sock`; otto socket is
  `/run/otto-ticket-board/ticket-board.sock`.
- PGU units and otto units have distinct names.

## Workflow Proof

Use the otto board write socket/URL. The created ticket id should use the
project prefix `OTTO`.

```bash
export TICKET_BOARD_URL=http://127.0.0.1:20740
export TICKET_BOARD_SOCKET=/run/otto-ticket-board/ticket-board.sock

scripts/ticket-board-write create-ticket \
  --caller-role user \
  --title "Otto workflow proof" \
  --body "PGU-601 proof ticket." \
  --state draft

# Replace OTTO-N with the created ticket id.
scripts/ticket-board-write release-draft OTTO-N \
  --caller-role designer

scripts/ticket-board-write route OTTO-N \
  --caller-role director \
  --state in_progress \
  --assignee ops

scripts/ticket-board-write submit-to-audit OTTO-N \
  --caller-role ops \
  --commit-hash 608ae70

scripts/ticket-board-write audit-sign-off OTTO-N \
  --caller-role audit \
  --text "Audit accepts the otto workflow proof."

scripts/ticket-board-read --board-url http://127.0.0.1:20740 OTTO-N
```

The final read should show state `director_review` and assignee `director`.

Notification proof:

```bash
psql -X -v ON_ERROR_STOP=1 \
  'postgresql:///otto_ticket_board?host=/var/run/postgresql&user=ticket_board_service' \
  -c "SELECT ticket_id, target_role, kind, event, ticket_state_at_event, ticket_assignee_at_event
      FROM ticket_board.notification_trace
      WHERE ticket_id = 'OTTO-N'
      ORDER BY ts, id;"
```

Expected trace has sends or queued delivery attempts for:

- `director` when the ticket enters triage/final sign-off;
- `designer` when a draft ticket is created;
- `ops` when the ticket enters implementation;
- `audit` when the ticket enters audit.

## Launcher Notes

Do not launch otto panes from the `agent` user. A real otto team launch should
be invoked as `otto-agent`, after the privileged block has enabled linger and
created `/run/user/1005`.

Minimal otto launcher config shape:

```json
{
  "project": "otto",
  "repository": "/home/otto-agent/Projects/otto",
  "worktree_branch": "main",
  "worktree_remote": "origin",
  "roles": [
    {
      "role": "designer",
      "slot": 0,
      "tmux_session": "otto-designer",
      "target": "otto-designer:0.0",
      "cli": ["claude"],
      "live_commands": ["claude"],
      "resume_mode": "flag",
      "resume_flag": "--resume",
      "yolo": true,
      "env": {
        "TICKET_BOARD_PROJECT": "otto",
        "TICKET_BOARD_URL": "http://127.0.0.1:20740",
        "TICKET_BOARD_SOCKET": "/run/otto-ticket-board/ticket-board.sock",
        "TICKET_BOARD_CALLER_ROLE": "designer"
      }
    },
    {
      "role": "director",
      "slot": 1,
      "tmux_session": "otto-director",
      "target": "otto-director:0.0",
      "cli": ["claude"],
      "live_commands": ["claude"],
      "resume_mode": "flag",
      "resume_flag": "--resume",
      "yolo": true,
      "env": {
        "TICKET_BOARD_PROJECT": "otto",
        "TICKET_BOARD_URL": "http://127.0.0.1:20740",
        "TICKET_BOARD_SOCKET": "/run/otto-ticket-board/ticket-board.sock",
        "TICKET_BOARD_CALLER_ROLE": "director"
      }
    },
    {
      "role": "audit",
      "slot": 2,
      "tmux_session": "otto-audit",
      "target": "otto-audit:0.0",
      "cli": ["claude"],
      "live_commands": ["claude"],
      "resume_mode": "flag",
      "resume_flag": "--resume",
      "yolo": true,
      "env": {
        "TICKET_BOARD_PROJECT": "otto",
        "TICKET_BOARD_URL": "http://127.0.0.1:20740",
        "TICKET_BOARD_SOCKET": "/run/otto-ticket-board/ticket-board.sock",
        "TICKET_BOARD_CALLER_ROLE": "audit"
      }
    },
    {
      "role": "ops",
      "slot": 3,
      "tmux_session": "otto-ops",
      "target": "otto-ops:0.0",
      "cli": ["codex"],
      "live_commands": ["codex"],
      "resume_mode": "subcommand",
      "resume_subcommand": "resume",
      "yolo": true,
      "env": {
        "TICKET_BOARD_PROJECT": "otto",
        "TICKET_BOARD_URL": "http://127.0.0.1:20740",
        "TICKET_BOARD_SOCKET": "/run/otto-ticket-board/ticket-board.sock",
        "TICKET_BOARD_CALLER_ROLE": "ops"
      }
    }
  ]
}
```

This config is a shape reference, not a committed active config. Use verified
CLI/model choices before starting a real pane set.

## Status

PGU-590 stopped at these hard blockers:

- no non-interactive root path from the ops pane;
- no access to `/home/otto-agent` from `agent`;
- no `otto_ticket_board` database;
- no project-specific workflow seed;
- no generic implementer role.

As of PGU-601, the last three are fixed in code. The remaining required action
is Eric running the privileged block above, then the team running the workflow
and notification proof.
