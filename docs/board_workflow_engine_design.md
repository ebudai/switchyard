# Design Spike: Data-Driven Ticket Stages + Roles (PGU-516)

Pure design spike, no implementation. Grounded directly in the current engine
(`scripts/ticket_board/schema.sql`, `app.py`, `server.py`,
`frontend_script_core.py`) at commit `e00c9af` (origin/main). Every claim below
cites the actual current code, not a paraphrase.

## Key insight the spike builds on

The board is already halfway to this model — it just isn't expressed as data:

- `needs_inspection` / `needs_eric_signoff` are already "a stage entered only if
  a boolean is set" (`enforce_ticket_workflow_update`, schema.sql:623-636,
  616-619). The *concept* of an optional, gated stage already exists.
- `in_progress` is already "a stage shared by several roles" — any of
  `main/app/ops/perf/research` can own it
  (`ticket_is_implementer_assignee`, referenced schema.sql:575, 608, 712).

So this isn't inventing a new capability; it's *unifying* six places that each
hardcode a fragment of the same underlying concept:

| # | Concern | Where today |
|---|---|---|
| 1 | State enum / CHECK | `app.py:26` `STATES` tuple; `schema.sql:39,136` `CHECK (state IN (...))` |
| 2 | Transition whitelist | `enforce_ticket_workflow_update`, schema.sql:653-668 (one big `IF NOT (... OR ... OR ...)` chain) |
| 3 | RBAC per transition | `require_actor(ARRAY[...], 'name')` called once per SQL function — 19+ call sites, e.g. schema.sql:3541 (`route`→director-only), 3696 (`start_work`→implementer roles), 3980 (`audit_sign_off`→audit-only) |
| 4 | Notify routing | `transition_target_role(p_state, p_assignee)`, schema.sql:1432-1445 — a `CASE` mapping state→role |
| 5 | Frontend columns + ordering | `COLUMNS` array, `frontend_script_core.py:4-14`; `state_rank(p_state)` SQL function, schema.sql (adjacent to transition_target_role) — two independently hand-maintained lists that must stay in sync with #1 |
| 6 | Per-transition behavior | One `CREATE FUNCTION` per action (`start_work`, `submit_to_audit`, `audit_sign_off`, `eric_reopen`, `release_draft`, ~15 total) each duplicating its own RBAC check + state write |

Six places, one concept, currently kept in sync by hand. That's the lift.

## A) Data model

Two tables replace the six hardcoded pieces.

```sql
CREATE TABLE ticket_board.workflow_stages (
    name            text PRIMARY KEY,          -- 'inspection', 'director_review', ...
    display_label   text NOT NULL,             -- 'Inspection' (frontend COLUMNS.label)
    rank            integer NOT NULL UNIQUE,   -- replaces state_rank() CASE + COLUMNS order
    owner_roles     text[] NOT NULL,           -- {'inspector'} single-owner, or
                                                -- {'main','app','ops','perf','research'} shared
    is_terminal     boolean NOT NULL DEFAULT false,   -- 'done','cancelled'
    entry_gate_field  text,                    -- e.g. 'needs_inspection'; NULL = always enterable
    gate_skip_to      text REFERENCES ticket_board.workflow_stages(name),
                                                -- where to go instead if the gate is false
    exit_signoff_field text                    -- e.g. 'audit_signoff'; when it flips true,
                                                -- auto-advance out (mirrors schema.sql:641-644)
);

CREATE TABLE ticket_board.workflow_transitions (
    from_stage      text NOT NULL REFERENCES ticket_board.workflow_stages(name),
    to_stage        text NOT NULL REFERENCES ticket_board.workflow_stages(name),
    action_name     text NOT NULL,             -- 'submit_to_audit', 'audit_sign_off', ...
    allowed_roles   text[] NOT NULL,           -- RBAC as data (replaces require_actor ARRAY literal)
    is_auto         boolean NOT NULL DEFAULT false,  -- fires on signoff flip, no explicit action
    PRIMARY KEY (from_stage, to_stage, action_name)
);
```

Worked mapping of *today's actual rows* (a representative subset, not the full
table) — this is what Phase 0's data migration writes, verbatim from the
current hardcoded lists:

