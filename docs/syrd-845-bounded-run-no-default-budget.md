# SYRD-845: bounded_run has no default whole-run budget, and a case's hang deadline scales from its clean run

## What was wrong

The User, on 2026-11-22: "I don't want time limits on the test runs, sometimes a really long run is reasonable."
SYRD-844 put that into the reviewer guidance, but `tests/bounded_run.py`, the harness the methodology tells everyone
to use, still enforced durations:
- **`DEFAULT_TOTAL_SECONDS = 540.0`:** a whole-run budget, chosen to fit the Bash tool's 600-second foreground limit.
  A scoped plan that legitimately took longer ended with its remaining mutants recorded as inconclusive.
- **`DEFAULT_CASE_SECONDS = 300.0`:** a fixed per-case deadline, meant as hang protection. A suite whose clean run
  takes longer than 300 s could never be mutation-tested without a flag, and one at 219 s
  (`execution_lease_test`) had little room.

## The change

**No whole-run budget by default.** `DEFAULT_TOTAL_SECONDS` is now `None`, and `Deadline(None)` never runs out. A
caller that wants a budget still asks for one with `total_seconds=`, `--total-seconds` or
`SWITCHYARD_BOUNDED_TOTAL_SECONDS`, and it applies as before.

**Each case's hang deadline scales with its suite.** Mutation tools derive a test's timeout from its normal runtime,
with a factor and a constant. Here the formula is:

    deadline = max(CASE_TIMEOUT_FLOOR_SECONDS, clean_seconds x CASE_TIMEOUT_FACTOR)
    k = 10, floor = 60 s

- **Why k = 10.** The old fixed 300 s was chosen as "about nine times the slowest suite" measured in 2026-09 (32 s).
  Ten keeps that margin, but for every suite: a 219 s suite now gets 2,190 s, not a 300 s it barely fits.
  - The margin matters because a mutant can make a suite legitimately slower, for example by removing a fast path
    or making the suite wait out a timeout of its own. Such a case should finish, not be cut off.
  - The cost of the margin: a mutant that truly loops is held for ten clean runs before it is stopped. The User
    accepts long runs, and looping mutants are rare.
- **Why a 60 s floor.** Most focused suites run in under a second (the 2026-09 median), and the measured clean run
  then says little. The floor covers interpreter, PostgreSQL and fixture start-up jitter, and a host loaded by a
  parallel sweep, so a slow start is never read as a hang.

**Where the clean runtime comes from:**
- **Measured here:** before the first mutant, `run_mutation_plan` runs each suite once on the clean tree. The plan
  starts from the unmodified tree, and each mutant is restored byte-for-byte.
- **Passed in:** a caller that already measured the clean runs passes `clean_seconds=` (one per suite), or
  `"clean_seconds"` in a `mutate` plan file, and no clean run happens.
- **Recorded:** each suite record now carries the deadline it ran under (`"timeout"`).

**A clean run that fails or leaves processes behind gives no baseline,** and every mutant is `inconclusive` with the
reason. A suite that fails on the clean tree would otherwise "kill" every mutant (mutation tools refuse to run in this
case for the same reason). The clean run has no deadline (SYRD-846, below). A caller may cap it with
`clean_run_seconds=`, and a whole-run budget, when given, covers the clean runs too. A clean run past either is
refused the same way.

**An explicit deadline still overrides the scaling:** `case_seconds=`, `--case-seconds` or
`SWITCHYARD_BOUNDED_CASE_SECONDS`. No clean run is needed then.

**`run`, one command,** has nothing to measure. It takes `--case-seconds`, or `--clean-seconds S` (the same formula,
for automatic hang protection). With neither, it has no deadline (SYRD-846, below).

**A looping mutant is still stopped** at its scaled deadline, and recorded as `inconclusive`, never as a kill or a
pass.

## Running a long plan

With no budget, a plan can outlast a foreground tool call. There are two ways to run one, and the module docstring
and the methodology say both:
- **in the background,** through the CLI's background-task tool, which says when the command exits;
- **under `switchyard-bounded --expect <duration>`,** which tells the board how long the command is expected to take
  and stops nothing.

The board's 15-minute long-command notice to the Director is unchanged: it reports, and it never stops anything.

## Text updated

- **`tests/bounded_run.py`:** the docstring's limits section, and the `mutate --help` text.
- **`docs/onboarding/adversarial-collaborative-methodology.md`:** "Bound every mutation or adversarial run" gives the
  scaled deadline, no default budget, and the two ways to run a long plan.
- **`tests/residual_processes_boundary_test.py`:** a comment citing "the documented per-case bound" now names the
  unmeasured-command default.

