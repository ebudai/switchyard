# A test suite that could stop the machine's tmux server

Running the viewer geometry suite from App's live role pane disconnected every
project presentation pane. Twice. The User had to open a new terminal and run
`switchyard syrd` to rebuild the six-pane presentation each time.

## Why `TMUX_TMPDIR` is not isolation

The suite set `TMUX_TMPDIR` to a private directory and believed that was enough.
It is not. Measured from inside a live role pane, with `TMUX` set:

```
bare tmux:                  /tmp/tmux-1006/default      <- the live server
TMUX_TMPDIR=<private> tmux: /tmp/tmux-1006/default      <- ignored entirely
tmux -L <name>:             /tmp/tmux-1006/<name>       <- overrides $TMUX
tmux -S <path>:             <path>                      <- overrides $TMUX
```

A tmux command with no `-L`/`-S` uses the socket named by `TMUX` and never
consults `TMUX_TMPDIR`. So the fixture built its sessions on the caller's own
server and ended with an unqualified `kill-server` against it.

Audit had passed the same suite, because it ran outside tmux -- where `TMUX` is
unset, `TMUX_TMPDIR` does work, and the defect is invisible. That is the shape
of this whole ticket: the condition the bug needs is the one a clean test
environment removes.

## The guard already existed, and was red

`tests/tmux_test_invocation_lint_test.py` already encoded both rules -- tests
must use an explicit socket, and `kill-server` is forbidden -- and its
repository-wide assertion was failing on main. Its own docstring said as much.
Six suites violated it.

A red guard is not a guard. Two changes made it enforceable:

- **`-S` counts as isolation, alongside `-L`.** Both pin which server is meant.
- **`kill-server` is forbidden *unqualified*, not absolutely.** The blanket ban
  was why five suites carried a violation nobody could clear: the correct
  teardown for a private server *is* `kill-server`, so the rule forbade the
  right answer. This ticket's own wording is the better rule -- never an
  unqualified destructive command against an unverified server.

## One implementation, not six

`tests/tmux_socket_cleanup.py` now owns the guarantee:

- `private_tmux_socket` -- the socket path, **derived to be exactly where
  `TMUX_TMPDIR` would put it**;
- `private_tmux_env` -- the environment with `TMUX` and `TMUX_PANE` dropped and
  the directory set;
- `private_tmux_args` -- every command carrying `-S`;
- `assert_private_tmux_socket` -- refusing before anything is created, not after;
- `kill_private_tmux_server` -- refusing a socket outside its own directory, and
  stopping nothing when nothing is running.

Containment is by path components on resolved paths: `/tmp/fixture.ab` is not
inside `/tmp/fixture.a`, however the two read as strings.

That the socket path matches `TMUX_TMPDIR`'s own derivation is load-bearing
rather than tidy. The fixture pins the socket with `-S`, but the code under test
re-invokes tmux for itself -- a pane's attach command, a launcher's own calls --
carrying no socket flag and finding the server through the directory. A helper
that picked its own name gave the two different servers, and the symptom was
`there is nothing to attach to` rather than anything about sockets. It broke a
suite that had been passing, and was caught by running that suite against
baseline rather than assuming its failure was environmental like the others
around it.

## Proving it, from inside a pane

`tests/tmux_isolation_sentinel_test.py` runs each workload **inside a real pane
of a sentinel server** that stands in for the live one. `TMUX` is therefore
genuinely inherited, which is the condition the defect needs and the one that
running outside tmux removes.

The sentinel's sessions are compared by name **and creation timestamp**: a
server killed and rebuilt between two looks presents the same names.

Four cases: a successful run, a failing run so the cleanup path is exercised, a
teardown aimed at a server it does not own, and the viewer suite from the
incident run inside a pane in full.

With the isolation reverted to its pre-fix form the suite fails in under a
second:

> the workload stopped the tmux server it was running on: the sentinel at
> .../tmux-1006/default is gone. That is the SYRD-219 defect -- an unqualified
> tmux command reaching the caller's own server.

That took a second pass. The first version detected the defect only by timing
out after 300 seconds: killing the sentinel also kills the pane running the
workload, so the marker the wait depends on is never written. A hang is a weak
signal for a defect this specific, so the wait now watches the sentinel's
liveness too and says what happened.

A smaller thing found the same way: the pane wrapper collected its exit status
with `; printf %s $?`, which silently wrote nothing, because this account's
shell is fish and `$?` is not how fish spells it. The status now comes from an
explicit `/bin/sh` wrapper rather than from whatever `default-shell` happens to
be.

## Scope

The sentinel suite drives one full suite -- the one from the incident -- because
running every tmux suite inside a pane would cost far more than it proves. The
rest of the tree is held by the source lint, which is now green and therefore
actually load-bearing.