```
workflow_stages:
 name             rank  owner_roles                              entry_gate_field    gate_skip_to      exit_signoff_field
 draft            0     {}                                       -                   -                 -
 backlog          1     {}                                       -                   -                 -
 analysis         2     {director}                                -                   -                 -
 in_progress      3     {main,app,ops,perf,research}              -                   -                 -
 inspection       4     {inspector}                               needs_inspection    audit             inspector_signoff
 audit            5     {audit}                                   -                   -                 audit_signoff
 eric_review      6     {director}                                needs_eric_signoff  director_review   eric_signoff
 director_review  7     {director}                                -                   -                 -
 done             8     {}                                        -                   -                 -           is_terminal
 cancelled        9     {}                                        -                   -                 -           is_terminal

workflow_transitions (subset):
 from_stage   to_stage    action_name           allowed_roles
 in_progress  inspection  submit_to_inspection  {main,app,ops,perf,research}
 in_progress  audit       submit_to_audit       {main,app,ops,perf,research}
 inspection   audit       inspector_sign_off    {inspector}
 audit        eric_review audit_sign_off        {audit}          -- via exit_signoff_field auto-route
 audit        director_review audit_sign_off    {audit}          -- same action, different target picked by needs_eric_signoff
 eric_review  director_review eric_sign_off     {eric}
 director_review done     mark_done             {director}
```

Note `audit_sign_off` legitimately routes to *two different* `to_stage`s
depending on `needs_eric_signoff` (schema.sql:641-644) — this is exactly the
"optional gate" concept applied to an *exit* rather than an *entry*; the model
above (`exit_signoff_field` + a small resolver that consults the ticket's own
gate fields) covers it without a special case.

## B) Derivation — nothing hand-maintained twice

- **RBAC**: every `require_actor(ARRAY[...], 'name')` call site becomes one
  generic `require_actor_for_transition(from_stage, to_stage, action_name,
  actor)` that looks up `workflow_transitions.allowed_roles`. The 19+ literal
  `ARRAY[...]` in schema.sql collapse to one lookup function plus data rows.
- **Notify routing**: `transition_target_role(state, assignee)` becomes: if
  `workflow_stages.owner_roles` has exactly one entry, that's the target role;
  if it has more than one (a *shared* stage), fall back to
  `NULLIF(assignee, 'unassigned')` — exactly today's `in_progress` behavior,
  now expressed as "shared stage → assignee-routed," not a special case.
- **Frontend COLUMNS**: the frontend fetches
  `SELECT name, display_label FROM workflow_stages ORDER BY rank` (a small
  addition to the existing board payload, or a new `/api/stages` endpoint)
  instead of the hardcoded JS array in `frontend_script_core.py`. One
  source of truth; the JS array is deleted, not duplicated.
- **state_rank**: literally `SELECT rank FROM workflow_stages WHERE name = ...`
  — the hand-written `CASE` disappears.

## C) Optional + shared + insertable stages

- **Optional entry**: generalizes `needs_inspection`/`needs_eric_signoff`
  exactly as shown in the table above — `entry_gate_field` names the boolean
  ticket column; if it's false, the transition target resolves to
  `gate_skip_to` instead. Adding a THIRD optional gate (say, a
  `needs_perf_review` stage) is a new `workflow_stages` row + a new boolean
  column on `tickets` — no trigger-function edit.
- **Shared stages**: `owner_roles` as an array is the whole mechanism; no
  separate concept needed.
- **Inserting a stage — the "director eyes" example, as actual config**:

```sql
-- Shift inspection and everything after it down one rank to make room.
UPDATE ticket_board.workflow_stages SET rank = rank + 1 WHERE rank >= 4;

INSERT INTO ticket_board.workflow_stages (name, display_label, rank, owner_roles)
VALUES ('director_eyes', 'Director Eyes', 4, ARRAY['director']);

INSERT INTO ticket_board.workflow_transitions (from_stage, to_stage, action_name, allowed_roles) VALUES
  ('in_progress', 'director_eyes', 'submit_to_director_eyes', ARRAY['main','app','ops','perf','research']),
  ('director_eyes', 'inspection',   'director_eyes_pass',      ARRAY['director']),
  ('director_eyes', 'in_progress',  'director_eyes_kick_back',  ARRAY['director']);
```

Three inserts, zero code changes, zero deploys. This is the entire point of
the spike and the concrete example the ticket asked to see as config.

## D) Migration path (the important part)

**Not** a big-bang cutover. The config-driven engine is built *alongside* the
current one and cut over one dimension at a time, safest first — and at every
phase, the full existing suite (`ticket_board_schema_test.py`,
`ticket_board_rbac_test.py`, `ticket_board_postgres_triggers_test.py`,
`ticket_board_postgres_backend_test.py`,
`ticket_board_migration_runner_test.sh`, plus the PGU-504 Playwright UI
harness) runs **unmodified** and must stay green. If it doesn't, the migration
step isn't done, full stop.

- **Phase 0 — mirror, don't change.** Create `workflow_stages` /
  `workflow_transitions`, populate them to exactly reproduce today's hardcoded
  lists (a data migration, not a redesign). Write one new equivalence test:
  compile the config back into the old shapes (a `(from,to)` allow-set and a
  `(action, allowed_roles)` map) and assert byte-identical to hand-transcribed
  copies of the current `STATES`/whitelist/`require_actor` literals. This test
  is the safety net for every later phase — if the config ever drifts from
  intent, this fails first.
