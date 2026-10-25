# SYRD-644: one bounded hold for a provider out of quota

## The problem

A role whose provider account has exhausted its usage quota cannot work, but
every path kept treating it as a role that could:

- hand-overs and updates were delivered into a provider that refused each turn;
- the ticket's turn read as unresolved, so the unresolved-turn prompts, idle
  nudges, stall prompts and their Director escalations ran on their schedules;
- whatever text was on the pane was the only evidence, at any age.

## What the providers actually say

Measured, and kept as fixtures in
`tests/fixtures/syrd644_provider_quota_evidence.json`.

- **Claude Code 2.1.288.** A turn ended by an API error runs **no Stop hook**.
  It fires `StopFailure`, which the binary describes as "fires instead of Stop
  when an API error (rate limit, auth failure, etc.) ended the turn".
  - **Payload:** `{session_id, transcript_path, cwd, prompt_id,
    hook_event_name, error, last_assistant_message}`, measured offline against a
    local fake API.
  - **`error` values:** `rate_limit`, `overloaded`, `authentication_failed`,
    `billing_error`, `server_error`, `unknown`, and a few others.
  - **A subscription limit** also writes an API-error record to the transcript
    with `quotaLimits: {status: "rejected", resetsAt, rateLimitType}`. Two real
    records from this host are kept: the five-hour limit "You've hit your
    session limit · resets 10:20pm (America/Toronto)", and the weekly limit
    "... · resets Oct 3, 4am (America/Toronto)".
  - **An API key's 429** carries no rejected quota. It is a throttle, not an
    exhausted quota.
- **Codex 0.163.0.** It has no failure hook, and an errored turn runs no Stop
  hook (SYRD-594). Measured offline in its TUI:
  - **Usage limit:** `■ You’ve hit your usage limit. ... try again at Oct 25th,
    2026 12:33 AM.` The message wraps at the pane's width, and the time is the
    Codex process's own zone.
  - **No reset given:** it says "try again later.".
  - **API key:** `■ Quota exceeded. Check your plan and billing details.` This is
    OpenAI's `insufficient_quota` (SYRD-594): credit that no clock restores. It is
    classed `billing`, like Claude's `billing_error`, and begins no hold. It keeps
    the stall and escalation handling it had, which shows the Director the pane's
    line. `runtime_switch_stall_test` (SYRD-595) runs exactly this screen, and
    holding it would put a hold notice ahead of the stall escalation, which then
    waited behind the Director pane's child-work hold.
- **agy, Copilot and Hermes** have no measured quota signal. They keep the
  stall, unresolved-turn and capacity handling they had.

## Evidence, never a stale screen

Evidence counts only from the registered provider process, named by its pid
**and** its start time, as the board registered it. A reused pid, or a
replacement in the same pane, is another process. That holds even when the
new process registers before any pass has looked at its predecessor's last
answer (the SessionStart race).

- **Claude.** The installed `StopFailure` hook records facts beside the pane's
  state (`provider_error`):
  - the error class;
  - the transcript record's limit fields;
  - the provider's own error line, bounded;
  - the session, the time, and the pane process.

  It never records a prompt. Only for an exhausted quota does it also mark the
  pane `idle` (`claude.StopFailure`), a trusted idle source that is not a turn
  end: the provider is waiting, and the hold's one attempt must be able to
  reach it. Every other API error leaves the state as it was. The listener
  classifies (`provider_quota.classify_claude`). Evidence time is the hook's,
  so a later attempt's failure is always newer. The hook names the pane's
  process by pid and start time; a record naming another process, or none, is
  ignored.
- **Codex.** The usage-limit line must end the screen above the empty prompt
  (the rule SYRD-594 uses for its capacity error), while the hook says the turn
  that started it has not ended normally.
  - The hook stamps each turn start with the process that took it
    (`turn_process`: pid and start time). The listener's rewrite of a stale
    turn keeps it. A turn another process took, or one with no stamp, is never
    looked at, whatever the screen shows.
  - Its evidence time is that turn's start, so the same old line under the same
    turn is never new. A turn begun before a release, whose refusal is drawn
    after it, was refused before the reset and renews nothing.
  - A renewal also needs one more quota line on the screen than the hold last
    saw, because a turn id can move before the screen is redrawn.

## The hold

All of it is in `pgu1011`. `schema.sql` carries the same objects and
`rbac.sql` the grants.

`provider_quota_holds` has one current row per role. Each row records:
- the role's runtime, registration generation, pid and provider session;
- the evidence: source, time, turn and message;
- the provider's reset time, if it gave one;
- the deadline: the reset plus a grace (`TICKET_BOARD_QUOTA_HOLD_GRACE_SECONDS`,
  default 120 s), or with no reset a bounded recheck
  (`TICKET_BOARD_QUOTA_HOLD_RECHECK_SECONDS`, default 30 min);
- its history.

**Begins (`note_provider_quota`)** only from evidence from the registered
process (generation, pid and start time, all checked again at the board) that
is newer than the role's last ended hold.
- The Director gets one notice, with its own payload kind
  (`provider_quota_hold`), because an open stall episode admits only its own
  escalation (SYRD-595).
- The notice says until when, and how to clear it.
- A second role on the same runtime and reset window holds on its own evidence
  but joins the first notice: one account, one message.

