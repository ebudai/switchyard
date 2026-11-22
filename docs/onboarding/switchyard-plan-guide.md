# Switchyard Plan Guide

The onboarding document for the **plan gate**: how a project's implementation
plan is written, changed, reviewed and used. Read it if you own the plan (the
project's designer, or the role `switchyard plan-gate` named), if you are Audit
reviewing a plan ticket, or if you are the Director scoping work from the plan.

It is project-agnostic. Examples use `PGU-123`; your project has its own prefix.

---

## Why the gate exists

A plan that the Director edits while scoping each ticket decays: it grows without
bound, its near-term detail swallows the later milestones, and it fills with
status, findings and rulings until it is a log. Nobody reviews the whole of it.
The plan gate gives the plan one owner, one way to change it, and a review of the
whole document every time it changes.

## The three levels

1. **Milestone.** A coherent, shippable step, with a stable ID (`M1`, `M2`, ...).
2. **Deliverable.** A testable unit of work inside a milestone, with a stable ID
   (`D1.1`, `D1.2`, ...). Every deliverable states:
   - its **acceptance criteria**, concrete enough to test;
   - its **dependencies** (other deliverables, external facts);
   - its **owner specialty** (which kind of implementer takes it).
3. **Ticket.** The board's, not the plan's. The Director creates tickets from a
   deliverable; each names the deliverable it serves.

Only the **current and the next milestone** are detailed. Later milestones are
listed with their intent, in a sentence or two, and detailed when they become
next.

## The index

The board reads one thing from the plan: its index, a single fenced block tagged
`plan-index`, holding JSON. It lists every milestone in order, whether it is
detailed, and its deliverables:

````markdown
```plan-index
{"milestones": [
  {"id": "M1", "title": "Ingest", "detailed": true,
   "deliverables": [{"id": "D1.1", "title": "Reader"}, {"id": "D1.2", "title": "Indexer"}]},
  {"id": "M2", "title": "Query", "detailed": false,
   "deliverables": [{"id": "D2.1", "title": "Search"}]}
]}
```
````

The rules, which the board enforces when a plan ticket is submitted:
- exactly one `plan-index` block;
- every ID is unique and stable (letters, digits, dots, dashes; 32 characters at
  most). Never renumber; a removed deliverable's ID is not reused;
- each milestone has exactly `id`, `title`, `detailed` and `deliverables`, and
  each deliverable exactly `id` and `title`. There is no field for status,
  progress, dates or rulings;
- a detailed milestone lists at least one deliverable.

The prose around the index carries each deliverable's acceptance criteria,
dependencies and owner specialty. The board reads the index at the **approved
commit**, the commit the last plan ticket was closed on, and nowhere else.

## What never goes in the plan

- **Status or progress.** The board has it: a deliverable's tickets and their
  states.
- **History.** Git has it.
- **Rulings and decisions.** They go to the decision log
  (`docs/decision-log.md` unless the project chose another), with their date.
- **Follow-ups, fixes and findings.** They are backlog tickets, not packets
  added to the plan.

## Plan tickets: the only way the plan changes

A plan ticket carries the `plan_change` gate. The Director creates one
(`ticket-board-write create-ticket --plan`, or `plan-ticket <id>`), or a scope
change opens one for you. A plan ticket needs no deliverable and is not unplanned: it
changes the plan itself. Marking it turns on its User sign-off gate; leave its Audit gate
on too (the default), because the board does not add Audit for you. You work it in
Implementation like any ticket:

- its candidate changes the plan file and, where a ruling belongs on record, the
  decision log, and nothing else. The board refuses any other change, and a
  plan whose index is missing or malformed;
- Audit reviews the **whole plan** against the checklist below;
- the User approves it in User Review (after the Director's DAT, where the project
  runs it). The board refuses to close a plan ticket the User did not accept;
- the Director signs it off and closes it. The commit it is closed on becomes
  the approved plan.

**Re-plan at milestone boundaries.** When every ticket of a milestone's
deliverables is Done or cancelled, you and the Director are told once that a
re-plan is due. Detail the next milestone then, and list the one after it. Until
the next milestone is detailed, tickets for its deliverables are refused
Implementation.

## Audit's plan checklist

Audit reviews a plan ticket's candidate as a whole document, not as a diff.
Refuse it unless:

- [ ] every deliverable is testable and has acceptance criteria;
- [ ] every deliverable's dependencies are stated;
- [ ] every deliverable belongs to a milestone, and the index matches the prose;
- [ ] the next milestone is detailed before the current one closes;
- [ ] there is no history, status or ruling in the plan (rulings are in the
      decision log);
- [ ] it is within the size limit (1,500 lines unless the project chose another).
      The board flags a plan over the limit on any candidate that changes the plan
      file, a plan ticket's included; a plan no candidate changes is the approved one,
      measured when it was approved.

## Adopting the plan gate on an existing project

Do not rewrite an existing plan in place. At a milestone boundary of the
project's choosing:

1. Freeze the old plan as an archive (for example
   `docs/archive/implementation-plan-2026-11.md`), unchanged. It is history.
2. Turn the gate on: `switchyard plan-gate <project> enable --owner designer`
   (or `--runtime <cli>` for a new Designer).
3. The Director creates the first plan ticket (`ticket-board-write create-ticket
   --plan`) and routes it to the plan owner, who writes a new plan for the
   **remaining work only**, in the three levels, with its index. Rulings the old
   plan carried move to the decision log.
4. Until that plan ticket is closed there is no approved plan, so no deliverable
   can be named: a ticket routed into Implementation meanwhile is marked unplanned
   with a reason. Tickets already in Implementation, and work a review sends back
   there, are not checked again.
5. Once that plan ticket is closed, the Director names a deliverable, or
   `unplanned` with a reason, on each open ticket before routing it.
