# SYRD-842: an optional research stage — a question, an evidence-reviewed finding, and a hand-back to the decider

> **Historical design and rollout evidence.** This ticket record describes the
> revision that introduced the research stage; it is not the current operating
> contract. Use the [Director guide](onboarding/switchyard-director-guide.md#research-spikes-the-optional-research-stage),
> the [Researcher guide](onboarding/switchyard-researcher-guide.md), and
> `switchyard research-stage --help` before acting.

## The ask

Some projects need research spikes before they can plan or build. The Director
ran them by hand, outside any stage: nothing marked the spike as research,
nothing reviewed its evidence, and nothing handed the finding back to whoever
had to act on it. The User asked for an optional stage, modelled on the
documentation gate (SYRD-624): an opt-in composer, with existing tenants
unchanged until they enable it.

## The shape (`scripts/ticket_board/research_stage.py`)

`with_research_stage(document, role=<existing role> | researcher={runtime,
template_role, slot}, directory="docs/research")` adds:

- **`needs_research`**, a gate flag, off by default.
- **`research`**, a `system` stage owned by the research role, ungated, placed
  just before Implementation. The Director's `start_research` moves a research
  ticket there from triage.
- **The researcher's three moves**, all owner-scoped:
  - `submit_finding`, which carries a commit;
  - `submit_finding_without_commit`, which carries a reason;
  - `research_hand_back` to triage, which carries a reason.
- **`research_review`**, when the project runs Audit (exactly one review stage
  gated by `needs_audit` with a sign-off). It is a `review` stage owned by
  Audit's owners, gated by the ticket's own `needs_audit`, and skipping to
  triage. It holds **no sign-off flag**. Audit's `research_sign_off` is a move
  to triage, and `research_kick_back` returns the ticket to Research with a
  reason. Without Audit, both submissions go straight to triage.
- **The Director's two moves from triage**, each with a reason:
  - `return_finding` to Research;
  - `close_on_finding` to the terminal stage that finished work goes to. That
    is the one terminal stage the Director cannot already move triage work
    into, so the shipped example's `done`.
- **Implementation's Director-only defer and cancel**, cloned out of both new
  stages.
- **`research` added to `user_waits`**, so `await-user` works in Research when
  the project has an active User.
- **A top-level `research: {"directory": ...}` policy**, which
  `workflow_config.validate` now admits.

A new **Researcher** has these properties:
- `name: researcher`;
- kind `support`, which carries no board semantics of its own;
- capabilities `add_comment`, `await_role` and `clear_awaiting_role`;
- detached, `ephemeral` and never `serial`;
- `RESEARCHER_PROMPT` as its onboarding prompt;
- its template role's model, and the window's next free display slot.

With `--role`, the existing role becomes the stage's owner and nothing about
it changes.

`without_research_stage` does the following:
- removes everything above;
- names both stages in `remove_stages`;
- leaves a Researcher declared but inactive.

The board refuses it while either stage holds a ticket.

`validate_research_stage` runs on every document from `workflow_config.validate`
and holds:
- the Researcher's authority;
- the stage and review shapes;
- the policy;
- the gate's default;
- every composed move, exactly; nothing else enters or leaves either stage;
- the User wait.

### Decisions the composition settled

- **Route, not gate.** Making Research the ordinary route's gated first stop
  (skip to Implementation when the flag is off) would have changed one existing
  behaviour. The executor exempts a move *into Implementation* from the
  blocker check, so a blocked ticket can still be queued for its implementer.
  A route whose declared destination became Research would have lost that
  exemption on every enabled board. So Research has its own Director move,
  `start_research`, and the database refuses the ordinary route for a ticket
  carrying the flag.
- **Router.** Research has exactly one owner, so there is no owner for Router to
  choose. A research ticket goes to the research role directly, whether or not
  the project runs Router.
- **No new sign-off flag.** I first gave the review its own `research_signoff`.
  It broke the documentation gate and the Publisher: each holds "clears every
  later sign-off" against the document's whole sign-off list, so composing
  research after either made the document invalid. Reusing `audit_signoff`
  instead made two stages that "Audit signs off", which the Publisher refuses.
  It also made disabling the stage remove an outstanding review obligation
  from every live ticket that still needed Audit. The review therefore holds
  none, like a Director-owned documentation gate. All 24 orders of research,
  documentation gate, Router and Publisher compose, and each turns off and on
  again in each order.
- **No-code finding.** `allow_no_code` is admitted only out of Implementation, by
  both validators. A plain move with a required reason needs no validator
  change. The database clears an earlier round's findings commit when a
  no-commit finding leaves Research.

## The database (`pgu1051_syrd842_research_briefs.sql`)

These rules apply only on a board whose declared workflow has the `research`
stage and the `needs_research` gate (`research_declared()`).

- **`ticket_research_briefs`.** One row per ticket, holding the question, the
  timebox (15 minutes to 30 days) and what a usable answer looks like. All
  three are required. It also keeps the round's start, its timebox notice and
  its finding. The brief is written by `set_research_brief`:
  - it is the Director's alone (`director_edit`);
  - it turns `needs_research` on;
  - each write is a Director comment.
- **`require_research_brief`.** A `BEFORE UPDATE` trigger that refuses, by any
  path:
  - Research without the flag;
  - Research without a brief;
  - the triage-to-Implementation route for a flagged ticket;
  - `return_finding` and `close_on_finding` on any other ticket.

  Each refusal names the command that fixes it. The trigger also clears an
  earlier round's commit when a no-commit finding leaves Research.
- **`note_research_round`.** An `AFTER UPDATE` trigger. Entering Research starts
  a round: a new clock, no notice and no finding yet. A submission out of
  Research records which form it took and the commit.
- **`note_research_timeboxes`.** The listener runs it once per pass. It tells
  the Director once per round when a research ticket's timebox passed in
  Research with no submission, and stops nothing.
  `research_timebox_notice_is_current` keeps a late notice from being
  delivered once its round is over.
- **`ticket_research_handoff`.** The listener reads this line for a transition
  notice:
  - for the researcher, the brief and the file a findings commit changes;
  - for Audit, the finding and what to review;
  - for the Director, back in triage, the finding.

## What git must say (`scripts/ticket_board/research_finding.py`)

`before_transition`, run by the service:
- **`submit_finding`.** The commit must:
  - be known to the board's trusted commit cache;
  - have exactly one parent;
  - add or edit exactly `<directory>/<TICKET>.md`, as an ordinary
    non-executable file, and nothing else.

  The comment names the commit and the file. A merge, an empty commit, another
  path, an executable file or a second file is refused, and the refusal names
  the path.
- **Other moves.** A commit is refused on every move but `submit_finding`.
  Reasons are bounded: 4000 characters, or 16000 for a no-commit finding,
  which is the whole finding.

The finding commit is published like any candidate. The board checks its
content, not its ref.

## Surfaces

- **`switchyard research-stage <project> enable --role R | --runtime CLI
  [--template-role T] [--research-directory D] | disable [--dry-run]`.** It runs
  through `workflow_manage`'s apply path, which is in `TENANT_WRITES`, so root
  is refused. It shares role-prompt's dispatch block.
- **`ticket-board-write` commands** (in the new `stage_cli.py`;
  `write_cli.py` shrank):
  - `research-brief`, `create-ticket --research --question --timebox
    --usable-answer`, `start-research`, `close-on-finding` and `return-finding`;
  - `submit-finding`, `submit-finding-without-commit` and `research-hand-back`;
  - `research-sign-off` and `research-kick-back`.
- **`switchyard new`.** After the documentation gate, the menu
  "Research stage (optional)" offers:
  - off (the default);
  - each implementer by name;
  - `researcher`, a new role asked the guided way.

  The choice is recorded as `project.research` and read back by `--from`. It is
  set by `team-launcher design --research-role / --researcher-*`, and applied
  by `research_onboarding.plan_with_research_stage`.
- **Onboarding.** The Researcher's guide, copied into every project; the
  Director guide's "Research spikes" section; the Director skill; the launcher
  guide; the documentation map.

## Tests

`tests/research_stage_test.py` covers:
- **Composition:** with an existing role and with a new Researcher, every
  refusal, off and on again, and all 24 orders with the documentation gate,
  Router and the Publisher.
- **Broken shapes:** each refused by `workflow_config`.
- **The board's own validator:** on a `schema.sql` board, and on main's tree
  upgraded with this tree's migrations.
- **The flow on the migrated board**, with the real server and listener, a git
  repository as the commit cache, and the documentation gate and Router on:
  - every refusal;
  - a findings commit through Audit's kick-back and sign-off, closed on the
    finding, with each hand-off notice checked;
  - every malformed findings commit, with the ticket untouched;
  - the no-commit form, the hand-back, the Director's return, `needs_audit`
    off, and a User wait in Research;
  - the timebox notice: once per round, again in a new round, and not after the
    round ends;
  - disable, refused while occupied.
- **The real `ticket-board-write`.**
- **`switchyard research-stage`** against a real projection. It runs under a
  slug no host registers, and checks that it resolves its own configuration
  before running anything.
- **`switchyard new`'s** record, prompt, summary and payload.
