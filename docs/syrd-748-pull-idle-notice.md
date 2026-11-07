# SYRD-748: the pull idle notice names only idle workers

Origin: MEFP, 2026-11-07.

## What was reported

MEFP-990 waited in `pull_ready_source_first` from 04:19 UTC. The pull idle notice said:

> MEFP-990 has been ready to claim since 2026-11-07 04:19 UTC, and these implementers are idle without claiming it:
> luna-2 (working).

`worker-pool status` called Luna-2 ready, with its slot free. The Director withdrew MEFP-990 and routed it to Luna-2
by hand.

## What the evidence shows

From MEFP's board, read through its `/api/board`:

| Time (UTC) | Event |
|---|---|
| 18:27:07 | Luna-2 starts MEFP-988, a package continuation of MEFP-979. |
| 18:30:12 | Luna-2 submits MEFP-988 without code. |
| 18:31:10 | Audit signs it off, which frees Luna-2's slot (`release_after: audit`). |
| 18:32:22 | The Director withdraws MEFP-990 ("Pull-ready delivery failed while Luna-2 was idle"). |

So Luna-2 was free for about 70 seconds before the manual route. Over that time the pickup's eligibility test
(`pull_pickup.why_not_eligible`) called it "working".

**The recovery that would have claimed.** A Codex turn that ends without its Stop hook (SYRD-594) leaves the hook at
the turn's start. The gate recovers such a turn once it is `stale_codex_busy_hook_seconds` old (120 s) and the screen
shows Codex's empty prompt (`_stale_codex_busy_trace`). Pickup then claims. Reproduced on current main: a freed Codex
worker in that state is recovered and claimed for, with no delivery to it in between. 70 s is inside that window.

**What is missing.** No notification id, failure kind, trace row, pane-state record or pane capture was preserved.
MEFP's listener journal and runtime directory are private to its tenant account. The Director decided (21:10 UTC) that
the event does not establish a defect in claiming, and that no speculative gate, composer, child-process or delivery
change should be made. If it recurs after the 120 s boundary, a new report must carry the notification id and kind and
the trace or pane-state evidence, so that exact path can be fixed.

**The one defect the evidence proves.** The notice called a worker that was still working "idle without claiming".
The notice fires once per ready episode, at the first pass where a claimant holds no reservation. For MEFP-990 that was
the moment Audit freed Luna-2, while its last turn was still finishing. The notice named every running claimant that
had not claimed, with its reason, including "working" and "background work running".

## The change

`pull_pickup` no longer passes to `notify_pull_idle_capacity` any claimant whose reason is in `WORKING`: "working",
or "background work running". A worker still finishing its turn, or with background work running, is not idle capacity
that failed to claim. It claims when its turn is over, or when the gate recovers a stale Codex turn.

Still named, as before:
- a worker stopped on a prompt;
- one whose readiness is unknown;
- one that is idle with nothing claimable.

The notice still fires once per episode. A pass where every free claimant is working sends nothing and records nothing,
so a later idle non-claimant is still reported for that ticket.

**Unchanged:**
- the 120 s stale-turn recovery, and claiming;
- `demand_start`'s busy set;
- SYRD-736's stranded-rework check, which reads the full idle map;
- stopped or providerless workers, which are neither claimed for nor named.

## Tests

`tests/pull_idle_notice_test.py` uses a production-built board with the example workflow opted into pull scheduling:
- the real HTTP server and notify listener;
- the real PaneActivityGate and its pane probes, over Codex's measured screens;
- main as the Codex claimant, with a live stand-in provider process;
- the stale-turn window scaled to 30 s.

Main runs first, from a git archive.

| Case | Main | This tree |
|---|---|---|
| Freed by Audit, its turn still finishing (hook 2 s old) | "main (working)" | names nobody, and claims nothing yet |
| Its turn then ends | claims | claims |
| A stale turn at the empty prompt | recovered and claimed | recovered and claimed |
| Its turn ended on background work still running | "main (background work running)" | nothing |
| Stopped on a prompt with its slot free | named, once | named, once |

## Mutation

Four mutants, all killed:
- no filter;
- "working" not filtered;
- "background work running" not filtered;
- everything filtered.
