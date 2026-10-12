# SYRD-113 slice A: a privileged action is a job, and its output is recorded

SYRD-113 asks for every catalogued privileged action to be a durable job tied
to its originating ticket, with its output captured and returned to that
ticket. The Director approved three separately audited candidates on the
ticket:
- **A, this one:** job identity and output capture in the root helper and its
  journal.
- **B:** the board ingests root-owned job records, writes the ticket event and
  notifies the owner.
- **C:** install, repair and retention, plus the disposable success and
  failure acceptance.

## What SYRD-112 left

The root helper, `privileged_helper.py`, opened a rollout-journal attempt for
every run and then ran the operation with `subprocess.run`:
- **Nothing was captured.** The output went to the pane only, and the
  attempt's `stdout.log` and `stderr.log` stayed empty.
- **Nothing named the job.** The record named no ticket, no action and no
  requester. The helper resolved the requesting control role and discarded it.
- **A timeout was an ordinary failure.** Only the direct child was killed, so
  anything it had started kept running.
- **The two deadlines raced.** The front door and the helper both used
  900 seconds.

## The change

**The originating ticket.**
- `ticket=PREFIX-N` is an optional job value on every catalogued action that
  names a project (`privileged_actions.JOB_ARGUMENTS`). It is validated to the
  board's own id form; anything else, and any ticket on a projectless action,
  is refused. A projectless action has no board to report to.
- The helper finds the ticket on the project's own board before anything
  runs: the board re-derived from root's registration, as for the caller
  check. A ticket the board doesn't hold, or a board that can't be reached, is
  refused with "nothing was run", and no attempt is opened.
- The ticket never reaches the operation. Commands are built from
  `operation_values`, the validated values minus the job's own.

**The job record.** `Attempt.describe_job`, called before the attempt opens,
records `job` in `result.json` and in the journal's start entry:
- the action and its polkit action id;
- the operation values;
- the ticket;
- the requesting control role;
- the release commit.

Other journal writers (record-rollout, pane rebind, boundary repair, workflow
adoption) describe no job, and their records are unchanged.

**Capture.** The installed runner, `_run_command`:
- **Streams.** It starts the operation in its own process group and reads its
  output in 64 KiB chunks, holding nothing beyond a chunk.
- **Keeps the pane useful.** Each chunk is written to the pane as it arrives,
  exactly as the operation produced it.
- **Records.** It hands each chunk, decoded, to the attempt. There, each line
  is redacted (the existing `Redactor`), timestamped, and counted against the
  stream's limit before anything is written:
  - **Per stream:** 4 MiB (`STREAM_LIMIT_BYTES`). Past it, lines are counted
    and not written, and at close one marker says how many bytes and lines
    were dropped after how many were recorded.
  - **Per line:** 64 KiB (`LINE_LIMIT_CHARS`), the most of one line held while
    its newline is awaited. Past it the rest of the line is counted and not
    held. The last 256 characters of what was held are dropped too
    (`CUT_LINE_MARGIN`), since a secret straddling the cut can leave a head too
    short for any pattern to recognise. The line ends with a marker giving the
    count.
  - **In the result:** `result.json`'s `capture` records each stream's limit,
    recorded, dropped and total bytes, dropped lines, and whether it was
    truncated. The existing sha256 artifacts cover the files as written.

**Deadlines.**
- The helper owns the shorter deadline: 840 seconds. At it, it stops the whole
  process group, SIGTERM then SIGKILL after a 10-second grace each. It records
  the attempt as **`timed_out`**, with what the operation said first, and
  returns 124.
- The front door's deadline is derived from the helper's: 840 + 2 × 10 + a
  40-second margin = 900. So the front door receives and shows the helper's
  durable result, rather than racing it.

**Reading it back.** `switchyard rollout-log` shows each job's action, ticket
and requester under its attempt.

**States in this slice:** `running` (opened), `succeeded`, `failed` and
`timed_out`. An attempt that started and never finished still reads as
`interrupted`.

