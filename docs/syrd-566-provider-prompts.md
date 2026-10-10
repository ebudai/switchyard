# SYRD-566: a pane stopped at its provider's own question is not a worker ready for work

## What otto saw (release 2951c5c9, report item 13)

- **No hook until the first prompt.** Codex fires no hook until its first
  prompt. Until then, the only state a fresh Codex pane has is the launcher's
  own seed (`team_launcher.start`, idle), written before the CLI has drawn
  anything.
- **A startup modal.** On a retiring model, Codex stops an unattended start at
  "Meet GPT-6 Sol … 1. Try new model / 2. Use existing model".
- **The `/new` menu.** `/new` asks "Where should the new conversation run?
  1. Use current Git worktree / 2. Create new Git worktree". The second option
  moves the role out of its managed checkout.

## Measured: Codex 0.162.0 and Claude Code 2.1.294, offline

Every screen below was recorded the same way:
- a throwaway `HOME`/`CODEX_HOME`, inside `unshare --user --map-root-user
  --net`, so there was no network at all;
- a private tmux server;
- Codex logged in with a placeholder key; Claude with a placeholder
  `ANTHROPIC_API_KEY` and onboarding pre-recorded.

The recordings are kept in `tests/fixtures/syrd566_provider_screens.json`.

- **Model retirement.** A Codex configured with `gpt-5.5` shows the notice.
  Codex's bundled catalog upgrades `gpt-5.5` to `gpt-6-sol`, with
  `migration_markdown` "Meet GPT-6 Sol". The ordinary prompt is drawn at
  0.5 s and the notice replaces it at 4.0 s. That timing was measured three
  times with identical results.
- **`/new`.** It always asks the location question, with Codex's worktrees
  feature on or off.
- **`/clear`.** It clears the terminal, starts a new chat in the same checkout
  and asks nothing.
- **Folder trust.** Codex asks "Trust this folder? › 1. Trust and continue /
  2. Back to Agent Command Center". Claude asks "❯ No, exit / Yes, I trust this
  folder" with only the highlighted row marked. Both sit above a key-hint
  footer.
- **Sign-in.** Codex's menu spaces its numbered options out with descriptions
  between them.

**What main does with them.** Main's activity gate, given a fresh pane with
only the launcher seed, behaves as follows:
- the retirement notice and Codex's trust question read idle, so a ticket
  notice is typed into them, and its trailing Enter answers with the
  highlighted option;
- the sign-in menu, the `/new` menu and Claude's trust question hold only
  because the cursor looks like somebody typing (`human_composing`). That is
  work evidence, so a reminder there is dropped as stale, and nobody is told
  why the pane is stuck.

## The classifier

`scripts/ticket_board/provider_prompt.py` recognises a question from three
things:
- a key-hint footer among the last three rows that tells you to press Enter
  ("enter/esc confirm", "enter select · esc back", "Press enter to continue",
  "Enter to confirm · Esc to cancel");
- above it, an option under the selection cursor;
- at least one more option in the same label column. Options may continue
  across a gap when the next one is numbered, as on Codex's sign-in menu.

The question is named from its own wording: `model_retirement`,
`folder_trust`, `sign_in` or `new_conversation_location`. Anything else that
matches is a generic `menu`.

These read as no question:
- Codex's ready prompt, which has "? for shortcuts" and no Enter hint;
- Claude's ruled box;
- every SYRD-550 Claude composer capture;
- a numbered list in a transcript;
- a working turn's "esc to interrupt";
- options with no cursor, or a footer with no options.

## What changes

1. **The gate holds a question.** `PaneActivityGate` reads the question off
   the capture it already takes for the composer, so it makes no extra tmux
   call. A pane at a question is busy as `provider_prompt:<kind>`, whatever
   its hook says and before any hook. This is not work evidence, so reminders
   wait instead of being dropped.
2. **A just-seeded pane is still starting.** For its first 10 s after the
   launcher's seed, a pane is busy as `provider_starting`. That is more than
   twice the measured 4.0 s before Codex draws its notice over its first
   prompt.
3. **The Director hears what it is stopped at.** SYRD-557's hold episodes
   carry a provider-prompt hold. `note_background_hold` gains a fourth
   argument, the description. Migration pgu983 drops the three-argument
   version, so a call cannot be ambiguous. After `TICKET_BOARD_PROVIDER_PROMPT_ALERT_SECONDS`
   (default 120 s; a question never ends by itself), one alert:
   - names the question;
   - says nothing was typed into it;
   - says what releases it.

   An older board with only SYRD-557's function never gets a prompt hold
   described as background work.
4. **A blocked worker is not a started worker.** `worker_screen` watches a
   freshly started pane until it shows a question, settles for 8 s without
   one, or 20 s pass. A blank screen has not drawn yet and never counts as
   settled. Then:
   - `switchyard worker-pool start` reports `failed to start`, not
     `started`, and exits 1, naming the question, its options and what answers
     it;
   - a second start says `not started`, with the same detail, and starts
     nothing more;
   - `status` and readiness show `running, blocked: …`, and
     `can_take_work` is false. It is not a readiness blocker, because a
     restart reads readiness before its stop, and the restart is the remedy;
   - `set-role-runtime` counts a new worker stopped at a question as a
     `worker_problem`, so the switch is not reported done (SYRD-560).
5. **Fresh context stays `/clear`.** Switchyard already sends `/clear` to
   every runtime (SYRD-135). The test pins that `/new` is never sent, and
   re-measures the real Codex: `/clear` asks nothing, and the pane's working
   directory is still its checkout.

