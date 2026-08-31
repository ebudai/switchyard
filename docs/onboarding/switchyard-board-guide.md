# Switchyard Board Guide

How to use the ticket board and `directorctl`. This is the **mechanics** doc: what the
stages are, who may move a ticket where, and how to read and write from a pane.

For *why* the system is shaped this way, see
`adversarial-collaborative-methodology.md`. For how to run the pipeline as director,
see `switchyard-director-guide.md`. For deploying and operating the board service itself,
see the Switchyard source checkout's `docs/ticket-board-service.md`.

This guide is project-agnostic. Examples use `PGU-123`; your project has its own prefix.

---

## The model

One board per project. Every piece of work is a ticket. A ticket lives in exactly one
**stage**, is assigned to a **role** or explicitly unassigned, and moves between stages
only along declared **transitions**. Nothing moves by editing a field — movement is
always an operation with a name, and the board records who performed it.

Roles are per-project. A typical set: `director` (coordinates, reviews, merges), one or
more implementers, `audit` (verifies), `inspector` (visual review), `user` (the
human), and optional support roles. Conventional implementer names are `main` for
core/domain implementation and integration, `ops` for environment, services, tooling,
and infrastructure, `app` for application/UI work, `research` for investigation and
design support, and `perf` for measurement and performance work.

---

## The pipeline

| Stage | Board label | Who owns it |
|---|---|---|
| `draft` | Draft | — |
| `backlog` | Backlog | — |
| `analysis` | Triage | director |
| `in_progress` | Implementation | implementers |
| `inspection` | Inspection | inspector |
| `audit` | Audit | audit |
| `dat` | DAT | director |
| `user_review` | UAT | user |
| `director_review` | Final Sign-Off | director |
| `done` / `cancelled` | Done / Cancelled | terminal |

The normal path is **backlog → analysis → in_progress → audit → director_review → done**.

`inspection`, `audit` and `dat` are **gated**: a ticket only enters them if the
corresponding flag is set, otherwise it skips straight past. `user_review` uses the
same UAT gate on the post-DAT path, but the director may also route an analysis
ticket there when the user needs to answer a question.

| Stage | Entry gate | Skips to when unset |
|---|---|---|
| `inspection` | `needs_inspection` | `audit` |
| `audit` | `needs_audit` | `dat` |
| `dat` | `needs_user_signoff` | `director_review` |
| `user_review` | `needs_user_signoff` | `director_review` |

Set those flags when you **create** the ticket (`--needs-audit`, `--needs-inspection`,
`--needs-user-signoff`). The director can adjust gate flags later with `edit-fields`;
other roles are restricted.

---

## Who may do what

Permissions are enforced in the database, not just the UI. An operation you are not
entitled to perform is refused with an error naming the constraint — that refusal is the
system working, not a bug to route around.

**Implementers** (`main`, `app`, `ops`, …)
- `start-work` — backlog/analysis → in_progress
- `submit-to-audit` — in_progress → audit (with `--commit-hash`)
- `submit-to-inspection` — in_progress → inspection
- `implementer-kick-back` — in_progress → analysis, when the ask is not implementable
- `request-commit-exempt` — in_progress → analysis, for work with no commit
- `file-bug` — create a defect ticket without going through the director. The new ticket
  is linked to the source ticket and lands in `analysis`.

**Audit**
- `audit-sign-off` — audit → dat (or → director_review when the DAT gate is unset)
- `audit-kick-back` — audit → in_progress
- `file-bug` — create a defect ticket without going through the director. The new ticket
  is linked to the source ticket and lands in `analysis`.
- Audit **cannot** re-assign or route. It is a gate, not an implementer, and it is
  deliberately not an owner of any implementation stage.

**Inspector**
- `inspector-sign-off` — inspection → audit
- `inspector-kick-back` — inspection → in_progress

**Director**
- `route` — the general move, from almost anywhere to almost anywhere. Director-only.
- `defer` — → backlog
- `cancel` — → cancelled
- `mark-done` — director_review → done (with `--commit-hash`)
- `director-dat-sign-off` / `director-dat-kick-back`
- `set-blockers`, `set-manually-controlled`, `edit-fields`
- `release-draft` — draft → analysis
- `force-move` / `override-move` — last resort; **always narrate in a comment**

**Non-user roles**
- `await-role` / `clear-awaiting-role` — mark or clear an active ticket as waiting on a role

**User (the human)**
- `user-sign-off` — UAT user_review → director_review
- `user-reopen` — back to analysis, from user_review, director_review or done
- `release-draft` — draft → analysis

---

## Reading the board

**Read with HTTP GET. Never add a comment to "have a look".** Comments notify the
assignee.

**You do not need to look the address up.** Every pane is launched with it in the
environment, so use the variable rather than a hardcoded port:

```bash
curl -s "$TICKET_BOARD_URL/api/board"            # whole board + columns
curl -s "$TICKET_BOARD_URL/api/tickets/PGU-123"  # one ticket, with comments
echo "$TICKET_BOARD_URL"                         # if you want to see it
```

`TICKET_BOARD_SOCKET` is set the same way for the write path, and the write client uses
both by default. Each project gets its own port, socket, database and service unit, all
derived from the project slug — so a pane cannot reach another project's board by accident:
it does not have the address.

---

## Writing to the board

Two equivalent paths. Both enforce the same rules.

**Shell:**

```bash
scripts/ticket-board-write --caller-role ops submit-to-audit PGU-123 --commit-hash abc1234
scripts/ticket-board-write --caller-role audit audit-sign-off PGU-123 --text "..."
```