- **Phase 1 — cosmetic (frontend COLUMNS + `state_rank`).** Read from config.
  Zero backend/RBAC risk; purely presentational. Do this first because a bug
  here is visually obvious and reversible in seconds.
- **Phase 2 — stage ordering** wherever rank is consumed (progress bars, sort
  order). Still read-only against config; still no enforcement change.
- **Phase 3 — transitions, in shadow mode first.** Rewrite
  `enforce_ticket_workflow_update`'s whitelist to consult
  `workflow_transitions`, but run it *alongside* the existing hardcoded
  version for a soak period: compute both, log (don't enforce) any
  disagreement. Only flip to config-as-source-of-truth once a soak period
  shows zero disagreements across real traffic. This is standard practice for
  replacing a trusted decision function (shadow-mode / dual-computation
  validation) and directly addresses "how do we know the new engine agrees
  with the old one" without trusting a one-time test suite alone.
- **Phase 4 — RBAC, last, on purpose.** `require_actor` calls move to the
  generic config-driven lookup only after 0-3 are stable. This is the
  highest-stakes piece: this session hit a real RBAC mis-wire live (main's
  `route` call rejected with "main cannot call route" during the PGU-11 work,
  because `route` was director-only and the packet didn't account for it) —
  that class of mistake, multiplied across a much larger config-driven
  surface, is exactly the risk this phase ordering is designed to catch last,
  with the smallest, best-tested blast radius by the time it's touched.

## E) Risk analysis

The core risk is auditability: a ~15-line hardcoded `IF` chain can be read and
verified by eye in one sitting (as this session's own audit role has done
repeatedly). A config table can encode a much larger transition graph, most of
which never gets exercised by manual testing — silent gaps are the real
danger, not loud failures.

Mitigation: **exhaustive, not spot-check, testing of the config itself.**
Author one checked-in "golden matrix" fixture — every `(from_stage, to_stage,
role)` triple in the full state space, hand-labeled allow/deny — and test the
*runtime config* against that fixture exhaustively, not just the handful of
transitions a hand-written test happens to exercise. This turns "did I wire
this right" from sampling into total coverage, which is the only way a much
larger declarative surface stays as trustworthy as today's small hardcoded
one.

Second, smaller risk: the Phase 3 shadow-mode dual-computation is itself
temporary complexity (two systems computing the same answer). Flag it
explicitly as debt to delete once cutover is confirmed stable — it should
never become a permanent parallel system.

## F) The serial-gate caveat

Making it *this* easy to insert a single-owner stage (three `INSERT`s, per
the worked example above) re-enables the exact anti-pattern this project has
already hit once: a single-owner review gate that stalls everything flowing
through it while that one role is busy or away. The flexibility is the
feature and the foot-gun simultaneously.

Recommendation: make the **cost** of a single-owner stage visible at
creation time, not just in hindsight. Concretely:
- A computed `bottleneck_risk` view: for any `workflow_stages` row with
  `array_length(owner_roles, 1) = 1`, surface current ticket count + average
  dwell time in that stage on the board UI, so a single-owner gate's real cost
  is visible the moment it starts backing up, not after Eric notices things
  have stalled.
- Require a non-empty justification comment on any `workflow_stages` INSERT
  with a single-role `owner_roles` (enforced by a trigger, not just a
  convention) — a deliberate small speed bump so adding a serial gate is a
  decision, not a one-line accident.

## G) Effort / scope estimate + recommendation

- Phase 0 (config tables + mirror + equivalence test): small-to-medium *effort*,
  but demands real care — this is a mechanical transcription of every rule
  in section A's table, and a transcription error here undermines every later
  phase's safety net.
- Phase 1-2 (cosmetic + ordering): small, low-risk, good first deliverable to
  build confidence in the approach.
- Phase 3 (transitions + shadow mode): medium — the actual design work, plus
  a soak period that takes calendar time more than engineering time.
- Phase 4 (RBAC cutover): medium effort, but treat as HIGH-CARE given the
  real precedent bug cited above.
- Overall: a genuine multi-phase project (weeks of elapsed time across a soak
  period, not a quick refactor), against a 4,600-line schema file with dense,
  interleaved trigger logic. The value is real and concrete — the
  "director eyes" example goes from "touch 6 files + redeploy" to "3 SQL
  inserts" — and the board has already organically converged on most of the
  needed concepts (optional gates, shared stages) without anyone designing
  them as such, which is a good sign the abstraction is the right shape, not
  a speculative one.
- **Recommendation: proceed, staged exactly as in section D, scheduled for a
  render-work lull as Eric specified — but do not skip or compress the
  phase ordering (cosmetic → ordering → transitions/shadow-mode →
  RBAC-last) or the golden-matrix exhaustive test from section E.** Those two
  things are what keep this as trustworthy as the current hardcoded engine;
  without them, the "big lift" risk is real, not just a formality.
