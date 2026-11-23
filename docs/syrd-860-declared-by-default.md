# SYRD-860: every new tenant declares its workflow

## What happened

A tenant made by `switchyard new` without `--workflow-config` had no declared workflow, even after a complete run:
- `GET /api/workflow` answered `{"revision": 0, "document": null}`;
- every composer (research stage, plan gate, Router, documentation gate, Publisher) refused with "no workflow
  configuration is active for this board";
- provisioning seeded the legacy default workflow as table rows (`workflow_seed: default-project`) and declared no
  document.

A document was generated only when the design turned on Router, the documentation gate, the research stage or the
plan gate, because each of those composers needs a document to compose onto.

## The decision

The Director decided `switchyard new` declares a workflow by default. Defaults do the normal thing, and `new` itself
offers the research stage and the plan gate.

## What changed

**The document.** `new_project_command` now gives every new tenant a declared document before anything is rendered,
through `router_onboarding.declared_by_default`. Unless the tenant already has one, that function composes it with
`router_onboarding.plan_document`, then validates it. `plan_document` is the generator the options already used: it
calls `legacy_workflow.compose_legacy_workflow`, the same one `adopt-workflow` uses to compose a document for an
older tenant, so there is no second generator.
- **Where the document comes from.** `--workflow-config`, or a workflow a non-design artifact carries, is used
  instead, exactly as before.
- **The answers from `new`.** Router, the documentation gate, the research stage and the plan gate, when chosen,
  compose onto whichever document it is.
- **Provisioning.** The workflow SQL applies the document through `apply_declared_workflow`, before the board starts,
  as it already did for a declared tenant. Root records its own copy (`workflow.json`). The launcher config's roles
  are projected in the document's order: `director` first, then the implementers, Audit and the designer.

**The design artifact.** The artifact writer copies the declared workflow back into the design artifact. A design
artifact may not carry one (`PROJECT_DESIGN_FORBIDDEN_KEYS`), so `switchyard new --from` would refuse a retry. Router
and the Librarian already took it out again; the research stage, the plan gate and a Director-owned documentation
gate did not. Now `new` takes it out for every design artifact (`router_onboarding.drop_artifact_workflow`). The
launcher config and root's record hold it.

## Where the legacy table seed stays

`build_plan` still sets `workflow_seed = "default-project"`; `render_workflow_sql` applies the declared document
whenever the plan has one. The seed is still used by:
- **tenants provisioned before this change.** Their plans declare no workflow, so `upgrade`, `resume-provision` and
  replays render the same rows as before. They are unchanged.
- **`render_add_role_sql` and `render_vcs_close_role_sql`.** They edit the rows of such tenants. Their callers,
  `switchyard add-role` and `set-vcs-close-role`, refuse a declared tenant, and every new tenant is one now. Each
  refusal names that tenant's path: `add-role` already did (SYRD-510); `set-vcs-close-role` now does too (the
  stage, approval and `mark_done` to change in the document, and `ticket-board-workflow apply`). The board guide says
  which projects each command is for.
- **`adopt-workflow`'s legacy composition** (`propose_legacy_workflow_adoption`). It is how an older tenant gets a
  document, and it is gated on that seed.
- **`team-launcher new` from a non-design artifact.** A legacy artifact carrying its own workflow is used as given.

## Existing undeclared tenants

They are unchanged. Every composer's refusal on them now names the way forward, for the project in hand:

> this board has no declared workflow, so there is nothing to compose onto: adopt one first. `switchyard
> adopt-workflow <project>` shows the document composed from the project's plan, `pkexec switchyard adopt-workflow
> <project> --apply` records it, `pkexec switchyard migrate-workflow <project> --apply` hands it to the Director,
> and `switchyard finish-upgrade <project>`, run from the Director's pane, installs it on the board

## Outside a role pane

A composer's dry run is the board's own check, asked as a role. With no `TICKET_BOARD_CALLER_ROLE`, it used to end in
the write client's `ValueError`. It now refuses in one line: "… asks the board as the Director: run it from the
Director's pane (TICKET_BOARD_CALLER_ROLE is not set here)".

## Tests

- **`tests/router_onboarding_test.py`.** With every option off, a design artifact provisions a declared workflow with
  no Router, and the artifact carries no workflow afterwards. On a real board built from that tenant's SQL:
  - `GET /api/workflow` answers a revision above 0;
  - `enable-research-stage --dry-run` and `enable-plan-gate --dry-run` succeed through `workflow_manage.main`, the
    command the Director runs.
- **`tests/team_launcher_new_project_test.py`.** It is re-pinned: the dry run's SQL applies the declared document, not
  rows, with the same stages, owners and moves, and the roles come in the document's order.
- **`tests/undeclared_workflow_refusal_test.py`.** Each composer on an undeclared board refuses in one line naming the
  adopt step for the project. Outside a role pane, it refuses in one line, never reaching the write client.