`git grep 540` finds no other statement of the old budget.

## Not changed

- **`switchyard-bounded`:** its `--deadline` is always the caller's own.
- **The board's long-command notices.**
- **`run_bounded` and its cleanup.**

## Evidence

`tests/bounded_run_scaled_deadline_test.py`. Plans that would take hours run against a fake clock and a stubbed
`run_bounded`; the looping mutant and the command line run real processes.
- **Main first,** loaded from git (`7b85764e`): four 200 s cases end at the 540 s budget, the rest inconclusive. A
  400 s suite times out at the fixed 300 s, even with a large budget.
- **No budget:** the same 800 s plan gives every mutant its verdict, with nothing inconclusive.
- **A budget still applies,** given as an argument or through the environment.
- **The scaling:**
  - clean runs of 30, 219, 400 and 0.3 s give deadlines of 300, 2,190, 4,000 and 60 s, each measured once;
  - each suite gets its own deadline, and `clean_seconds` skips the clean run;
  - an explicit deadline (argument or environment) overrides the scaling, with no clean run.
- **No baseline:** a suite red on the clean tree runs no mutant and makes every one inconclusive. (SYRD-846 replaced
  the hour's bound on a hung clean run with no default cap.)
- **Real processes:**
  - a mutant that loops forever is stopped at its scaled deadline (a 1.5 s floor in the test) and recorded as
    inconclusive, and never reaches the kill decision;
  - a real mutant beside it is killed.
- **Command line:**
  - `run --clean-seconds` gives the floor, and `run --case-seconds` is the deadline itself;
  - a `mutate` plan with `clean_seconds` records its scaled deadline, and `--help` states the defaults.
- **Text:** the docstring and the methodology no longer state 540 s, and both say how to run a long plan.

The existing `tests/bounded_run_test.py` (SYRD-403) passes unchanged.

## SYRD-846: the last fixed caps, and two pinned branches

The Director's rule since SYRD-845 is that the only acceptable hang protection is protection that scales with the run.
Two fixed caps were left, and both are gone:
- **The clean run has no default cap.** `CLEAN_RUN_SECONDS` is now None. The clean run is the unmodified candidate, so
  a hang there is a real defect in the code under review, not a mutant artefact. A caller may still give a cap with
  `clean_run_seconds=`.
- **`run` with nothing measured has no deadline.** `DEFAULT_CASE_SECONDS` is now None. `--clean-seconds` keeps the
  scaled deadline (automatic hang protection), and `--case-seconds` is still the deadline itself. The help, the module
  docstring and the methodology say which to pass.
- **The backstop** for a run with no deadline is the board's long-command lease. It tells the Director about any
  command under a role's pane still running after 15 minutes, with a handle to interrupt it, and it stops nothing by
  itself.
- **`run_bounded(timeout=None)`** (or an infinite timeout) waits for the command however long it takes, and cleans up
  the same way. A clean run with no cap records `"timeout": null`.

Two branches had no test, and a mutant of each survived SYRD-845's suite:
- **The Director's mutant** gave every suite the first suite's measured deadline. A measured plan now has a fast suite
  first and a slow one after it, through the stubbed `run_bounded`. Each suite's recorded deadline must follow its own
  clean run (60, 300 and 90 s for clean runs of 2, 30 and 9 s), and the slow suite must not be cut off.
- **Audit's M3** dropped the refusal when the clean run leaves processes behind. A clean run that reports a group still
  present after cleanup (`leftover_group`, as `run_bounded` reports one it could not stop) must run no mutant, and
  every mutant must be inconclusive with "left processes behind".

**Tests**, in `tests/bounded_run_scaled_deadline_test.py`:
- **Main first,** loaded from git at `410f9359`:
  - main refuses a two-hour clean suite at its hour, with every mutant inconclusive;
  - main's `run` with nothing measured stops at 300 s.
- **No clean-run cap by default:** a five-hour clean run, faked, completes and scales to 50 hours per case.
- **Caps that still apply:** a given `clean_run_seconds` still applies, and so does a whole-run budget.
- **Unmeasured `run`:** it runs with no deadline and keeps the command's exit code. A measured one keeps its scaled
  deadline, and an explicit one wins.
- **Each suite gets its own measured deadline,** and a clean run that leaves processes refuses the plan.
- **The real looping mutant** is still stopped at its scaled deadline.
- **Text:** no fixed cap is stated in the docstring or the methodology, and both say how an unmeasured run is protected.
- **`tests/residual_processes_boundary_test.py`:** its comment no longer cites a bounded_run default; the 300 s there is
  that suite's own bound on a sub-second probe.
