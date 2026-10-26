# SYRD-684: the board's socket answers runtime assignments again

## What happened

After the 2026-10-26 deploy of `fd06cac8`, `switchyard pie` refused:

```
team-launcher: refusing to launch pie before changing local state: its running board does not provide
project-account process authority (Remote end closed connection without response)
```

**The cause.** SYRD-677 added `report_credentials` to `GET /api/runtime-assignments`. The handler read it as
`self.server.report_binding_key`, which only the HTTP server (`TicketBoardServer`) set. The same handler serves the
board's Unix socket (`TicketBoardUnixServer`). There the read raised `AttributeError`, and the connection closed
with no response.

**Why every launch broke.** A process-authority tenant's launch asks exactly that route over that socket before it
changes anything (`board_authority_preflight.process_authority_board_compatibility`). So:

- attach, start and reload of a running tenant refused;
- a resume from stopped asks the same route (`board_socket_answer`, SYRD-670), took the dropped connection for
  silence, and waited out its bound for a board that "never answered".

**Why no test caught it.** SYRD-677's tests drove the HTTP server, and the launch suites stub the preflight or stand
up their own fake board on the socket.

## The fix

Both servers take the same report inputs:

- `report_follow_up.server_credentials(report_token, report_binding_key)` returns the report secret and the usable
  binding key: the key unless it is unset or equal to the secret (SYRD-677). Both servers set
  `report_token` and `report_binding_key` from it.
- `TicketBoardUnixServer` takes `report_token` and `report_binding_key`, and `cli.py` passes it the same values it
  passes the HTTP server.

So the socket and the HTTP port advertise the same `report_credentials`, and the handler reads no attribute that one
of its servers lacks.

**What the socket still refuses.** Report filing and follow-ups stay HTTP-only: the socket server always has a role
authority, and `report_follow_up.authenticate` refuses any request that has one. Holding the secret does not change
that.

**What is left alone.** The duplicate `started board:` line a running tenant's resume prints after
`already running board:` is cosmetic, and it is pinned by `tenant_suspension_boundary_test`'s golden, recorded from
the baseline launcher. It is a separate change.

## Recovery

After the release with this fix is installed, `sudo switchyard upgrade <tenant>` restarts the board on it. Until
then, no process-authority tenant on `fd06cac8` or later can be launched. Running panes are not affected.

## Tests

`tests/board_socket_runtime_assignments_test.py` runs the real board process (`scripts/ticket-board.py`, with its
unit's command line and environment) on a production-built database, with a real Unix socket and HTTP port. It
covers four boards: one with a binding key, one with only the report secret, one with neither, and one whose binding
key is its report secret. Main runs first, from a git archive.

**Main:**

- a stopped tenant's resume waits out its bound;
- attach, attach-or-start and reload each refuse with "Remote end closed connection without response";
- the socket drops the request, and the board logs the `AttributeError`.

**This tree, for each board:**

- the resume starts the board (a stand-in `systemctl start` runs the real process) and finds it answering;
- the launch, in every mode, gets past its authority preflight and changes nothing before it;
- both probes are answered;
- the socket answers HTTP 200 JSON with the same `report_credentials`, project and authority mode as the HTTP port;
- the board logs no exception;
- the socket still refuses `file_report` with the board's credential;
- an AST check that every `self.server.<name>` the handler reads directly is set by both servers;
- no secret or key appears in any output.
