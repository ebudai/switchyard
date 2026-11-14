# SYRD-672: a provider's safety refusal holds its whole account

## The problem

Repeated cybersecurity or safety refusals can get a provider account suspended. Switchyard treated a refusal like any
other failed turn. It redelivered, reminded, nudged, and kept routing work to other roles that share the same
account. One refusal could become an account-wide outage. In the October incident, the refusal came in the Director's
own pane, and every later attempt to explain the defensive context was refused too.

The rule: **never argue with a provider refusal from the same account.** The first refusal is a circuit breaker.

This is slice 1. The fixed-schema recovery packet (SYRD-737), full Director replacement (SYRD-738) and the
one-operation break-glass role (SYRD-739) build on the hold and notice interfaces below.

## What the providers say

Read from the installed binaries on 2026-11-07, read-only. No provider was ever sent a refusal-provoking request, and
the tests use stand-ins only.

- **Claude Code 2.1.318:**
  - A refused turn's transcript entry is an assistant message with `stop_reason: "refusal"` and
    `stop_details.category`.
  - Claude Code has a *refusal fallback* that re-sends the request to another model, on the same account.
    `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK` turns it off.
- **Codex 0.166.0:** two lines of its own.
  - `This request has been flagged for possible cybersecurity risk.`
  - `Your account was flagged for potentially high-risk cyber activity …`, when it routes the request to a fallback
    model.

  An errored Codex turn runs no Stop hook (SYRD-594).
- **Hermes, agy, Copilot:** no format the board can read. They are held by hand.

## Evidence

- **Claude panes** start with `CLAUDE_CODE_DISABLE_REFUSAL_FALLBACK=1` (`role_command.py`), so a refused request
  is never re-sent by the CLI itself.
- **The pane hook** (`ticket-board-pane-idle-hook`) runs at Stop and StopFailure. It reads the turn's last
  transcript entry and, when that entry is a refusal, records `last_refusal`:
  - what it records: `{evidence: <entry uuid>, category, source, observed_at, session_id, pane_pid, pane_start_time}`;
  - what it never records: the prompt, the response or the pane's text. A refusal also blanks the StopFailure
    record's message.

  The record is carried across later writes, so a listener that was down still sees it.
- **The listener** (`provider_refusal.run`, called first in `quota_hold.run` each pass) takes evidence only from a
  role's registered process, matched by pid and start time:
  - Claude: `last_refusal`, keyed `claude:<uuid>`;
  - Codex: one of its lines as Codex's own event block (`■ ` or `⚠ ` at the margin), directly above the prompt at
    the cursor with nothing after it, while the hook says the turn that process started has not ended
    (`CODEX_UNENDED`). A later turn's echo or reply below it, or the words quoted in a prompt or a reply, is not one.

    **It must be first seen after that turn began.** `note_refusal_sighting` keeps, per role, what the registered
    process's screen ends on (a signature of the drawing, or none) and since when. The record lives in the database,
    so a listener restart neither forgets an old line nor ages a new one. The time is kept only while the same process
    shows the same drawing.

    The identity is `codex:<pid>:<start>:<turn start>`, the process and the turn. What is on screen can't be the
    identity, because a respawned pane (`respawn-pane -k` clears its history) refusing the same prompt in the same
    words draws exactly what it drew before (Audit on `9c59afc2`).
- **`note_provider_refusal`** re-checks the registration, then takes the identity: it begins the hold, or joins the
  one that stands. **Every identity is taken once, ever** (`provider_refusal_evidence`), including one first seen
  while a hold already stood. So:
  - an old refusal line still showing under a new, harmless turn was seen before that turn began, and holds nothing;
  - a refusal drawn afresh in a new turn holds again, and so does one after a respawn, however it is worded;
  - a second refusal seen during a hold is consumed by it and holds nothing after the clear.

Quota, overload, authentication, billing and Codex's capacity error are not refusals. They keep SYRD-644's and
SYRD-594's handling.

## The hold (pgu1030)

`provider_account_holds` has one current row per runtime. A row holds every role whose runtime is that CLI:
- `role_account_runtime`: the live registration's runtime, else the workflow's declared runtime, so a stopped role
  is held too;
- `role_account_hold(role)`: the current hold, or NULL.

**Why per CLI.** Every role runs as the project's owner account, with the owner's sign-in copied into role homes
(SYRD-69, `role_credentials`). That makes this board's account "the owner, on that CLI".

**Hermes caveat.** A Hermes role's account is really its configured API provider. Holding every Hermes role
over-holds, which costs time and no work.

