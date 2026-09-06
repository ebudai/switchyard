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

To add an implementer role after a project exists, use the Switchyard source
checkout's role command instead of editing the provision files by hand:

```bash
switchyard add-role <project> <role> --cli codex
```

The command appends the launcher role, creates the role worktree, expands the
board's assignee/caller/workflow role registration, restarts the tenant board to
load the new role environment, and starts the new role's tmux session. Generated
layouts extend automatically for the new visible role. Hand-maintained or
unrecognised layouts are protected: if the requested visible slot does not
exist, the command refuses before changing the project and tells you to either
use `--detached` or pass `--relayout` to replace the existing layout with a
fresh generated layout. A newly created visible slot appears in the terminal
window after the project window is relaunched; the command does not reshape a
running Konsole window in place. For example, `switchyard add-role mefp ops
--cli codex --relayout` adds an `ops` implementer that can receive
implementation tickets and call implementer operations, regenerating the layout
only because `--relayout` gives explicit consent. Existing roles are left
running.

To move the final close after sign-off to an existing role, use:

```bash
switchyard set-vcs-close-role <project> <role>
```

The role must already exist in that project. The command inserts the generated
`VCS` stage before `Done`, changes `mark_done` to that role, updates the board
unit's operation-role environment, and restarts the tenant board. It is not
available for the built-in `pgu` board.

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
- `submit-to-audit-without-commit` — in_progress → audit, for FINISHED work that
  produced no commit; requires `--reason`, which is recorded as an attributed
  comment so audit sees the claim. Use this rather than borrowing a hash from
  unrelated work
- `request-commit-exempt` — in_progress → analysis, to hand the ticket back for
  a decision about whether it needs a commit at all. Note it UNASSIGNS the
  ticket: it is for "this needs rethinking", not for "this is done and there is
  no diff"
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
- `await-role` / `clear-awaiting-role` — mark or clear an active ticket as waiting on a
  role. Use it when the next action is genuinely someone else's and there is no ticket to
  point `blocked_by` at (that field only accepts ticket IDs). It does **two** things:
  the ticket's nudges are suppressed for four hours, and — for `director` only — the
  ticket appears in `ticket-board-read director`. Both matter: before PGU-906 only the
  suppression worked, so escalating made a ticket silent *and* invisible. Note the
  asymmetry, because it is a real limit and not an oversight in your reading: the
  per-role queues (`ticket-board-read queue <role>`) filter on assignee alone, so
  `await-role` at any role other than `director` still only mutes nudges.
  It does not move or reassign the ticket: the ticket stays in its own stage with its own
  assignee, which is what you want, because the work is still theirs.
  Do not `await-role` your own role: the normal write path rejects it, and a direct
  same-actor marker would be self-defeating because the same ticket-activity rule clears it.
  The marker clears when the awaited role ACTS -- a state change or a reassignment -- and
  not when they comment. Replying "seen, still blocked" leaves the escalation standing,
  which is the point: before PGU-909 that acknowledgement cleared it and the ticket
  dropped out of the queue while still blocked. If your reply IS the thing that was
  awaited, clear it yourself with `clear-awaiting-role`.

**User (the human)**
- `user-sign-off` — UAT user_review → director_review
- `user-reopen` — back to analysis, from user_review, director_review or done
- `release-draft` — draft → analysis

---

## The board skill

Every pane also has a `switchyard-board` skill installed in whichever CLI it runs,
covering the same tool usage and safety rules in the compact form an agent loads
while working. It is a projection of one source, so it says the same thing on all
four runtimes. Load it when you are about to touch the board; read this guide when
you need the mechanics behind it. If a session reports it cannot find the skill,
`switchyard board-skill verify` says which runtime is missing it and
`switchyard board-skill install` restores it.

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
`TICKET_BOARD_URL`. The credential stays in a 0600 EnvironmentFile-style file, and panes
carry only its path through `TICKET_BOARD_TENANT_REPORT_TOKEN_FILE`. New projects can
carry those values in their launcher config as `upstream_report_url` and
`upstream_report_token_file`; the pane launcher exports them for every role without
putting the token value on the process command line. If `TICKET_BOARD_REPORT_ORIGIN_PROJECT`
is set, `--origin-project` is optional and defaults to the current tenant slug.

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

**`manually_controlled` mutes nudges and bypasses transition gates.** Use it only
for deliberate holds, never to force work through a gate. State changes still
normalize the assignee to the target stage owner, so a held ticket can submit to
audit without leaving an implementation-stage assignee in the audit stage.

**A `commit_hash` on a ticket is not proof the work is on main.** Always verify:

```bash
git merge-base --is-ancestor <commit> origin/main
```

Long-lived branches diverge. A branch cut days ago can revert everything merged since.
