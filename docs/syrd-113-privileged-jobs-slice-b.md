# SYRD-113 slice B: the board service records root's privileged jobs on their tickets

Slice A made each privileged run a job in root's rollout journal: the action
and its values, the originating ticket, the requesting control role, the
release, and bounded, redacted, timestamped output. Nothing brought a job to
its board, so a result still had to be read off a pane and copied by hand.

## Decided before building

The Director's decision, as corrected on the ticket:
- **The reader is the board service.** That is `boardsvc`, with its own
  database role `ticket_board_service` and `Group=` the tenant group. It is
  never the notify listener: the listener is a user unit running as the
  tenant account, the same uid as every role pane, and that uid is
  peer-mapped to the listener's database role. Any role process could have
  called a listener ingest function.
- **No new credential.** A job is believed because root wrote it, and only as
  root wrote it.
- **Exactly once, by job id.** Root assigns each attempt id once. A finished
  job never changes, so a restart, an outage or a repeated pass changes
  nothing.

## The change

**The table and the one write** (migration
`pgu991_syrd113_privileged_jobs.sql`, with the same text in `schema.sql`;
grants in `rbac.sql`):
- `ticket_board.privileged_jobs` is keyed by the attempt id. It holds:
  - the ticket and the action, with its id and values;
  - the requester and the release commit;
  - the state (`running`, `succeeded`, `failed` or `timed_out`) and the exit
    status;
  - start and finish times, and root's record directory;
  - the log checksums and the result checksum;
  - the capture accounting and the redacted last lines.
- `ticket_board.record_privileged_job(job)` is the only write:
  - **Who:** only the service's database role, and only in a session that has
    set the reconciler's own `ticket_board.privileged_job_reconciler`. No
    request path sets that, so a role request (through HTTP or the socket),
    the listener and the tenant report token all reach the database by other
    functions. It is executable only by `ticket_board_service`.
  - **Running:** a running job is recorded as running, and nothing is said.
  - **Finished:** a job first seen finished, or finishing after it was seen
    running, is recorded once. That first recording is the only one that:
    - appends the ticket's event, a comment by `privileged-job`, marked urgent
      for a failure or a timeout. It gives the action and its outcome, who
      asked and with what, root's record directory, the stdout, stderr and
      result checksums, and the redacted last lines;
    - wakes the next owner: the ticket's owner on success, and on a failure or
      a timeout the controller as well, each once (keyed
      `privileged-job:<job>:<role>`). Only roles with a pane are woken, since a
      notice to one without is requeued forever (SYRD-549).
  - **Finished jobs never change.** Neither a different ending nor a return to
    running moves one.
  - **No authority.** It signs no gate, edits no field and moves no ticket.
    Roles can read the table, as they can every board table, and nobody but
    this function writes it.
- `ticket_board.ticket_privileged_jobs(ticket)` lists a ticket's jobs for its
  JSON and its page. `ticket_board.finished_privileged_jobs()` lists the
  finished job ids, so a pass doesn't re-read them.

**The reader** (`scripts/ticket_board/privileged_jobs.py`). It believes a
record only as root wrote it:
- **No links followed.** The journal root is reached from `/` one component
  at a time. Its ancestors must be root's, and not writable by others unless
  sticky.
- **Every directory and file:** opened without following a link, and refused
  unless it is root's, writable by nobody else, and in root's group or this
  board's own (the service's) group.
- **The result:** within 1 MiB, of the journal's schema, naming this project
  and the attempt its directory is, and a ticket of this board's prefix.
- **A finished record:** each log within its stream's bound, hashing to what
  the result recorded, and the result hashing to what root's index chained
  at finish. A record copied, edited, linked, misplaced or swollen is refused,
  and the refusal says why.

`reconcile` reads everything not yet finished and records it.
`start_reconciler` runs it in the board service on start and every
`TICKET_BOARD_PRIVILEGED_JOB_INTERVAL` seconds (default 30), logging what it
records and each refusal once. `cli.run_server` starts it. The listener
doesn't.

**The ticket.** The ticket JSON gains `privileged_jobs`, read by the board's
own read query (`ticket_read_query.py`, through `extension_operations`). The
ticket page shows one line per job: its state, exit, requester, record and
checksum. The two files over the review ceiling, `server.py` and `app.py`, are
untouched.

**The states.**
- **Present:** `running`, `succeeded`, `failed` and `timed_out`, as root
  records them.
- **`pending_authorization` doesn't arise.** The front door runs an action
  only when polkit answers "authorized now", asked without interaction
  (`polkit_preflight.may_proceed`). An action that would ask a human is
  refused before privilege is requested. That includes the one project action
  that takes a ticket and is `auth_admin`, `select-shared-release`. So no job
  ever waits for authorization. If the front door ever learns to wait for a
  human, that state belongs here, recorded the same way.
