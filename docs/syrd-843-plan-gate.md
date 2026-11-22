# SYRD-843: an optional plan gate — a designer-owned, audited, User-approved three-level plan the Director scopes from

> **Historical design and rollout evidence.** This ticket record describes the
> revision that introduced the plan gate; it is not the current operating
> contract. Use the [plan guide](onboarding/switchyard-plan-guide.md), the
> [Director guide](onboarding/switchyard-director-guide.md#scoping-from-a-plan-the-optional-plan-gate)
> and `switchyard plan-gate --help` before acting.

## The ask

On sapi, `docs/implementation-plan.md` started with three levels and decayed in
five weeks:
- it grew from 852 lines to 12,185;
- one phase took about 6,350 lines, while the later phases stayed a sentence
  each;
- it filled with about 112 audit notes and rulings;
- 278 of its 279 commits were the Director's.

The causes were:
- the wrong owner, with no gate;
- the plan doubling as the backlog;
- re-planning one packet at a time.

The User asked for an optional gate, modelled on the documentation gate. No
existing plan is migrated.

## The shape (`scripts/ticket_board/plan_gate.py`)

`with_plan_gate(document, owner=<role> | designer={runtime, template_role,
slot}, plan_file, decision_log, max_lines)` adds:

- **`plan_change`**: a gate flag, off by default, that marks a plan ticket.
- **The plan owner as an Implementation owner.**
  - An existing owner must already own Implementation.
  - Choosing the designer applies `with_designer_implementation` (SYRD-678),
    with `plan_change` as its routing scope.
  - A new Designer is a detached `designer` of kind `draft`, copied from a
    template role, then made the same.
- **The top-level `plan` policy:** `{owner, file, decision_log, max_lines}`.
  The defaults are `docs/implementation-plan.md`, `docs/decision-log.md` and
  1,500 lines.

**No stage is added.** A plan ticket goes through the ordinary path: the owner
implements it, Audit reviews it, the User approves it in User Review, and the
Director closes it. That needed no change to the executor, and it composes
with the documentation gate, Router, the Publisher and the research stage in
all 120 orders, each turned off and on again. `without_plan_gate` removes the
flag, the policy and the designer's routing scope.

## The index

The index is a single fenced block tagged `plan-index` in the plan file, holding
JSON: an ordered list of milestones (`id`, `title`, `detailed`,
`deliverables[{id, title}]`). `parse_plan_index` checks:
- that there is exactly one block;
- unique stable IDs;
- that a detailed milestone lists its deliverables;
- that no other field appears. There is no field for status, history or
  rulings.

A fenced block was chosen over front matter or a sibling file for two reasons:
- **It stays with the prose.** A plan ticket cannot change one without being
  able to see the other.
- **It needs no YAML parser.**

The board reads it only at the **approved commit**, the commit the last plan
ticket was closed on.

## The database (`pgu1052_syrd843_plan_gate.sql`)

All of it applies only where the declared workflow has the `plan` policy.

- **`plan_indexes`.** An index per commit, recorded by the service, which reads
  git. **`plan_approvals`.** A plan ticket closed into the finished stage
  (mark_done's terminal stage) makes its commit's index the approved plan.
- **`require_plan_scope`** refuses:
  - a route into Implementation (a declared forward `move` from a non-review
    stage) without a deliverable or an unplanned reason. Returns and reopens
    are never checked: a kick-back, the Publisher's return or the User's
    feedback sends work already in flight back, whatever its deliverable;
  - a route serving a deliverable the approved plan no longer has;
  - a route serving a deliverable of an undetailed milestone ("re-plan due");
  - closing a plan ticket without `user_signoff`, or without `audit_signoff`
    where the workflow runs Audit;
  - closing a plan ticket on a commit whose index the board has not read.

  Plan tickets need no deliverable. Research tickets enter Research, not
  Implementation, so they need none either; they may name one.
- **`set_ticket_deliverable`** is the Director's (`director_edit`).
  - It takes a deliverable, which must be in the approved plan; an unknown one
    is refused with the plan's list.
  - Or it takes `unplanned` with a reason.
- **`request_scope_change`** is the Director's (`set_blockers`).
  - It opens a plan ticket for the owner: created, flagged, needing the User,
    and moved by the board's own declared Director move into Implementation.
  - Or it extends the open plan ticket with a comment.
  - Either way, the waiting ticket is then blocked on it.
- **The milestone-complete notice.** When every deliverable of a milestone has
  tickets and all are terminal, the Director and the plan owner are told once
  that a re-plan is due, with whether the next milestone is detailed.
- **`note_plan_finding`.** A finding is recorded once per ticket, commit and kind,
  as a comment for Audit and a notice for the Director. There are two kinds:
  - `plan_file_touched`: a non-plan candidate changed the plan file;
  - `plan_size`: the plan file is over the limit.
- **Turning the gate off.** A trigger on the workflow configuration refuses
  removing the policy while a plan ticket is open.
- **`plan_status()`.** It reports the approved commit, milestones and
  deliverables with ticket counts by state, unplanned tickets counted apart,
  and the re-plan-due markers. It is the read for the board UI.

## What git must say (`scripts/ticket_board/plan_candidate.py`)

- **Submission out of Implementation.** The candidate is compared with its
  merge-base with main.
  - A plan ticket's candidate changes only the plan file and the decision log,
    and its plan has a valid index. The index is recorded.
  - A non-plan candidate that changes the plan file is a finding, and so is any
    candidate at which the plan file is over the size limit, whatever it
    changes. Findings refuse nothing.
- **Closing a plan ticket.** The index is read and recorded at the closing
  commit.
- **A documentation commit that changes the plan file is refused.** The
  Librarian may move a ruling to the decision log, never into the plan.

## Surfaces

- **`switchyard plan-gate <project> enable --owner R | --runtime CLI
  [--template-role T] [--plan-file F] [--decision-log F] [--max-lines N] |
  disable [--dry-run]`.** It runs through `workflow_manage`, a `TENANT_WRITES`
  operation, in the shared dispatch block.
- **`ticket-board-write`:**
  - `set-deliverable` (alias `set-ticket-deliverable`), `request-scope-change`
    and `plan-ticket [--off]`;
  - `create-ticket --deliverable | --unplanned-reason | --plan`.
- **`switchyard new`.** "Plan gate (optional)": off, the designer, or an
  implementer. It is recorded as `project.plan` and set by `team-launcher
  design --plan-owner`.
- **Docs:**
  - the plan guide: the three levels, the index, what never goes in, Audit's
    checklist, and adopting the gate on an existing project;
  - the Director guide and skill, the Librarian guide, the launcher guide and
    the documentation map.
- **Board UI.** The deliverable on the ticket view and a plan view are a
  successor ticket for app, as the Director's comment allows. `plan_status()`
  is its read.

## Tests

`tests/plan_gate_test.py` covers:
- composition, every refusal, and off and on again, in 120 orders;
- the index rules;
- the board's validator on both build paths;
- the full flow on a migrated board, with the documentation gate, Router and the
  research stage on;
- the real CLI;
- `switchyard plan-gate` against a real projection.

## Audit's kick-back of d06711b2 (round 2)

- **A plan could be approved without Audit.** The board refused closing a plan
  ticket the User had not accepted, but not one whose `needs_audit` was turned
  off. It now refuses closing one without `audit_signoff` where the workflow
  runs Audit, and marking a plan ticket turns `needs_audit` on as well.
- **Returns were checked as routes.** The deliverable check applied to every
  move into Implementation from a non-review stage, so publication_return,
  publication_kick_back and director_send_to_implementer were refused for a
  ticket in flight with no deliverable, or one whose deliverable a re-plan
  dropped. It now applies only to a declared forward `move`. This is read from
  the transition the update takes (`ticket_board.workflow_action`), which the
  Publisher's own finish sets too.
- **The size alarm covers any candidate**, as the ticket asks, not only one
  that changes the plan file.
