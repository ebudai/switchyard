# SYRD-678: a designer may also own Implementation work

> **Historical design and rollout evidence.** This ticket record describes the
> revision that allowed a designer to implement; it is not the current
> procedure. Use
> [A designer that also implements](onboarding/switchyard-director-guide.md#a-designer-that-also-implements)
> and current command help before acting.

## What pie asked for

pie's design review runs as an ordinary loop:
1. The designer is assigned the ticket in Implementation.
2. It submits the design to Audit with a commit.
3. Audit's kick-back returns it to the designer.
4. Audit's sign-off moves it on.

The board refused the loop in two places:
- The validator requires every Implementation owner to be of kind `implementer`.
- Assigning a non-owner is refused.

Making the designer an implementer would also end its being the designer. Its
kind decides its presentation, onboarding, pane and worker pool. Every round
needed two Director overrides.

## The shape

A role's `kind` stays its single identity. What it may do is its kind plus any
`additional_kinds` it declares:

```json
{"name": "designer", "kind": "draft", "additional_kinds": ["implementer"],
 "routing_scope": {"flag": "design_work"}, ...}
```

- **Additional kinds are limited to one combination:** a `draft` role may also
  be an `implementer`. Every other combination is refused: a reviewer or an
  implementer declaring more, a designer declaring `reviewer`, an empty or
  repeated list, or a string. A draft role that owns Implementation without
  declaring it is refused as before.
- **Everywhere the board asked "is this role an implementer" by kind, it now
  asks `role_has_kind`.** This covers Implementation ownership (both
  validators), the remembered implementer (`last_implementer_assignee`), Router's
  assignable owners, and the pull claim and its claimant pool. Everything that
  follows ownership was already ownership-based: serial focus, reservations,
  notifications, the queue and SYRD-673's producer of a revision. All of these
  now apply to the designer unchanged.
- **The designer stays the designer:** `kind: draft`, owner of Draft,
  `release_draft`, its onboarding prompt, presentation label, pane and worker pool.
- **`routing_scope` names a declared gate flag.**
  - Router may give the role only tickets carrying that flag. Router's
    `assignable` owners on the ticket leave it out otherwise, and
    `routing.scopes` says why.
  - `router_assign` refuses it, both over HTTP and in the database.
  - The Director's own assignment is not limited.
  - A role with a scope may not be ephemeral under pull scheduling, because a
    claimant takes pulled work of any kind.
- **Documents that declare neither field behave exactly as before.**

## Enabling it

```bash
ticket-board-workflow enable-designer-implementation --board-url <board> --config <tenant config> \
    [--routing-flag design_work] [--dry-run]
```

`designer_implementation.with_designer_implementation` makes these changes:
- adds `additional_kinds` to the active designer (which needs a pane);
- adds the designer to the Implementation stage's owners, and to every move all
  its current owners take into or out of that stage, except a pull claim;
- with `--routing-flag`, declares the gate flag (off by default) and the scope.

The result is validated like `apply`, and written the way every Director
workflow write is. Since SYRD-676, that no longer needs root, however the
tenant was provisioned.

## Evidence

`tests/designer_implementer_test.py` runs on boards built the production way,
through the HTTP API, `ticket-board-write` and the notify listener. It also
runs on main, where the document is refused ("implementation owners must be
implementers").
