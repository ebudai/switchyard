# SYRD-788: an ephemeral role's cleared session hears its role before its ticket

Follow-up to SYRD-770. With Router set to `ephemeral: true` (workflow revision 125, 2026-11-15), the board cleared
Router's session for SYRD-785 and delivered one line: "SYRD-785 -- Aborting switchyard new takes many Ctrl-C presses
… entered Implementation". The session had no Router prompt, and never loaded the `switchyard-router` skill.
Haiku read the line as an assignment and implemented the ticket: 99 tool calls in 11 minutes. The Director stopped it.

## Why the cleared session heard nothing

The pane hook (`ticket-board-pane-idle-hook`) gives a role its onboarding prompt at SessionStart, but only on a fresh
start, and its test had two conditions:
- **The source had to be `startup` (or none).** A session cleared by `/clear` starts with `source: "clear"`
  (PGU-816), which was not counted.
- **The launch must have resumed nothing.** `resume_session_id` comes from the launch's environment
  (`TICKET_BOARD_PANE_SESSION_ID`), so a pane launched resuming a session never counted as fresh. Router had been
  launched persistent, resuming its conversation.

A cleared session got only active-work context, and only for an in-progress ticket of its own. Router's tickets are
in triage, so it got nothing.

## What changed

1. **The hook:** a cleared session gets its role's onboarding (the stored prompt, or the packaged remit), opening
   "Your <role> session was cleared for its next ticket.", whatever the launch resumed. Any active work follows,
   as a cleared pane was told before. Other starts are unchanged: a fresh start is onboarded, while a resumed launch
   or a resumed or compacted session keeps what it had. The hook is the same length (1299 lines).
2. **The listener:** the first notice delivered after a clear, to Router or the Librarian, starts with one line:
   "You are the <role> role, and your session was just cleared for this ticket. Before anything else, load the
   switchyard-<role> skill and follow it, and your role's onboarding: act on this ticket only as <role>."
   (`notification_session_clear.cleared_preface`.) These are the roles whose job is not to build what a notice
   names. This line is what agy and Hermes are told, since they run no SessionStart at a clear. An implementer's
   notice is its assignment, so it still arrives verbatim (SYRD-106). Only the first notice after the clear carries
   the line.

## Measured: what a real Claude Code sends after `/clear`

Offline, with no account or API: Claude Code 2.1.318 (`--model claude-haiku-5-5`) ran against a stand-in API that
logs every request body (`tests/fixtures/syrd788_probe`). The tree's real pane hook answered SessionStart for a
Router pane launched resuming a session (`TICKET_BOARD_PANE_SESSION_ID` set). The probe sent a prompt, `/clear`,
then a ticket line:
- **Base (f5c21661):** the ticket line's request after `/clear` carries no Router prompt. This reproduces the incident.
- **Candidate:** that request carries "SessionStart hook additional context: Your router session was cleared for its
  next ticket. <the Router prompt>". Claude Code puts SessionStart hook context into the next request, as a
  system-role message beside the ticket line.

Whether Haiku then routes is the acceptance step: the Director sets Router back to ephemeral and reads the next
routing's transcript for one write.

## Every ephemeral role

- **Router** gets both layers.
- **The Librarian** (ephemeral since SYRD-624; its prompt is `TICKET_BOARD_ROLE_ONBOARDING_PROMPT`) gets both layers.
- **Ephemeral implementers** (pulled work) get their onboarding from the hook on Claude and Codex. Their notice stays
  verbatim.
- **Runtimes:**
  - Copilot reports a clear as a `new` session start;
  - Hermes runs `on_session_start` with no source;
  - agy runs no SessionStart at `/clear` (PGU-816).

  On those, the preface line is what a cleared Router or Librarian is told.

## A role acting outside its capabilities

This is not cheap here. The SYRD-753 shell guard is one standard-library hook, at its 1250-line limit, installed for
all five runtimes. Every role of a tenant shares one account, so a Router-only policy would need:
- a role-scoped hook for each runtime, staged into each tenant;
- an allowlist that must not refuse Router's own reads and board writes (`curl /api/workflow`, `ticket-board-write
  router-assign`, a `--reason-file`).

The board already limits what Router can do to the board: its one capability is `router_assign` (and
`router_return`), enforced in the database. What escaped was work in its own worktree and session. The two layers
above remove the cause.

## Tests

- `tests/cleared_role_onboarding_test.py`:
  - a real `TicketBoardNotifyListener` (SYRD-135's rig, with Router's runtime row) sends the clear, then the preface,
    then the ticket's line, with no preface on the next notice; an ephemeral implementer is cleared and handed its
    ticket verbatim;
  - the real hook gives a cleared session of a resumed launch its onboarding, and its onboarding then its active
    work; other starts are as before;
  - the real Claude Code probe above. On every exit, a timeout included, it stops its private tmux server and
    removes its socket directory, and the test checks a normal and a cut-off run leave nothing behind (SYRD-800). It says so when it is skipped, where Claude Code, tmux or an unprivileged user
    namespace is missing.
- 11 mutants, all killed, including the clear not counted as fresh, the resumed launch still blocking it, the
  preface dropped, after the notice, on every notice, or given to an implementer.
