# SYRD-794: a process the current ticket's work started is that work, whatever its session

## What happened

When main was handed SYRD-785 (2026-11-15), the Director got two notices: "main was handed SYRD-785 with background
work still running in its pane from earlier work", one for pid 1712264 and one for pid 1712265. They were
`postgres: io worker 0` and `io worker 1` of a throwaway test cluster that main's current SYRD-785 sweep had started:
- **the sweep:** bwrap, then `runtime_switch_stall_test.py`;
- **the postmaster:** in the sweep's session;
- **the io workers:** PostgreSQL 18 starts each one in a session of its own (pid = pgid = sid).

The hand-over report (`handoff_children.leftovers`, SYRD-636) judges each session under the pane on its own: its
worktree, its lease, its age. A worker runs in the cluster's data directory and holds no lease, so it was "earlier
work". Every suite that starts a cluster would have produced one false notice per io worker.

## What changed

A session whose parent chain reaches a session that is the role's current work, by its worktree or by its lease, is
that work too, and is not reported.
- **The walk** reads each parent from /proc as it is now.
- **No reused pid:** each parent must have started no later than its child (start ticks since boot), so a pid reused
  since can never stand in for a parent.
- **The bounds:** the walk stops at the pane or at init.

What is still reported, as before (SYRD-583, SYRD-636):
- a child whose parents have ended (an orphan reaches nothing);
- a child whose ancestry reaches only a finished ticket's work, or nothing known.

## Tests

`tests/setsid_child_attribution_test.py`. Real processes run under a stand-in pane that is a child subreaper, so that
an orphan stays in its tree as a provider's leftovers do: a "sweep" in a session of its own in a real git worktree,
its "postmaster" (run from a data directory), and the postmaster's child, which calls setsid. The real `leftovers`
samples the real /proc.
- In the current ticket's worktree: nothing is reported. Then the sweep and postmaster are killed, and the orphaned
  worker is reported.
- In a finished ticket's worktree: the sweep and the worker are both reported.
- Over a fake /proc:
  - a grandchild through a session of its own is attributed;
  - a "parent" whose start is later than the child's (a reused pid) is not;
  - the pane's own child is reported.

Mutants:
- killed: the ancestry check dropped; a reused parent pid believed; a finished ticket counted as current; only the
  first parent looked at;
- equivalent, which survives: letting the walk go past the pane. Every session it could match lies under the pane,
  and a process above the pane cannot share one without a reused pid. The bound only keeps the walk short.
