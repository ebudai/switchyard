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
  "ephemeral": true,
  "onboarding_prompt": "You are one of a bench of implementers. Take one ticket."
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

`presentation: attached` promises every worker a pane, and a window has six
slots. The free ones are handed out in order, and a pool that would need a
seventh is refused by name -- with the count and the suggestion to declare it
on-demand -- rather than by the document validator complaining about a number.

`ephemeral: true` is what makes a worker interchangeable rather than an
implementer with a history -- it starts each ticket from a cleared session, the
behaviour SYRD-135 already gives any role that asks for it.

`onboarding_prompt` is the bench's remit, declared once because every worker in
it does the same job. A pool that declares none inherits the remit of the role
its workers are copied from, which is the right default and a poor answer for a
bench whose job differs from that role's (SYRD-36). The bound is the workflow
document's own, so a pool cannot declare a prompt the document would refuse at
apply time.

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
matters is which roles the board will actually route work to. It also checks
that the tenant's declared workflow names a **holding destination** (`queue`),
because a pool is one ticket per worker and the board keeps it that way by
diverting the extra ticket somewhere; a document with nowhere to divert to makes
the board refuse the routing outright, at the moment a director tries to give a
busy worker a second ticket (SYRD-31).

## Where a worker durably exists

In the tenant's own workflow document, not in a table in this code. Expanding a
pool adds, for each worker:

- a **role**, copied from an existing active role of the pool's kind -- its
  capabilities and the stages it owns come from that template, so a worker does
  the job some role in this tenant already does rather than a job invented here;
- ownership of every stage the template owns, which is what makes the worker a
  valid assignee, gives it the board's per-role serial reservation, and makes it
  the notification target for its own tickets;
- a place in the **actors** of every transition the template may take, because
  owning a stage without being able to move out of it is a worker handed tickets
  it cannot submit;
- `slot: null`, so the projection makes it a detached session -- on-demand
  presentation is the absence of a pane, not a special kind of one;
- `ephemeral` and `serial` as the pool declares them.

One rule names the worker everywhere: role `impl-3`, session `<project>-impl-3`,
pane target `<project>-impl-3:0.0`, worktree `<worktree base>/impl-3`. The board
enforces the last of those itself -- `role_runtime_assignments` has a unique
constraint on the pane address and `register_role_runtime` refuses a target that
is not the one the document declares -- so two workers cannot come to share an
address and a notification cannot land on the wrong one.

The launcher config is a **projection** of the document
(`workflow_launcher.project_roles`), so a worker declared once exists as a board
identity, a notification target and a pane without anyone writing it three
times.

## Concurrency, and what a bench does to the lanes below it

A worker holds **one ticket at a time**. That is not new: the board already
reroutes a second ticket routed to a busy implementer to the tenant's declared
holding destination and records what it is waiting for and behind what
(SYRD-31). A pool of N workers is therefore N tickets in flight, and the
N+1th waits for a free worker.

The lanes downstream are the part a bench changes. Eight workers submit into one
review stage; without anything else, eight tickets sit there assigned to one
reviewer, all with a deliverable notification addressed to the same pane, and
nothing in the board saying which is current. That is the ambiguity; the silence
is its twin, because a reviewer working the second ticket while five more are
delivered has no record of what it did not get to.

So a role may be declared **serial**:

```json
{"name": "inspector", "kind": "reviewer", "serial": true, ...}
```

A serial role is handed the oldest waiting ticket in the stages it owns, and the
notifications behind that one are **deferred** -- requeued with a reason naming
the ticket ahead of them, traced as `finish_current_defer`, and still there.
Never dropped. Order is by arrival (`entered_current_state_at`, then ticket
number), so the lane has a head, an order and a queue.

`switchyard worker-pool <project> admission` reports this, lane by lane, and
says which lanes are ambiguous. Expanding a pool **serialises the review lanes
it feeds** and reports having done so, rather than reporting the problem and
leaving it: a bench whose reviewers are not serialised is the failure this
exists to remove. Only roles the document calls reviewers are touched -- a
control or user stage deliberately holds several tickets at once.

