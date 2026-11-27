# SYRD-891: an agy role whose tool calls are all being denied is told to the Director once

## What happened

On ocstab (2026-11-27), a Researcher on agy 1.5.17 had every `read_file` and `run_command` refused with "tool call
denied by pre-tool hook: " and an empty reason, for two hours on OCSTAB-4. No alert reached the Director or the
operator. The role's turns ended and its hooks said idle, and nothing that watches a pane looks at what a tool call
came back with. The only clue was the role's own complaint, which the User relayed by hand.

SYRD-890 found that incident's cause: agy 1.5.17 reads a pre-tool answer with no decision as a deny. The reporter's
first suspect, an expired login, was not it: tools stayed denied after the re-login. The Director reframed this
ticket as detection whatever the cause, whether agy's own permission layer, a hook, or the login.

## What agy shows, measured

Measured on the installed agy 1.5.17 with SYRD-890's offline probe, plus logging hooks on every event:

- **No PostToolUse hook runs for a denied call.** After a denial only PreInvocation, PostInvocation and Stop run, so
  no tool hook can see a refusal.
- **agy's transcript records every outcome.** It is `.../brain/<conversation>/.system_generated/logs/
  transcript_full.jsonl`; every hook payload names it as `transcriptPath`, and the pane's SessionStart record
  already keeps that payload.
  - A call is a `PLANNER_RESPONSE` step with `tool_calls`.
  - Then there is one step per call: `status: DONE` when it ran, `status: ERROR` with agy's `error` when it did not.
- **agy's words for a refusal all say "denied"**, from its binary:
  - "tool call denied by pre-tool hook: %s";
  - "tool call denied with reason: %s";
  - "User denied permission to run command:";
  - "Action denied: ...";
  - auto-denied commands;
  - "denied-repeat pre-check blocked".

`tests/fixtures/syrd891_transcripts` holds three of these transcripts as agy wrote them: three calls denied with no
reason, the same three run, and one refused with the guard's reason.

## What it does

`scripts/ticket_board/tool_denials.py` runs on every listener pass, beside the stall and capacity passes. A board
whose declared workflow runs no role on agy is asked nothing. For each role the board has registered on agy
(`role_runtime_assignments`):

- **The transcript.** The role's own session record (`<session_dir>/<target>.json`) names the transcript. Its last
  MiB is read, and only a file named `transcript_full.jsonl` or `transcript.jsonl`. An unchanged transcript is not
  read again: the answer is cached against both files' mtime and size.
- **The run.** A trailing run of three or more refused calls (`THRESHOLD`), with none run since, is reported. A call
  that runs ends the run. So does an error that is not a refusal, such as a validation error.
- **The likely cause:**
  - agy's newest log saying the login lacked an OAuth scope: re-login (the 403 `fetch_config` line, read from the log
    directory of the transcript's own home);
  - a hook denial with no reason: SYRD-890's fix, then a restart;
  - a hook's reason: pointed at;
  - agy's permission layer: `--dangerously-skip-permissions`;
  - otherwise agy's words, quoted as they are.
- **The board.** `note_tool_denials` (pgu1063, listener-only) keeps one row per role and run in
  `tool_denial_alerts`; a run is the conversation and the step it began at.
  - It queues one notice to the Director, on the role's current ticket. The notice names the role, how many calls,
    since when, agy's last refusal word for word, and the causes.
  - A role with no current ticket is not recorded, so it is told about once it has one.
  - The Director's own pane is never told about, because the notice would wait there too.
  - The notice carries the ticket's state and assignee, so it is dropped if the ticket moves before delivery.
  - `tool_denials` joins the Director-only notice kinds in delivery's eligibility check.

Nothing asks agy or the provider anything. Nothing changes a file outside the board's own row.

## Not done

- **agy's denial text itself** (ask 2) can't be changed. When agy denies on its own, our guard is not the one
  answering. The Director's notice carries the suspected cause instead.
- **A login-only alert at pane start**, from the 403 line alone, was not added. The Director reframed the detection
  around denied calls, and the login line now names the cause inside that notice.
- **SYRD-890** keeps the incident's cause and fix (Done).

## Pinned by

`tests/tool_denials_test.py` (46 checks):
- streaks, on agy's own transcripts and on written ones;
- causes, including agy's 403 line from the newest log only;
- the role's record and the cache;
- a real board, fresh and upgraded through pgu1063, with main and ops on agy and the real listener:
  - one notice, on main's ticket, delivered to the Director's pane;
  - none on the next pass;
  - one for a run in a new conversation;
  - ops told once it has a ticket;
  - a notice whose ticket moved before delivery, dropped;
  - nothing for calls that ran;
  - never the Director's own pane;
  - listener-only;
- a workflow with no agy role is asked nothing;
- schema.sql's function is pgu1063's;
- the listener pass runs it.
