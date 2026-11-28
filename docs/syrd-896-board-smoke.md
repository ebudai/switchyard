# SYRD-896: the deploy smoke proves the board serves this release and reaches its database, at any board size

## What happened

On syrd, two deploys failed and rolled back while ocstab's stress runs held the host at load 35 to 41:

- **Attempt 0364** (d5cdb26c, 2026-11-27 21:26Z) failed with "post-restart check live-build-id failed ... ticket-board
  smoke check failed for http://127.0.0.1:23326/api/board: timed out". The rollback's smoke timed out the same way,
  though the board was serving.
- **Attempt 0367** (1d116d91, 2026-11-28 02:26Z) failed with "the deploy canary ... /api/board: timed out". The board
  was left on f05a1b37.

## Why

The smoke read the whole `/api/board`. On syrd that is 29,151,017 bytes, built before its first byte. Each attempt had
a fixed 1 s socket timeout (`urlopen(url, timeout=1.0)`). At load ~41 the first byte took 3.8 to 4.0 s, so no attempt
could pass, however long the overall deadline was. The live build-id check read `/api/board` the same way.

## The fix

**What the smoke reads.** The smoke proves the board is serving this release and can reach its database, without
rendering the board:

1. `/api/build`, the build alone. Root already reads it for the live build (SYRD-777).
2. `/api/workflow`, a small read that opens a database connection (`TicketBoardApp.workflow_document`). It must answer
   200 with a JSON object.

`BOARD_SMOKE_PATH` (default `/api/build`) and `BOARD_SMOKE_DATABASE_PATH` (default `/api/workflow`) name the two
reads. The deploy, canary, post-restart and rollback checks all use them, through `smoke_check_url`. The live
build-id check reads `/api/build`.

**No fixed per-request cap.** Every board probe in `ticket-board-service-health.sh` lets each request take the time
left before its overall deadline (`BOARD_SMOKE_TIMEOUT_SECONDS`, `BOARD_CANARY_TIMEOUT_SECONDS`). That covers the smoke,
the build id, the commit cache and the write-token check. A busy host answers late, not wrong.

**Other callers that smoked `/api/board`.** Each now reads the build:

- `ticket-board-socket-smoke`, the post-deploy socket verification. It used a fixed 1 s socket timeout. It now reads
  `/api/build`, waiting the time left. The orphaned claim waits at least a second, or the time left.
- `board_supervisor_migration.default_probe` read `/api/board` with a 2 s cap. It now reads `/api/build` and
  `/api/workflow`, each with the time left.
- `packet_completion` and the operator packet's `curl`.

**Not changed.** These read the board for its content, not to smoke it:

- `worker_pool_command`, for its tickets;
- `workflow_adoption`, for its columns;
- `ticket-board-verify-resume`, for a ticket count;
- the pane idle hook.

`release_smoke` never reads `/api/board`, and neither does `switchyard status`.

## Pinned by

`tests/board_smoke_test.py`, using the real health script, socket smoke and supervisor probe:

- A board whose `/api/board` is slow and 30 MB passes, and `/api/board` is never asked.
- A `/api/build` that answers after 2.5 s passes, past the old 1 s cap.
- A board whose database read fails does not pass.
- The build-id check reads `/api/build` alone.
- The socket smoke passes on an answer after 2.5 s.
- The supervisor probe needs the database read.
- The canary passes the database read.
- A real board server passes against a live database, and the same server with its database unreachable answers
  `/api/build` yet fails the smoke.