**While held:**
- **Deliveries to the role are requeued until the deadline, not dropped.** That
  covers hand-overs, updates and anything else. Its tickets, reservations,
  context and rework are never touched.
- **Unresolved-turn prompts, stall prompts and their escalations stop.**
  `ticket_turn_is_resolved` asks `role_provider_hold_is_current` for the
  ticket's owner, and both their generators and their delivery check use it.
- **Director alerts about the role caused by its unavailability are dropped at
  delivery.** That covers unresolved-turn and idle escalations, stall
  escalations and capacity alerts, including any queued before the hold began.
  A permission prompt, a blocked pane or a long command is not covered. The
  listener may not resolve a ticket's owner itself, so it asks
  `provider_held_ticket_owner`, which names the owner only while that owner is
  held.
- **A held worker does not pull new work,** and is not idle capacity.

**At the deadline** the hold is released first. Its waiting notices become due
together, or, with none waiting, one recheck goes to the role as an update
about its work. That is the one fresh attempt.
- A released hold still counts for reminders and escalations during an
  attempt window of ten minutes at most, or until the attempt renews or ends it.
  A stall clock that ran through the hold must not fire the moment the attempt
  goes. Deliveries are not held in the window.
- **Renewed** only by evidence newer than the release. For Claude, a
  StopFailure after it; for Codex, a new turn with one more quota line.
- Evidence that arrives for a hold already past its deadline releases it at
  that deadline first, so a fast attempt's failure renews it rather than
  counting as a repeat.

**Ended at once:**
- **Reassignment** (`reassigned`): a new registration generation, or a runtime
  change from a runtime or model switch.
- **Replacement** (`replaced`): a new process, or the same pid started at
  another time.
- **New session** (`new_session`): a new provider session.
- **Recovery** (`recovered`): a normal turn end after the evidence.
- **Operator clear** (`cleared: <reason>`):
  `ticket-board-write clear-provider-hold <role> --reason`, the Director's.

When a hold ends, its waiting notices become due at once. Evidence no newer
than the role's last ended hold begins nothing, so a clear sticks while the
failure it answered is still on the pane.

**Restart:** the state is the board's, and deadlines are absolute. A restarted
listener keeps a current hold. One that comes back after a deadline releases
it, and the old record cannot bring it back.

**Status:** `GET /api/provider-holds` and `ticket-board-read holds`. Each hold
shows the role, provider, evidence time and source, the provider's own line,
the reset or recheck time, renewals, and the clear command. No credentials and
no prompts are shown.

## Not done

- **No recogniser for agy, Copilot or Hermes.** Their quota text or events are
  not measured, and adding one is a recogniser and a fixture.
- **No pre-emptive holds.** A role sharing an exhausted account is held only on
  its own evidence. The Director's single notice names the first.
- **The board stall alert** (SYRD-637) is board-wide and unchanged. Its digest
  reads each role through pull pickup's classifier as evidence only, so a held
  role appears there as idle.

## Tests

`tests/provider_quota_hold_test.py` runs on a production-built board with the
real listener, gate and hook program. Panes are processes under a process named
`tmux: server`, so a hook's record names the provider the board registered.
Timings are scaled to seconds, reset times are a few seconds ahead, and the
clock runs for real.
- **Parsing:** Claude's reset in its own words and zone, including the next day
  when needed; Codex wrapped and unwrapped lines, their zone, "later"; the API
  key's line and the capacity error, neither of which is quota; the non-quota
  Claude errors; a StopFailure record from another process is no evidence.
- **Claude with a reset:**
  - one hold and one Director notice;
  - a comment waits;
  - nothing goes to the role or about it, and the board reads its turn as
    resolved;
  - evidence older than the hold's, or from another process, changes nothing;
  - an escalation about the role queued before the hold is dropped by it;
  - at reset plus grace, one attempt fails afresh and renews the hold;
  - released with no attempt possible, the old record does not renew it, and
    the attempt window counts for reminders for ten minutes at most while
    deliveries go;
  - a normal turn end ends the hold.
- **Codex with no reset:**
  - a bounded recheck;
  - one recheck notice, an update and never a self-reminder, whose new turn's
    line renews the hold;
  - the old line under a new turn id does not renew it, nor does a line drawn
    after the release for a turn begun before it.
- **Invalidation and restart:**
  - a new registration ends a hold, and stops it counting the moment it
    registers; a runtime reassignment, a new session and an operator clear each
    end one; a clear sticks while its failure is still on the pane;
  - a replacement registered while its predecessor's quota failure is still
    there (a Claude record; a Codex turn and its quota line) is not held for
    it; a record or turn from the same pid started at another time is not
    evidence, at the listener or at the board; a hold naming another start is
    replaced, whether evidence or a pass finds it;
  - a listener restart keeps a current hold, and a failure from before a passed
    deadline does not resurrect it;
  - a deadline passes with no evidence left to look at, and the pass still
    releases the hold.
- **Shared account:** two roles held, one notice.
- **Not quota:** an overload or an auth failure holds nothing and leaves the
  pane's state, and so does another process's record.
- **Status and read client.**
- **Pull pickup** (SYRD-539's rig): a held worker that is registered, live and
  idle is neither claimed for nor named idle capacity, and is claimed for as soon
  as its hold ends.
- **Main, from a git archive:** a Director comment mid-outage is delivered into
  the refusing provider, and the turn reads unresolved. On this tree the
  comment waits, the turn reads resolved, and the Director is told once.