**Left to later slices:**
- `pending_authorization` belongs to the board-side job (B), because the front
  door that knows it can't write root's journal.
- `rolled_back` needs `deploy-release` to say so in its exit status. Today a
  rollback is visible only in its output. That's for C.
- The journal's modes (root:root 755/0444, so readable by every local account)
  are tightened to the tenant group, with the existing records repaired, in C,
  which owns install, repair and retention.

## Tests

`tests/privileged_job_capture_test.py`, 21 checks. It uses:
- the real catalogue;
- the real `rollout_journal.Attempt`, in a scratch journal root;
- the real streaming runner, on real child processes that print, fail, flood,
  write an unbounded line, straddle a secret across the cut, and hang with a
  forked grandchild;
- the ticket check against a real board over HTTP;
- the installed entry point, `main()`, with only the host's own facts stood
  in: the installation check, the pane identity, the control-role check and
  the launcher.

**Main, from a git archive in a child process:**
- a run can't name a ticket;
- every record has empty logs and no job;
- a timeout is recorded as `failed`, with the operation's own child left
  running;
- both deadlines are 900 seconds.

**This tree:**
- **The ticket:** validated, refused in any other form and on projectless
  actions, found on the board or refused (404, unreachable). Through `main()`,
  a missing ticket is refused before any attempt is opened.
- **The job:** recorded exactly, in the result and the start entry, with the
  requester the control check resolved.
- **Success and failure (exit 5; exit 3 now means a rolled-back `deploy-release`, slice C):**
  - every line is recorded, timestamped and redacted;
  - no secret is anywhere in the record;
  - the checksums are the files';
  - the pane still shows the raw output.
- **A 6 MiB flood:** stops at 4 MiB with one marker that matches the recorded
  counts, and stderr is unaffected.
- **A 3 MiB line:** cut at its bound with the count, the secret past the cut
  absent, and the next line recorded.
- **A secret straddling the bound:** none of it recorded.
- **A hang:** stopped at the deadline, both processes gone, recorded
  `timed_out` with its first output.
- **Deadlines:** the helper's, with its stop, ends inside the front door's.
- **`rollout-log`:** names the job.

## Evidence

- **Mutation:** thirteen mutants, run through `tests/bounded_run.py mutate`,
  all killed. Each kill was read on its failing assertion:
  - a projectless action taking a ticket;
  - the ticket reaching the operation;
  - the board check skipped;
  - the requester not recorded;
  - the installed runner not streaming;
  - output not redacted;
  - no stream limit;
  - no line bound;
  - a cut line keeping its end;
  - no timestamps;
  - the timeout stopping only the direct child;
  - a timeout recorded as a failure;
  - the front door's deadline equal to the helper's.

  The cut-margin mutant first survived, because the assertion computed its
  expectation from the module's own constants. The expectation is now pinned
  literally, and the straddling case was added.
- **Neighbour suites:** `privileged_helper_boundary_test` pinned a timeout as
  `failed`. It now expects `timed_out`, the state the ticket asks for. The fake
  runners and attempts the existing suites inject are untouched: the job
  description reaches only an attempt that has `describe_job`, and the
  record's writer only a runner that says it streams.
- **The sweep:** the 58 suites that touch the catalogue, the helper, the
  front door, the operations, the journal, `rollout-log`, record-rollout, or
  the journal's other writers were run serially on this tree and on main
  4c44195, with provider stubs that refuse to start.
  - Exit codes are identical, and the 5 red on both trees have identical
    normalised output.
  - `privileged_front_door_test` is red on both and stops at its first
    failure, so each of its tests was also run on its own on both trees: 35
    pass and 1 fails on each (`test_the_real_cli_answers_instead_of_hanging`),
    the same test.
- **Main moved during the work:** to 9ee705a (SYRD-547, the model probe).
  That commit touches none of the privileged or journal modules. This suite
  and its child run of main pass on 9ee705a. The mutants and the sweep ran on
  3932daa and 4c44195.
