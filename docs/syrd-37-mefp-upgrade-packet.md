# The mefp upgrade packet: eight ephemeral Hermes implementers

This is the acceptance scenario for the worker-pool capability, written out as
the operator would run it. Nothing in this document is a hardcode: every
tenant-specific value here -- the slug, the roles, the runtime, the eight -- is
configuration that the code reads and this document happens to name, which is
what an acceptance scenario is.

**Nothing in this packet has been run.** The Director's activation on
2026-09-16 says: *"Prepare the reusable eight-ephemeral-Hermes capability and
exact mefp dry-run/preflight. Do not perform the live mefp upgrade."* What
follows is the sequence, the evidence it rests on, the things that will stop it,
and the way back out of each step.

## What was observed, read-only

Every fact below was read without changing anything, from this host, on
2026-09-16. Where a fact cannot be read without privilege it says so rather than
being asserted.

| what | how it was read | what it said |
| --- | --- | --- |
| the board is running | `systemctl list-units` | `mefp-ticket-board.service` active |
| its address | `/etc/systemd/system/mefp-ticket-board.service` | `127.0.0.1:26623`, socket `/run/mefp-ticket-board/ticket-board.sock` |
| its roles | the same unit's environment | implementers `main,ops`; assignees `unassigned,main,ops,audit,director,user`; callers `director,main,ops,audit,user` |
| whether it declares a workflow | `GET /api/workflow` | `Not found` -- it runs the built-in workflow |
| its stages | `GET /api/board` | `draft, analysis, in_progress, audit, dat, user_review, director_review, done, cancelled` |
| how much work is on it | `GET /api/board` | **0 tickets** |
| root's provisioning record | `ls /etc/switchyard/provision` | `syrd` and `testing` only -- root holds **no** record for mefp |
| the tenant's own configuration | `ls /home/stellaris-agent/…` | not readable from this account; the repository boundary is closed, as SYRD-175/181 intended |
| Hermes's authentication for `stellaris-agent` | `switchyard worker-pool` preflight, 2026-09-16 (tranche 1) | installed, **unauthenticated** |

The last row is the one that cannot be re-read from here: probing it runs the
CLI as the owner account. The preflight re-checks it every time it runs, and the
run below is what settles it on the day.

## What the board's shape means for this upgrade

Two consequences follow from the observations, and both are about mefp rather
than about pools:

1. **mefp has no declared workflow.** Its roles are named in the board's
   `workflow_stages` rows, seeded from its provisioning plan. Workers can still
   be registered -- `switchyard add-role` is the supported path for such a
   tenant, and each run is privileged and journalled -- but a review lane cannot
   be serialised, because `serial` is a field of a document mefp does not have.
   With one reviewer (`audit`) and eight workers, that means up to eight tickets
   assigned to `audit` at once with nothing saying which is current.

2. **mefp has no `backlog` stage and no `inspection` stage.** Provisioned
   tenants exclude both (`TENANT_WORKFLOW_EXCLUDED_STAGES`). `backlog` is the
   shape a holding destination has: not terminal, owned by nobody, announcing
   nothing. A declared workflow must keep one -- the validator refuses a
   document without it -- and the serial-focus reservation needs somewhere to
   divert the second ticket routed to a busy worker. So migrating mefp to a
   declared workflow is not a transcription of what it runs today: it has to
   **gain a parking stage**, with a director route in and out of it, and that is
   a change to the tenant's workflow which only the Director may review and
   approve.

Neither of these is discovered at run time here. Both are reported by
`switchyard worker-pool mefp` and `switchyard worker-pool mefp plan` before
anything moves.

## The sequence

Run as an operator on this host. `switchyard worker-pool` is unprivileged and
must be run as the tenant owner (`stellaris-agent`) or, to read a closed tenant
configuration, under `pkexec`. Every privileged step is wrapped in
`switchyard-record-rollout` so its output, exit status and operator are kept in
root's journal and a role can read them back with
`switchyard rollout-log mefp` (SYRD-128, SYRD-132).

### 0. Read it again, change nothing

```
switchyard worker-pool mefp
switchyard worker-pool mefp plan
```

The preflight is the gate. It reports, for the pool mefp declares: whether
Hermes is installed and signed in for `stellaris-agent`, whether the board knows
each worker, whether any worker name is already somebody else's role, and what
the presentation would look like. The plan reports the ordered steps and, next
to each, the blocker that step clears. A blocker no step clears is reported as a
blocker on the plan too.

**Expected on the day, from the observations above:** two blockers -- the
unauthenticated Hermes runtime, and eight identities the board does not know --
and a legacy-tenant plan whose `review admission` step is itself a blocker.

### 1. Declare the pool in mefp's configuration

`worker_pool` is a field of the tenant's launcher configuration:

```json
"worker_pool": {
  "name": "impl",
  "runtime": "hermes",
  "size": 8,
  "kind": "implementer",
  "presentation": "on-demand",
  "ephemeral": true,
  "onboarding_prompt": "<what a mefp Hermes implementer is for, in the Director's words>"
}
```

It is refused at load time if it names something the system could not run: a
size outside 1..64, a name that cannot be a role, account and tmux session, an
empty runtime, an unknown presentation mode, or an unknown field.

