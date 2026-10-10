# SYRD-573: who may claim ready work survives a worker rotation

Origin: MEFP, 2026-10-07 (`mefp:pull-eligibility-runtime-rotation-2026-10-07`),
the live follow-up to the SYRD-539 pull queue.

## What happened

MEFP enabled pull with its claim transition naming the eight Luna workers then
running. It later rotated two slots: it stopped luna-1 and luna-2 (Claude) and
started luna-7 and luna-10 (Codex), keeping eight running. All fifteen Lunas
were already declared active, ephemeral implementers that own Implementation.
The rotation changed which sessions ran, not the document.

| Symptom | Cause |
|---|---|
| `claim-next` from luna-7 or luna-10: "role ... cannot claim ready work" | Claim authority was the claim transition's literal actor list. `claim_ready_ticket`, the claimant-assignment trigger, the transition actor checks and the listener's pickup all read it. |
| "these implementers are idle without claiming it: luna-2 (no registered provider process)" | Pickup put every listed worker it could not use into the idle notice, including workers that were not running at all. |
| `worker-pool status`: 0 able to take a ticket | Every worker was judged by the pool's single runtime. A worker rotated to Codex was checked for Claude's sign-in, board skill and folder trust. |

## The rule

On a board with a pull policy, the **claimant pool** is every role that:
- is declared active;
- is an implementer;
- is ephemeral;
- owns the implementation stage.

The rule reads only the role declarations. It names no tenant's roles, and it
does not read the claim transition's actor list.
- **Ephemeral** was already required of every claimant (SYRD-539/540), because
  a claimed ticket starts its own conversation. It is also what keeps a
  persistent implementer, such as a tenant's `main` or `ops`, out of the pool.
  Such a role is routed by the Director as before.
- **The actor list** is still validated as before, and every document that
  validated before still validates. The list never named anyone outside the
  pool, because every listed actor had to be an ephemeral implementer and a
  claimant must own the stage it lands in. So nothing that could claim before
  is refused now.

One rule, two copies that are tested to agree:
- SQL: `ticket_board.pull_claimant_pool(cfg)` and
  `ticket_board.transition_allows_actor(cfg, tr, actor)`.
- Python: `workflow_config.pull_claimant_pool` and
  `workflow_config.transition_allows_actor`.

`transition_allows_actor` means one of the transition's actors, or, for the
claim transition, any member of the pool. These ask it:

| Path | Where |
|---|---|
| `claim-next` | `claim_ready_ticket` |
| the listener's pickup | `claim_ready_ticket`, for each pool member it judges eligible |
| the canonical claim action | `perform_workflow_action_as`, then `enforce_declared_ticket_update`; on the HTTP side `available_transitions`, which also gives the ticket its `workflow_actions` |
| assigning the claimant | `lock_pull_assignment` |
| the idle notice | `notify_pull_idle_capacity` ignores any name outside the pool |
| status | `worker-pool status`, and the queue's per-claimant context report |

## Running is the listener's to judge

Of the pool, the listener claims only for a worker whose registered provider
process is still the live one. It reports as idle only such a worker that
cannot take work: busy, on a prompt, with unknown readiness, or idle with
nothing claimable.
- A worker with **no registered provider process**, or whose **registered
  process is gone**, is not running. It is neither claimed for nor named.
- A running worker whose readiness is unknown is still named as unknown.

## The cap

The board never starts a worker. Concurrent claims are therefore bounded by the
claimants that are running, which is the cap MEFP keeps by running eight
sessions. Serial reservations are unchanged:
- one active ticket per worker;
- a ticket in Audit holds its author until Audit's declared approval, and then
  the author may claim again;
- pinned rework waits for its author.

## worker-pool status

- Each worker is checked against the runtime its role declares, falling back to
  the pool's. This covers sign-in, the board skill and folder trust.
- On a pull board, each worker line says whether it "claims ready work". The
  capacity line adds how many workers can claim now.
- A board without a pull policy prints exactly what it printed before.

## Migration

`pgu982_syrd573_pull_claimant_pool.sql` adds the two functions and carries
schema.sql's copies of the five functions that now ask them. It moves no
ticket and sends nothing. On a board without a pull policy the pool is empty,
so every check is unchanged.

## What MEFP gets on deployment

Assuming its document stays at revision 109, the pool is luna-1 to luna-15:
- `main` and `ops` are not ephemeral, so they are not in it;
- the eight running Lunas (luna-3 to luna-10) are the claimants;
- luna-1, luna-2 and luna-11 to luna-15 are stopped, so they are neither
  claimed for nor named.

No workflow edit is needed. The board does not start a ninth worker, and
neither does anything else.

## Tests

`tests/pull_claimant_rotation_test.py` builds the rotation with the real
worker-pool expansion. It uses a bench of four with the claim list frozen at
the first two, and `main` and `ops` persistent. It covers:
- the rule clause by clause, in Python and SQL, with the answers compared;
- a replacement claiming through `claim-next` and through the canonical
  action, on fresh, migrated and upgraded boards built the production way;
- a persistent implementer refused by both paths;
- an Audit-held reservation, held until approval and then released;
- the idle notice naming only free declared claimants;
- the real listener claiming for the two running replacements and no more;
- no notice minted for stopped workers, and a running worker with unknown
  readiness named once;
- worker-pool status judging each worker by its own runtime.

`tests/pull_pickup_test.py` changes in one place. A worker with no registered
provider process is no longer named in the idle notice, which is the
behaviour this ticket reports.
