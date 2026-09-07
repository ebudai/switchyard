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
