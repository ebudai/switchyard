# Running the tests

```
scripts/ticket-board-test-suite
```

That is the documented invocation, and it is the one to use for anything
broader than a single module. It runs each suite in its own process, strips the
live board and pane environment so a test cannot reach the running tenant, and
puts every suite on a bus that reaches no systemd user manager.

A single module can be run directly:

```
python3 tests/<name>_test.py
```

Direct runs get the same bus isolation, because the shared test modules apply it
when they are imported. A module that reaches tmux without importing one of them
must call it itself; `tests/tmux_user_bus_isolation_test.py` enforces that
across the repository.

## A cold sweep

```
python3 tests/sweep.py [--out DIR] [--jobs N] <suite>...
```

To compare a candidate with its base, run the suites a change could reach from
nothing of your pane: each in a bwrap sandbox, with no network, a private `/tmp`
and `/run`, a disposable HOME and `env -i`. Some suites need what only the host
gives: user namespaces of their own, ids outside a sandbox's map, root-owned
host files, the system bus, a browser, or KVM and the network (`vm_kit_test`,
which boots a real VM). `tests/sweep_requirements.py` declares
those, with the reason, and the sweep runs them on the host instead, still cold.
It gives a suite that calls `live_boundary.isolate()` first no HOME, so the
suite builds its own disposable root, and the live pane's two store names, so
its sentinel tolerates that pane's own hooks. Run the sweep from the pane with
`TICKET_BOARD_PANE_SESSION_DIR` and `TICKET_BOARD_PANE_STATE_DIR` in its
environment: under a bare `env -i` the sentinel cannot tell that pane's own
writes from the suite's, and fails the run as LIVE DRIFT.