Absent means unchanged. A tenant that declares no `serial` role has exactly the
behaviour it had before: the implementation stage serialised, everything else
not.

## The life of one worker

```
switchyard worker-pool <project>                     # preflight: what it would cost
switchyard worker-pool <project> plan                # the ordered upgrade, with the way back out
switchyard worker-pool <project> apply [--apply]     # declare the pool in the workflow document
switchyard worker-pool <project> list                # every worker, what it needs, what it holds
switchyard worker-pool <project> admission           # the review lanes, and whether they are ordered
switchyard worker-pool <project> start   <worker>    # bring one up
switchyard worker-pool <project> stop    <worker>    # end its session, keep its identity and its work
switchyard worker-pool <project> restart <worker>    # recover a stuck runtime
switchyard worker-pool <project> attach  <worker>    # watch one, on demand
switchyard worker-pool <project> retire  <worker>    # take one out of service, for good
switchyard worker-pool <project> replace <worker>    # retire one and declare a fresh identity
switchyard worker-pool <project> rollback --journal  # undo an apply
```

Everything that would change a board, a configuration or an account shows what
it would do and writes nothing without `--apply`. `start`, `stop` and `restart`
act, because a tmux session is the one thing here that is not durable state.

None of it escalates. The document goes through the board's own workflow API as
the invoking role and the sessions live on the project account's tmux server, so
`worker-pool` is an unprivileged command -- which matters most for `attach`,
whose entire point is a terminal on a worker with no privileged parent shell
behind it (SYRD-76).

### Before a worker's first ticket

`start` refuses a worker that is not ready, and says which subsystem is missing
rather than saying "not ready": the board skill for its runtime, its onboarding
prompt, its runtime's authentication, its Unix account, its worktree, and
whether the document declares and routes it. The skill and the prompt are on
that list for a reason that cannot be fixed afterwards -- a worker started
without them takes its first ticket without knowing how to read the board, and
installing them later does not get that turn back (SYRD-35, SYRD-36).

`--force` starts anyway, and the report says it was forced and what it was
forced past.

### Retirement and replacement

Retiring **deactivates**; it never deletes. The name stays in the document, so
nothing can be given it again and so the tickets, comments and notification
traces that name it keep resolving to the worker that did the work. The board
agrees: `register_role_runtime` refuses a role that is not active configuration,
so a retired identity cannot come back.

Replacing retires one worker and declares a fresh one at the next number past
**every** member the pool has ever held, retired ones included. Retire `impl-3`
from a pool of eight and the replacement is `impl-9`.

### Cleanup and recovery

- A worker whose session has gone but whose identity is live: notifications for
  it are **requeued** with `role_runtime_unassigned`, not acked away. Restarting
  the worker delivers them.
- A worker whose runtime is stuck: `restart` is stop-then-start, reported as two
  actions rather than one, because a restart whose stop succeeded and whose
  start did not is a stopped worker.
- A pool applied and then regretted: `rollback --journal <path>` reverses the
  document write through the ordinary workflow rollback, which restores both the
  board revision and the launcher projection from the journal the apply wrote
  before it touched the board.

## Two shapes of tenant

A tenant running a **declared workflow** gains its workers in one reviewed
document apply -- which is also what serialises the review lanes and what the
rollback journal reverses.

A tenant still running the **built-in workflow** has no document to declare them
in. Its workers are registered one at a time through `switchyard add-role`,
which is the supported registration path for such a tenant, and each run is
privileged and journalled. What it cannot have is a serialised review lane: that
is a property of a document it does not have. `plan` says so as a blocker rather
than leaving it to be discovered, and migrating the tenant to a declared
workflow is a separate reviewed change.

## What this does not do

It does not bring up a real provider session, and it does not claim one. The
rehearsal in `tests/worker_pool_rehearsal_postgres_test.py` drives a real board
end to end -- eight workers, eight routed tickets, their own addresses, the
review lane's head and deferred tail, retirement, replacement and recovery --
but observing a live Hermes session load the board skill and take a real turn is
User UAT, and a fixture cannot stand in for it.