There is no expiry, and nothing asks the provider whether the hold can end. A timeout, a restart, a new registration,
a quota reset, or another account's success leaves the hold in place.

**What the hold gates:**

| Path | Gate |
|---|---|
| Every delivery: hand-overs, updates, reminders, nudges, stall prompts, alerts | `quota_hold.hold_delivery` asks `provider_refusal.hold_delivery` first, at claim and before the send. A notice to a held role, the Director's included, is requeued every 5 minutes, never dropped. Fails closed. |
| Unresolved-turn prompts, escalations, stall notices | `role_provider_hold_is_current` (now true for a held account), via `ticket_turn_is_resolved` |
| Director alerts about a held role | dropped at delivery, as SYRD-644 drops them, through `role_provider_hold_until` (now + 5 min for a held account) |
| Context restore and its notes | `session_context`: no attempt is opened, nothing is typed, and nothing parks. It restores once the hold is cleared. |
| Director capacity retry (SYRD-728) | `director_capacity.run`: no attempt is claimed or typed |
| Pull pickup | `pull_pickup._held`: every role on a held account |
| A worker's own claim | `cohort_claim_refusal` refuses first, whether or not the role has a cohort |
| Router | `router_scope_refusal` refuses a held role, so `ticket_routing.assignable` leaves it out and Router carries on with the eligible owners |
| Worker admission | `/api/provider-holds` `accounts.held[].roles` join the held set |

`typing_held` and `hold_delivery` fail closed: when the hold cannot be checked, the board holds.

## Notice

Each hold is told once. The record of who was told is in the database, so a listener restart never repeats a notice.
- **The Director:** an escalation on the refused role's ticket (`provider_hold_anchor`), whose payload kind is
  `provider_safety_hold`. It is current only while its hold stands. It names the account, the roles it holds, and
  the choices: inspect by hand, move the work, or clear the hold once the operator confirms the account is safe. It
  is not sent when the Director's own runtime is the one held.
- **The operator:** always one `user_events` row of kind `provider_safety_hold` (board-wide, its detail the runtime),
  for the phone bridge. It stands until the hold is cleared.
- **The refused role:** never told.

## Clear, and holding by hand

Both need a reason, and the database checks who may act (`provider_account_hold_actor`). The server admits the same
callers:
- the Director, or any role holding `set_manually_controlled`;
- the User, the workflow's `user`-kind role. No capability marks the User alone, so it is admitted by its kind. This
  matters when the Director's own account is held.

Commands:
- `ticket-board-write clear-provider-account-hold <cli> --reason …` records `cleared_at`, `cleared_by` and
  `clear_reason`, then makes the waiting notices due now. Each is still checked again before it goes.
- `ticket-board-write hold-provider-account <cli> --reason …` holds an account on a suspicion (`kind:
  'suspected'`). Repeating it changes nothing.

`ticket-board-read holds` and `GET /api/provider-holds` (`accounts`) show the current holds, with the roles each one
holds, and the last five cleared.

**The recovery packet (SYRD-737):** `GET /api/recovery-packet[?hold=N]` and `ticket-board-read recovery-packet`, a
fixed-schema, bounded packet the board builds from its own records, with no model turn. See
`docs/syrd-737-recovery-packet.md`.

**Replacing a held Director (SYRD-738):** `pkexec switchyard rebind-director`, an operator's command that moves the
Director to another runtime without a turn from the held one, and keeps work paused when none can take it. See
`docs/syrd-738-director-rebind.md`.

**A one-operation break-glass role (SYRD-739):** `pkexec switchyard break-glass`, a temporary role on another provider whose one board operation the board enforces by token, deadline and count. See `docs/syrd-739-break-glass.md`.

**For the children (SYRD-737 to 739):** `provider_account_hold_status()`, `role_account_hold(role)`,
`role_account_runtime(role)`, the `provider_safety_hold` user event, and the notice payload
`{kind, reason, id, runtime, hold, role}`.

## Tests

`tests/provider_account_hold_test.py` runs on a production-built board, with the real listener, gate and hook
(SYRD-644's rig), and only stand-in refusals:
- one hold and one notice across restarts;
- no further send to any role on the account;
- Router and claim exclusion, and the ticket moved to another account;
- the audited clear, with resume only after it;
- a stale refusal holds nothing, a live one holds again;
- the Director's own account held, cleared by the User;
- other failures make no account hold;
- holding by hand;
- the hook keeps the identity and never the words;
- the checks fail closed;
- the Claude pane environment.