**Undo:** remove the field. Declaring a pool creates nothing on its own.

### 2. Sign Hermes in once, for the owner account

```
switchyard mefp
```

The ordinary project start collects the owner's provider first run before any
pane opens, and it is **one** sign-in for the whole pool: every Hermes role's
`HERMES_HOME` symlinks `auth.json`, `.env`, `config.yaml` and `skills` from the
owner's shared `~/.hermes`, so eight workers inherit one login and one skill
tree (SYRD-191).

**Undo:** nothing to undo. A credential is the account's, not the pool's.

### 3. Install the canonical board skill for the runtime

```
switchyard board-skill install
```

Installs `switchyard-board` into `~/.hermes/skills`, which every worker reads
through that symlink. This is before any worker's first ticket on purpose: a
worker started without it takes its first ticket not knowing how to read the
board, and installing it afterwards does not get that turn back (SYRD-35).

**Undo:** the installed copy carries a Switchyard provenance footer and can be
deleted; nothing else wrote it.

### 4. Register the workers

Because mefp runs the built-in workflow, this is the `add-role` path, once per
worker, privileged and journalled:

```
switchyard-record-rollout mefp --label add-worker-impl-1 -- \
  switchyard add-role mefp impl-1 --cli hermes --detached
```

`add-role` writes the role into the launcher configuration, regenerates the
tenant's plan and board unit, applies the incremental workflow SQL that makes
the worker an owner of the implementation stage and an actor of its
transitions, restarts the board unit, prepares the worker's worktree, and starts
its session. `--detached` is what on-demand presentation means: no permanent
pane.

**Undo:** each run is in the rollout journal. A worker is withdrawn by removing
its role from the launcher configuration and re-running
`switchyard upgrade mefp`; `switchyard teardown mefp --dry-run` lists what a
full withdrawal would remove, and changes nothing.

### 5. Or migrate mefp to a declared workflow first

This is the alternative to step 4 and the only way to get a serialised review
lane. It is a separate reviewed change, and it needs a document that adds a
parking stage to what mefp runs today:

```
ticket-board-workflow apply --document <reviewed mefp workflow> \
  --rollback-document <reviewed baseline> \
  --config /home/stellaris-agent/stellaris-bugfix/.switchyard/provision/mefp.json --dry-run
```

`--dry-run` validates the document against the running board -- including every
refusal about omitted stages, omitted assigned roles and outstanding review
obligations -- and writes nothing. mefp carrying **0 tickets** is what makes
this the cheap moment to do it: there is no outstanding review to preserve and
no assigned role to retain.

With a document in place, the pool is declared in one apply:

```
switchyard worker-pool mefp apply --out /tmp/mefp-workers.json          # shows it
switchyard worker-pool mefp apply --out /tmp/mefp-workers.json --apply  # writes it
```

which adds the eight roles, makes them owners and actors, and serialises the
review lanes they feed -- reporting each of those as a change.

**Undo:** the apply prints a journal path, written before the board was touched.

```
switchyard worker-pool mefp rollback --journal <that path>
```

restores the previous board revision and the previous launcher projection
together.

### 6. Bring workers up, on demand

```
switchyard worker-pool mefp list
<release>/scripts/ticket-board-workflow prepare-role --config <mefp launcher config> --role impl-1
switchyard worker-pool mefp start impl-1
switchyard worker-pool mefp attach impl-1
```

`list` reports every worker, what it still needs, and what it is holding.
A worker declared by `worker-pool apply` has no worktree yet, and `start` does
not create one: prepare it once with `prepare-role`, which sets up its
worktree, hooks and folder trust and refuses a runtime that is not signed in.
Preflight and `start` both print that command, filled in, for a worker that
still needs it. `start` refuses a worker that is not ready, names the subsystem
that is missing, and exits non-zero (SYRD-278). `attach` is how somebody watches
one; it never escalates.

**Undo:** `switchyard worker-pool mefp stop impl-1` ends the session and leaves
the identity and the work where they are.

## What the rehearsal proves, and what it cannot

`tests/worker_pool_rehearsal_postgres_test.py` runs the whole of the above
against a real PostgreSQL board on a disposable cluster: eight workers declared
from one pool, each a role the board knows with its own pane address that the
board itself refuses to let another take; eight tickets routed to eight workers,
each cleared and then handed its own ticket at its own address and nobody
else's; a ninth ticket diverted to the tenant's holding destination with a
durable record of who it is waiting for and behind what; eight submissions into
one review lane where exactly one is delivered -- the oldest by arrival, against
the numbering -- and the other seven are deferred, traced and still queued;
the same eight all delivered at once when the `serial` declaration is cleared,
which is what shows the property comes from the document; a retired worker whose
identity survives and cannot be registered again; and work addressed to a worker
with no live runtime waiting rather than vanishing.

What it cannot prove, and does not claim: that a live Hermes session loads the
installed `switchyard-board` skill and uses it correctly on a routed ticket, and
that a Hermes Director turn discovers and follows `switchyard-director`. Those
are observations of a model's behaviour on real turns. They are the Director's
and the User's UAT (SYRD-35, SYRD-38), and a fixture standing in for them would
be the packet claiming something nobody saw.