"First" is read from the suite's syntax, and closed (`live_boundary.isolates_first`,
SYRD-820 and SYRD-824). Before a module-level `live_boundary.isolate()` call a suite
may only import the standard library and `live_boundary` itself (not renamed),
assign fresh names from literals and a short list of calls (`str`, `int`, `len`,
`Path`, a path's `resolve`/`joinpath`, `sys.path.insert`/`append`, `os.environ`
and `os.getenv` reads, `os.path` joins), and wrap those in `if` or `for`.
Anything else counts as running first, so the suite is not an isolating one:
any other call (`exec`, `runpy`, `importlib`'s spec loading included), an
import beyond the standard library, a `def` or `class`, and a rebinding of an
import or of `live_boundary`. A suite `ISOLATES_IN_MAIN` declares, with its
reason, counts too. A mention in a comment or a function body does not.

Every other suite gets a disposable HOME, and the temporary directory is one
other accounts can traverse. A suite
measured as load-sensitive is retried once, alone, and reported as passing on
retry. The sweep never runs as root (SYRD-815).

Declare a suite there only for a reason the sandbox cannot give, measured.
Twenty suites were red in every sandboxed sweep for these reasons alone, and so
guarded nothing (SYRD-806).

## A release in a disposable VM

`tests/vm_kit/` boots a throwaway KVM VM and uses a candidate the way production
does: the real `./install`, a tenant from `switchyard new` (offline, on a stand-in
Codex that runs only in the kit's VMs), the real board and listener. Reviewers run
it for a change to served board or listener code, or to install and deploy tooling:

```
tests/vm_kit/vm_kit.py check --commit <candidate> --keep-logs <dir>
```

Every VM is powered off at the end and proved gone. `tests/vm_kit/vm_kit.py --help`
lists the commands for a VM of your own; [the review VM](syrd-857-test-vm.md) is
the procedure and why it is safe.

## Why the bus matters

tmux built with systemd support asks the **user** systemd manager to place every
pane in its own transient scope, so systemd-oomd can isolate it:

```
org.freedesktop.systemd1.Manager.StartTransientUnit
tmux-spawn-<uuid>.scope        "Started tmux child pane"
```

That is correct in production and nothing here changes it. It is not correct in
a test process, which inherits the tenant's `DBUS_SESSION_BUS_ADDRESS` and
`XDG_RUNTIME_DIR` and therefore aims those calls at the tenant's own manager --
however temporary the test's tmux session names and sockets are. An isolated
socket is not isolation: the socket decides which tmux *server* the panes belong
to, and the bus decides which *manager* is asked to account for them.

SYRD-54 found the tenant's user manager wedged in a userspace spin, 94-97% of a
core, after recording 1,255 `Started tmux child pane` scopes -- 161 of them in
the nine minutes of two sequential sweeps of 101 modules. It stopped journaling
after the last one and never serviced `systemctl --user` again.

## How the isolation works

`tests/tmux_bus_isolation.py` fails closed rather than unsetting. libdbus
autolaunches a bus when `DBUS_SESSION_BUS_ADDRESS` is absent, and
`sd_bus_open_user` falls back to `$XDG_RUNTIME_DIR/bus`, so an unset variable is
not an absent bus. Both are pointed inside a private directory, at a socket that
is deliberately never created. tmux reports `StartTransientUnit call failed` at
its own debug level and starts the pane anyway.

The directory itself is created, because tmux and others fall back to
`XDG_RUNTIME_DIR` for their own sockets and one that does not exist produces
failures unrelated to what is being tested.

A test that genuinely needs a user manager must bring its own -- a fake, or a
disposable manager it starts and stops itself. `replaced_bus_address()` returns
the address the isolation replaced, for a test that has to find it.

## Version boundary

Measured on this host: **tmux 3.7c**, linked against `libsystemd.so.0`, makes
exactly one user-bus call per pane -- four panes, four connections to a
stand-in bus. The scope name is `tmux-spawn-<uuid>.scope`.

A tmux built without systemd support makes none. The isolation then changes
nothing at all, because an unreachable bus address a process never opens costs
it nothing, so there is no version test and nothing to keep in step. The
regression measures both directions and says so rather than assuming: if the
count with a reachable bus is zero, it reports reduced coverage instead of
failing.

## Fixture processes that cannot outlive the test

A suite that starts processes of its own (a fake tmux server, a provider, the commands a provider runs, a daemon in a
session of its own) owns them with `tests/fixture_processes.py` (SYRD-681). A session of its own is often the point
of such a fixture, and it is also why a timeout's TERM to the test's process group never reaches it. On 2026-10-25 a
`while :; do :; done # stale-616` from SYRD-616's suite spun one core for 25 hours, reparented into the user
session.

```python
with tempfile.TemporaryDirectory() as own:
    with fixture_processes.owned(own) as run:
        ...                                    # start processes with run.popen(...), fixture scripts with
                                               # fixture_processes.DIE_WITH_PARENT, TMUX_SERVER, PROVIDER
    left = run.sentinel()
check(left == [], f"every fixture process is gone: {left}")
```

A run sets up four boundaries, each working by process identity (pid AND start time, signalled through a pidfd),
never by command name:
- **Signals:** while a run is owned, SIGTERM, SIGINT and SIGHUP raise SystemExit, so a timeout, an interrupt or a
  closed session runs every `finally`.
- **The parent's death:** each fixture process gets `PR_SET_PDEATHSIG` against the process that started it, so the
  kernel ends the chain even when the test is SIGKILLed.
- **The run's mark:** every process the run starts inherits a per-run `SWITCHYARD_TEST_FIXTURE_RUN` id. At the end,
  each marked or recorded identity is sent TERM, then KILL.
- **A guardian:** a process in a session of its own waits for the test to end, then stops whatever still carries
  the mark. It is what cleans up after a SIGKILLed test, including a command's own children, which no parent-death
  signal reaches.

`tests/fixture_processes_test.py` ends a harness every way a test can end, and checks each fixture process is gone
while a decoy with the same command line survives. The ways are: success, a failed check, TERM, INT, HUP, SIGKILL,
and SIGKILL with the guardian killed too.