Nothing answers any question. Each remedy is written for a person, and names
the supported step where there is one: `ticket-board-workflow prepare-role`
for trust, first-run setup for sign-in, the role's model for a retirement, and
Esc or "Use current Git worktree" for `/new`.

## Neighbour suites

Four fakes answered every unrecognised tmux command as a pane start:
- `codex_folder_trust_test`;
- `worker_pool_start_status_test`;
- `worker_pool_restart_status_test`;
- `worker_pool_lifecycle_test`.

So the new `capture-pane` read counted as a second start. Each now answers it
as a host with no screen to read (exit 1), which the screen check treats as
"cannot tell". Their check counts are unchanged from main: 103, 32, 55 and 126.

`pane_activity_gate_boundary_test` pins the gate's imports, seams and
defaults. Three entries changed:
- the composer read's call-time import now also names `provider_prompt`;
- `_untrusted_idle_source_trace` reads `listener.ActivityTrace` once more, for
  `provider_starting`;
- the new `_open_prompt_trace` has no defaults.

Its pinned top-level names are unchanged: the 10 s window is a class attribute,
`PaneActivityGate.LAUNCHER_SEED_SETTLE_SECONDS`, not a module constant. It
runs 289 checks, as on main.

`background_hold_alert_test` (SYRD-557) checks grants on the four-argument
function.

## Tests

`tests/provider_prompt_test.py` runs 57 checks, with before-runs of main
774d365 from a git archive:

- **Classifier.** Every recorded question is named with its options as shown,
  and every ordinary prompt is not a question. So are all six SYRD-550 Claude
  captures, and these screens:
  - a transcript's numbered list;
  - a working turn;
  - a footer with no options;
  - options with no cursor;
  - a composer with a send hint;
  - "Enter" in prose;
  - an answered question still on screen above later output.
- **Gate, against main.**
  - On main, the retirement notice and Codex's trust question read idle, both
    fresh and after a turn; the others read as somebody typing.
  - Now every question holds, as `provider_prompt:<kind>`, under the
    launcher's seed, after a turn, under a busy hook's anti-clobber check, and
    under a stale Codex busy hook.
  - A pane that then cannot be read is unreadable, not still at the old
    question.
  - A pane seeded 2 s ago is `provider_starting`; main called it idle.
  - Ready prompts are exactly as deliverable as on main.
- **Production board and real listener.** On main, PGU-1 is typed into the
  retirement notice. Now it is held and never typed:
  - nobody is told at one minute;
  - after two minutes, one alert names the notice, says nothing was typed, and
    says what answers it;
  - once answered, the notice goes out once and the episode clears with a
    "hold ended" line.

  Also, the `/new` menu after an ended turn is held, and the Director is told
  to keep the checkout and that Switchyard uses `/clear`.
- **`switchyard worker-pool`.**
  - `start` on a worker at the notice or the `/new` menu reports
    `failed to start` and exits 1, and so does a notice drawn over the ready
    prompt after the start
    was first observed.
  - `status` says the same, and a second start starts nothing.
  - A ready or unreadable screen starts as before.
  - `can_take_work` is false, while `ready` is unchanged.
  - The watch also catches a question from a CLI that stays blank past the
    settle time (injected clock).
- **`set-role-runtime`.** `worker_problem` reports the notice for a new worker
  stopped at it.
- **Fresh context.** `/clear` is used for every runtime, and nothing sends
  `/new`.
- **Wording and thresholds.** Every kind has a description and a remedy. The
  threshold is two minutes unless configured. An older board is never told a
  question is background work.
- **The installed Codex, now.** Three observations, contained (SYRD-524) and
  offline:
  - `gpt-5.5` stops at the notice, and the real start watch names it;
  - `/new` asks the location question;
  - `/clear` asks nothing, ends at Codex's prompt, and tmux's
    `pane_current_path` is still the checkout.

## Comparison with main

510 suites ran under `env -i` with refusing model and konsole stubs: the 206
related suites from SYRD-565 and SYRD-557, plus every suite that imports what
this changes.
- **Pass on both:** 410.
- **Red on both:** every red on this tree, apart from those below, is red on
  main too. Their messages differ only in the newest migration's name
  (park_blocked, publication_migration_recovery) or the worktree path.
- **Neighbours:** the five described above, now green, with main's check
  counts.
- **Real-home suites:** `team_launcher_upgrade_cutover_test`,
  `team_launcher_viewer_test`, `team_launcher_presentation_titles_test` and
  `team_launcher_codex_rendered_scrollback_test` read provider-state files in
  the real home's pane-session directory. Run serially, they give identical
  results on both trees. One differed in the parallel sweep only because both
  trees wrote that directory at once. That they write the real home at all is
  older than this change.

## Mutation

28 mutants, run with `tests/bounded_run.py mutate`:
- **Covered:** the classifier, the gate's four entry points, the hold noting
  and wording, pgu983, worker readiness, start and status, the start watch,
  `worker_problem`, and the fresh-context command.
- **Result:** all 28 are killed, each by a named behavioural check.
- **Survivors that became cases:** five survived their first run. Each marked
  a rule the test did not yet pin:
  - the key-hint requirement;
  - where the footer may be;
  - a single option;
  - a stale question;
  - a slow first draw.

  Each got a case, and was then killed.

The whole plan was then rerun on the final code, after rebasing onto SYRD-573:
28 of 28 killed.

## Otto

When this is deployed to Otto, tell Otto's Director, so any local workaround
for these startup and context hazards is removed rather than left to drift.
