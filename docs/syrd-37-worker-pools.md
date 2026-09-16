# A pool of interchangeable workers

Some projects want one implementer with a long memory. Others want a bench of
them: several workers of the same runtime, told apart only by number, started
when there is work and retired when there is not. Writing that out as N role
entries means N places to keep in step, and it puts a project's operating model
into the shape of its configuration file rather than into a decision.

So a project declares a pool:

```json
"worker_pool": {
  "name": "impl",
  "runtime": "hermes",
  "size": 8,
  "kind": "implementer",
  "presentation": "on-demand",
  "ephemeral": true
}
```

and that expands to `impl-1 … impl-8`. Nothing about eight, about Hermes, or
about the project that asked for this is in the code: the name, the runtime, the
size, the kind, whether a worker holds a pane, and whether it starts each ticket
with a cleared session are the tenant's to declare. A pool of two auditors on
another runtime is the same feature.

`presentation: on-demand` is the default because a bench is usually larger than
a window can show: the persistent roles keep their panes, and a worker is
attached when somebody asks to watch it. A project that wants every worker
visible says `attached` instead.

`ephemeral: true` is what makes a worker interchangeable rather than an
implementer with a history -- it starts each ticket from a cleared session, the
behaviour SYRD-135 already gives any role that asks for it.

## The preflight

`switchyard worker-pool <project>` reports what bringing the declared pool up
would change and what would stop it, and **changes nothing**: no role, account,
worktree, board registration or session is created, and no existing role, ticket
or credential is touched. Blockers are marked and counted separately from
changes, because an operator reading a list of twelve lines needs to find the
two that matter.

What it checks, and why each is a blocker rather than a note:

- **the runtime is installed for the owner user**, because panes run as that
  account and a CLI installed for somebody else is not there;
- **the runtime is authenticated for that account**, because a worker whose
  provider is not signed in opens its first run instead of a prompt -- which is
  exactly how eight workers become eight sign-in screens (SYRD-191);
- **the board knows each worker's identity**, because a role the board cannot
  name is one it will not route work to or notify;
- **no worker name is already somebody else's role**, because a pool must not
  take over a role another part of the project declared.

Run against a tenant, it reads that tenant's live board rather than a file: what
matters is which roles the board will actually route work to.

## What this does not do yet

This is the declaration and the preflight. Starting, addressing, observing and
retiring individual workers, and the concurrency and replacement rules for a
bench, are the next tranche; the existing `add-role` path already carries the
board registration, Postgres role, worktree and pane creation a worker needs, so
that tranche is composition rather than new mechanism. Nothing here brings a
worker up, and nothing here should be read as having rehearsed one.