- **`rolled_back`:** for slice C, which needs `deploy-release` to report a
  rollback in its exit status.

## Tests

`tests/privileged_job_reconcile_test.py`, 25 checks.

**The setup:**
- **The board:** built the production way (companion roles, `schema.sql`, the
  real migrate runner, `rbac.sql`, a declared workflow).
- **The journal:** written by slice A's own `Attempt` and streaming runner,
  on real child processes.
- **The owner:** the test can't create root-owned files, so the reader is
  given the test's uid as the owner it believes. Its default, root, is also
  exercised.

**Main, from a git archive in a child process:** nothing reads the journal,
and a ticket carries no jobs.

**This tree:**
- **Recording:** one pass records three finished jobs (success, exit 5,
  timeout) and one still running, each with its state, exit and requester.
  Each finished one gets one event, urgent for a failure or timeout; the
  running one gets none.
- **The event:** it names the action, outcome, requester and values, root's
  record, all three checksums (equal to the ticket JSON's), and the redacted
  last lines. No secret reaches the board.
- **Waking:** a success wakes only the owner; a failure or timeout wakes the
  owner and the Director, once each.
- **Repeating:** the next pass re-reads only the running job and changes
  nothing. When that job finishes, it is completed once, with one event and
  one wake.
- **Restart:** a restarted service, after every notice was delivered,
  repeats nothing.
- **Thread and service:**
  - the reconciler thread records a job root finished while it ran;
  - the real board process, started as its unit starts it, runs the
    reconciler with its own default and refuses this non-root journal.
- **The database's own rule:** a finished job is recorded once. A different
  ending and a return to running are both `unchanged`.
- **Forgeries and damage**, each refused for its own reason and recorded
  nowhere:
  - a group-writable result, or a world-writable directory;
  - a linked log, or a linked attempt directory;
  - an edited log, or a result copied under another attempt id (not in
    root's index);
  - another board's ticket, or another project's record;
  - a log over its bound;
  - a record in another group;
  - with the reader's default owner, the whole non-root journal.
- **Who may write:** a service session without the reconciler's setting, the
  listener's database role, and the API as the Director or as App are each
  refused, and nothing is recorded.
- **Parity:** `schema.sql` and `rbac.sql` install what the migration does.
  Only the service can run the three functions, and nobody can write the
  table.

## Evidence

- **Mutation:** sixteen mutants, run through `tests/bounded_run.py mutate`,
  all killed. Each kill was read on its failing assertion:
  - **In the SQL:**
    - any service session may record a job;
    - a finished job is recorded again;
    - a failure doesn't wake the controller;
    - the owner isn't woken.
  - **In the reader:**
    - a writable record is believed;
    - any group's record is believed;
    - a linked file is followed;
    - a log's checksum isn't checked;
    - the result isn't checked against root's index;
    - another board's ticket, or another project's record, is believed;
    - the root's ancestors aren't checked;
    - finished jobs are read again.
  - **Elsewhere:**
    - the listener is granted the write;
    - the ticket's JSON carries no jobs;
    - the service doesn't start the reconciler.

  Two notes:
  - **Startup mutant:** it was inconclusive at first. The test read the
    service's output with no deadline, so with no reconciler it waited
    forever. The read is now bounded, and the mutant is caught on its
    assertion.
  - **Index-check mutant:** it is caught by the reader failing on the missing
    index entry it no longer checks for.
- **The sweep:** the 159 suites that touch the read query, the extension
  fields, the board CLI, `schema.sql`, `rbac.sql`, migrations, the ticket
  page, size review, serial queues, reminder snoozes, the helper or the
  journal were run serially on this tree and on main e2571f8, with provider
  stubs that refuse to start.
  - **The same reds:** 22 are red on both trees. They have identical
    normalised output, except where migration listings now name pgu991, and
    where the migration runner shows pgu991 finding its table already created
    by `schema.sql` (`IF NOT EXISTS`, as every migration that mirrors
    `schema.sql` does).
  - **Two suites changed, both updated:**
    - `ticket_board_read_query_boundary_test` pins the read query's SQL by
      hash. As SYRD-537, SYRD-541 and SYRD-568 did for their columns, the
      hashes were re-measured from the new SQL and the test's comment names
      the new column.
    - `operator_person_wait_test` (SYRD-451) upgraded a board by applying the
      new tree's `rbac.sql` before that tree's migrations. A grant naming a
      table that only the migrations create then fails. A deploy runs the
      migrations first, so each tree's `rbac.sql` now runs after its own
      migrations.
    - Both pass: 3 and 15 checks.
- **Main moved during the work:** to d8f36e1 (SYRD-553, host diagnostics).
  That commit touches none of this slice's files. This suite and its child
  run of main pass on d8f36e1. The mutants and the sweep ran on 63fb5e4 and
  e2571f8.