Subcommands are the operation name with dashes: `submit-to-audit`, `audit-sign-off`,
`mark-done`, `set-blockers`.

### Reporting Switchyard issues upstream

Tenant projects can file cross-cutting Switchyard defects and feature requests to an
upstream board without receiving broader write access to that board. Use the report-only
write path:

```bash
ticket-board-write file-report \
  --title "Switchyard issue title" \
  --body "What failed, what you expected, and any reproduction steps." \
  --external-source-ref "$TICKET_BOARD_PROJECT:local-context"
```

The target board is `TICKET_BOARD_REPORT_URL`, not the tenant board's
`TICKET_BOARD_URL`, and the credential is `TICKET_BOARD_TENANT_REPORT_TOKEN`. New
projects can carry those values in their launcher config as `upstream_report_url` and
`upstream_report_token`; the pane launcher exports them for every role. If
`TICKET_BOARD_REPORT_ORIGIN_PROJECT` is set, `--origin-project` is optional and defaults
to the current tenant slug.

An upstream report lands in `analysis`, assigned to `unassigned`. Directors identify it
by the detail metadata: `Origin: <tenant>` and, when supplied,
`External Source: <tenant-local-ref>`. The report token can only create this report; it
cannot set `state`, `assignee`, blockers, signoff flags, commit fields, or mutate an
existing ticket.

**Python:**

```python
import sys; sys.path.insert(0, "scripts")
from ticket_board.write_client import TicketBoardWriteClient
c = TicketBoardWriteClient(caller_role="director")   # address comes from the environment
c.add_comment("PGU-123", text="...")
c.mark_done("PGU-123", commit_hash="abc1234")
```

### Writing comment bodies safely

Use a **quoted heredoc**. In a double-quoted `--body`, backticks execute:

```bash
scripts/ticket-board-write --caller-role director create-ticket --title "..." --body "$(cat <<'EOF'
Body text with `backticks` and $variables, safely inert.
EOF
)"
```

---

## Talking to panes: directorctl

`directorctl` types into another pane's terminal. It is the delivery mechanism behind
notifications.

```
directorctl send <tmux-target> <message...>   # send one line and submit
directorctl send-file <tmux-target> <path>    # send a file's contents
directorctl capture <tmux-target> [lines]     # read a pane, read-only
directorctl callback <message...>             # message the director
directorctl status                            # which sessions exist
```

Targets look like `<project>-ops:0.0`.
Before sending, `directorctl` exits tmux copy-mode in the target pane, so a
pane that was scrolled up still receives and submits the message.

**Rules that exist because they were learned the hard way:**

- **Prefer the board queue over a direct send.** Edit and route the ticket; the
  notification follows. A direct nudge yanks a pane mid-work and bypasses the record.
- **Never send into a busy pane.** Typing into a half-written message garbles both. Check
  pane state first; treat odd-looking input as possibly clobbered.
- **An empty prompt does not mean idle.** A working pane still shows its input line. Read
  the pane's content, don't poke it.
- **Never Ctrl-C a pane** unless you are certain it is stuck. You will destroy its context.
- **`capture` is read-only** and is the right way to see what a pane is doing.

---

## Running commands from inside a pane

**Anything you run from a pane inherits that pane's identity.** The environment names a
pane target, and installed hooks write state to the pane the *environment* says you are —
not the pane you meant.

Before launching a CLI or a probe from inside a pane, clear the identity variables:

```bash
env -u PGU_PANE_SESSION_ID -u TICKET_BOARD_PANE_SESSION_ID \
    -u PGU_TICKET_BOARD_PANE_SESSION_DIR -u PGU_TICKET_BOARD_PANE_STATE_DIR \
    -u TICKET_BOARD_PANE_SESSION_DIR -u TICKET_BOARD_PANE_STATE_DIR \
    -u TICKET_BOARD_PANE_TARGET -u PGU_PANE_TARGET -u TICKET_BOARD_CALLER_ROLE \
    <command>
```

That is the full set, and clearing a subset is the trap: leaving
`TICKET_BOARD_CALLER_ROLE` set means acting as the wrong role, which the board will
happily accept. The authoritative list is `PANE_TARGET_ENV_KEYS` in
`scripts/team_launcher.py` — check it rather than copying this block if the two disagree.

Overriding the state and session directories is **not** sufficient: installed hooks pass an
absolute `--state-dir`, so they will still write where the hook says. Clear the identity,
not the destination.

Getting this wrong writes one pane's state into another's record, and in the worst case
relaunches a role onto a session that is already in use.

---

## Notifications

Notifications are delivered by typing into the assignee's pane. Two consequences:

- **Comments on terminal tickets (done/cancelled) do not notify anyone.** Write them for
  the record, not to reach someone.
- **Writing a ticket already triggers the notification.** Do not also send a message about
  it — you will deliver it twice.

---

## Things that surprise people

**Serial focus.** An implementer holds one ticket at a time. Route a second and the board
returns it to backlog automatically; it will be picked up when the first clears, unless
it is parked, manually controlled or blocked. This is the design, not a failure.

**Blocked is not deferred.** If a ticket waits on another, set a blocker and leave it in
its owning stage. Moving it to backlog reads as abandoned.

**Deferred is not silently parked.** An *assigned* ticket sitting in backlog will be
nudged. Either unassign it, or set `manually_controlled` for a deliberate hold.

**`manually_controlled` mutes nudges and bypasses gates.** Use it only for deliberate
holds, never to force work through a gate.

**A `commit_hash` on a ticket is not proof the work is on main.** Always verify:

```bash
git merge-base --is-ancestor <commit> origin/main
```

Long-lived branches diverge. A branch cut days ago can revert everything merged since.
