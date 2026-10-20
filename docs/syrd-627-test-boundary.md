# SYRD-627: a test run never changes a live account's home or runtime directory

## The incident

At 12:48:50 EDT on 2026-10-18, four directories changed from UID/GID 1006
(`switchyard-agent`) to subordinate UID/GID 494715:
`/home/switchyard-agent`, `.local`, `.local/state` and
`.local/state/syrd-ticket-board/pane-sessions`.

Test-shaped records also appeared in the Audit pane's session store:
`roles/ops/p-ops_0.0.json`, holding `{"target": "p-ops:0.0", "session_id":
"S1"}`, and its `.superseded`. Every Codex pane hook then failed on the
unreadable store. The Director repaired the exact UID in one polkit
operation.

### What ran

Audit was reviewing SYRD-612's candidate `ecb70f88` from its own pane, and
published that review at 16:51:23 UTC. The candidate's new
`tests/role_cutover_writes_test.py` runs its privileged half as namespace root
(`unshare --user --map-auto --map-root-user`). Its stand-in owner is UID 1500,
which this account's subordinate range (493216:65536) maps to 493216 + 1500 - 1
= 494715. The case that matched the forensics is its `Switch` fixture.

### Why it reached the live tree

Two things combined.

1. **Production: a pane's stores were handed to other accounts.** A project
   configuration with no `session_dir` gets
   `session_paths.default_session_dir_for_user(run_as_user)`. That function
   returned the ambient `TICKET_BOARD_PANE_SESSION_DIR` whatever account it was
   asked about. `default_pane_state_dir_for_user` did the same with
   `TICKET_BOARD_PANE_STATE_DIR`. In a role pane those variables name the pane's
   own live store; in the Audit pane, `.../pane-sessions/roles/audit`. So
   project `p`, run by owner 1500, resolved its sessions to Audit's store, and
   `role_session_dir` appended `roles/ops`. `account_session_dir` already
   refused to do this, and its docstring gives the reason.
2. **The fixture: a chown walk with no floor.** `Host.make` lchowned
   `[path, *path.parents]` to the owner wherever an entry was owned by namespace
   root (host 1006), stopping only at its own `homes` directory. A path outside
   the fixture never reaches that directory, so the walk took every ancestor
   owned by 1006, up to the first root-owned one (`/home`).

The suite also ran with the pane's HOME, XDG directories and pane variables,
which is the condition everything above needed.

### Reproduced

The candidate's suite was run as the Audit pane would run it, with a decoy home
and decoy pane variables. It chowned every ancestor of the decoy store it could,
up to root-owned `/tmp`, and created the decoy's `roles/audit/roles/...` tree.
The live tree stayed unchanged.

## The fix

### Production

`default_session_dir_for_user` and `default_pane_state_dir_for_user` honour the
ambient pane stores only for the caller's own account, which is what those
variables name. An account this host cannot resolve keeps the ambient store, so
legacy configurations behave as before. Any other account resolves its own
home's store, as `account_session_dir` already does.

### The test boundary: `tests/live_boundary.py`

A suite that maps root in a user namespace calls `live_boundary.isolate()`
before importing any production module. All 67 Python suites that do so call
it; the two shell suites re-execute themselves through
`live_boundary.py --run`.

- **Disposable environment.** HOME, TMPDIR and every XDG directory point into a
  fresh root under TMPDIR (under /tmp when TMPDIR is itself live).
  TICKET_BOARD_*, PGU_TICKET_BOARD_*, DIRECTORCTL*, TMUX and TMUX_PANE are
  removed, and TMUX_TMPDIR points into the root too. Every child inherits this,
  namespace children included. TMPDIR behaves like /tmp: the root is
  traversable (0711) and TMPDIR is sticky and open (1777), so a stand-in owner
  a suite runs as another identity can reach its fixture. The home, state and
  runtime leaves stay 0700.
- **Guard.** `isolate()` builds `tests/live_boundary_shim.c` as a private
  shared object and preloads it into every dynamically linked child. It
  refuses any actual operation that resolves under the invoking account's real
  home or `/run/user/<uid>`, after a shell has expanded variables, arrays,
  functions and substitutions:
  - chown, chmod, rename or replace, mkdir, rmdir, unlink, link or symlink,
    utime, truncate;
  - a write-mode `open` or `os.open`;
  - destructive process controls, including broad or foreign-group signals,
    real privilege/service tools, and shared tmux sockets.

  The Python guard remains the first layer for direct subprocess argv, so a
  plainly unsafe command is rejected before exec with status 126 and a stack.
  Children also load it through `tests/boundary_site/sitecustomize.py`, which
  `isolate()` puts on PYTHONPATH. Its refusal reports
  `test boundary: refused` and the real operation; the calling tool or shell
  may translate EACCES or EPERM into its own nonzero status. In the test's own
  checkout, writes are allowed but chowns never are.
- **Inheritance.** The boundary's own variables -- LD_PRELOAD and
  `SWITCHYARD_TEST_LIVE_ROOTS`, `_WRITABLE`, `_DISPOSABLE_ROOT`,
  `_BOUNDARY_LOG`, `_INVOKER_UID` and `_RUN_START` -- are the run's policy, and
  no child can change it. The native layer reads them once, when it loads,
  and every exec gives the child those values, whatever environment it was
  started with. The Python layer does the same for subprocess, os.system,
  os.exec* and os.posix_spawn*, so a suite's `env={"PATH": ...}` child is
  behind the boundary too. An omitted, emptied or replaced value is the run's
  again. A child may only add to the policy: a preload after the run's, another
  live root, a writable root that opens up none of the live roots it
  inherited, a later run start. (Audit found the first native layer let
  `LD_PRELOAD=` or `SWITCHYARD_TEST_LIVE_ROOTS=` switch it off in a
  descendant.)
- **Sentinel.** At isolation it snapshots:
  - the chain (the home, `.local`, `.local/state` and the runtime directory):
    owner, group, mode and inode;
  - every entry of every `*-ticket-board` store under `~/.local/state` and
    under the runtime directory, recursively: owner, group, mode, inode, type,
    size, change time, and for a file its SHA-256.

  At exit, it fails the run with status 97 on any of these:
  - a change to anything that was there;
  - a deletion;
  - an addition, whoever owns it.

  The report names each entry and what changed, the suite, and every namespace
  command it ran.

  The one exception covers a run started from a pane. That pane's own project
  store is rewritten by the hooks of the other live panes while a suite runs.
  There, the records those hooks keep (named for the role whose directory they
  are in) may change contents or be added:
  - `pane-state/<project>-<role>_N.N.json`;
  - `pane-sessions/.../<project>-<role>_N.N.json`, with its `.superseded` and
    `.resume.log` siblings;
  - `<role>.provider-state.json`.

  Even those may not change owner, group or mode, and may not be deleted.
  Nothing else in that store is exempt. A run not started from a pane (a VM
  sweep, `env -i`) has no exemption.

## The second incident: role servers exited during the first comparison

At about 19:05–19:15 UTC the same day, while a sweep of this ticket's suites
was running, every SYRD role server (switchyard-agent, UID 1006) and every MEFP
role server (stellaris-agent, UID 1002) exited. Otto (1005) and SAPI survived.

### What was established

- **The sweep reached the account's default tmux socket.** It started each
  suite under `env -i`, and the first boundary removed TMUX* as well. With no
  TMUX and no TMUX_TMPDIR, a bare `tmux` resolves to `/tmp/tmux-<uid>/default`,
  which is the account's live server. That is a mechanism for SYRD's loss. It
  cannot reach another account's server.
- **The suite then running is cleared.** `runtime_switch_stall_test.py` was
  traced in a sandbox: private /tmp and /run, the home read-only, run as an
  ordinary UID in the sweep's environment, and `strace -f` over execve and every
  signal syscall. Sentinel tmux servers ran for the runner's account and two
  other accounts. Both main's version and the tree that ran at the time:
  - passed;
  - executed no tmux, sudo, pkill, killall, systemctl or loginctl;
  - signalled only PostgreSQL's own processes;
  - left all three sentinel servers and all six sessions alive.
- **A base sweep in a disposable VM lost nothing.** All declared suites from
  main ran as a real user in the sweep's environment. Real tmux servers for
  that user and two other users were checked after every suite. None was lost
  and the home never drifted.
- **The only foreign action on record was a refused sudo.** At 19:05:19 the
  root journal shows a namespace-root `sudo` attempt (USER=testing-agent). It
  came from `resume_provision_test.py`, whose privileged child fell through to
  the real `sudo`. sudo failed closed.
- **The root journal shows nothing else:** no OOM kill, no user-manager restart,
  and no tmux, tenant-control or polkit event.

What stopped the MEFP servers is not explained by anything the sweep did, and
nothing visible from this account explains it.

### Host leftovers found

`/tmp/tmux-0`, owned by 1006 and created at 09:04, and `/tmp/tmux-2`, owned by
493217 (namespace UID 2 of this account), created at 09:05. A namespace-root
process's bare tmux resolves to `/tmp/tmux-<its uid>` and creates the directory
on the host. Real root's tmux then refuses a `/tmp/tmux-0` that root does not
own. These were left in place for the Director.

## The boundary's process rules

Direct subprocess argv is checked before exec in every Python process of an
isolated run. The native interposer checks the operations that dynamically
linked children actually attempt, including shell and namespace children.

- **tmux never reaches a shared default directory.** `isolate()` always gives the
  run a disposable TMUX_TMPDIR, and drops a TMUX that names the account's
  directory. The guard computes the socket each tmux command would reach, by
  tmux's own rules: `-S`, then TMUX unless `-L` names another socket, then `-L`
  or `default` under TMUX_TMPDIR, or `/tmp`. Uid 0 counts inside a namespace.
  The guard refuses any socket in a `/tmp/tmux-<uid>` directory, whatever the
  uid.
- **No real privileged or service tool runs:** sudo, pkexec, su, doas, runuser,
  systemctl, loginctl, machinectl, busctl, systemd-run.
  - "Real" means the program resolves into the system (`/usr`, `/bin`, `/sbin`,
    `/lib*`, `/opt`, `/etc`, `/snap`).
  - A stand-in a suite put on PATH runs, and so does a function its script
    defines (`sudo() { "$@"; }`). What a stand-in sudo is given is checked in
    turn.
  - The service tools may still ask (`is-active`, `status`, `show`, `list-*`,
    ...), but never change state.
- **No real kill by name:** pkill, killall.
- **No signal to every process, nor to a process group that is not the run's.**
  This covers `kill -- -1`, `os.kill(-1, ...)`, and `kill -<pgid>`,
  `os.kill(-pgid, ...)` or `os.killpg` for a foreign group. A group is the run's
  when its leader descends from the caller or started after `isolate()`.
  Targeted single-process signals and signal-0 probes are unaffected.

A direct Python refusal fails before exec with status 126 and its reason on
stderr. A native refusal returns EACCES or EPERM from the libc operation, so
the program or shell chooses the resulting nonzero status; the boundary's
diagnostic is the stable evidence. A refused Python signal raises
`PermissionError`, matching `kill` on EPERM.

The Python guard reads direct argv past wrappers (`env`, `timeout`, `nice`,
`setsid`, and similar), their options, and any stand-in sudo. It deliberately
does not parse `sh -c`, `shell=True`, `os.system`, script files, heredocs,
functions, arrays, substitutions, or `eval`. The invoked shell interprets
those normally, and the native layer checks the resulting `open`, `mkdir`,
`rename`, `unlink`, ownership, mode, link, timestamp, truncation, exec, signal
and tmux-socket operations. Thus an argument that merely contains a tool name
is harmless, while a command assembled as `command=(touch "$path");
"${command[@]}"` is refused when `touch` opens the live path.

The native layer cannot mediate statically linked programs, setuid programs
that discard `LD_PRELOAD`, or code issuing raw syscalls without libc. Direct
argv policy covers the known privileged and process-control tools, and the
exit sentinel remains the backstop for any live-state change outside the
interposer.

The sentinel also records each tmux server listening in the account's
directory, and the server the run was started from. A server that stops or
restarts fails the run with status 97. Live servers are read from the kernel's
listening-socket table, never by connecting to them. tmux leaves its socket
file behind when the server exits, so the file proves nothing. The check
allows a short settle, because `kill-server` returns before the server exits.

## The safe test contract

- **Isolate first.** A suite that runs code as namespace root, or as any
  identity other than the invoking account, calls `live_boundary.isolate()`
  before importing production code. `tests/live_boundary_test.py` checks that
  every such suite does.
- **Never trust the inherited environment.** A fixture never relies on HOME,
  the XDG directories or the pane variables. It gives every path it creates an
  explicit root of its own.
- **Chown walks need a floor.** A fixture that changes ownership along a path
  stops at a root it created, and refuses a path outside that root, instead of
  walking to `/`.
- **Commands go through both layers.** Direct argv is checked before exec. A
  shell command is interpreted normally, then its actual libc effects are
  checked by the native guard. The sentinel still proves that nothing escaped
  either layer.
- **tmux names its own server.** Use `-S` or `-L` under a directory the test
  created, or rely on the disposable TMUX_TMPDIR `isolate()` sets. Never remove
  TMUX and TMUX_TMPDIR and then run a bare tmux.
- **Cleanup signals what it started, by pid or by its own group.** Never by name,
  never everything.
- **Run sweeps in a disposable machine.** A broad comparison of suites runs in a
  throwaway VM, as a real unprivileged user, with sentinel servers for that user
  and at least one other. It does not run on the host.
- **Run from a pane like this.** To run suites from a role pane, run them as
  they are: `isolate()` scrubs the pane. When sweeping many suites, still use a
  disposable HOME and TMPDIR for the runner itself.

## Tests

`tests/live_boundary_test.py`, with a before-run on main at `f22638f`:
- **Production rule:**
  - the caller's account keeps its pane stores;
  - another account resolves its own;
  - an unknown account keeps the ambient store.
  On main, another account gets the caller's pane store.
- **The p-ops/S1 set-aside,** run in a namespace child as root, with the Audit
  pane's variables pointing at a decoy live store:
  - here, `S1` is recorded and set aside in owner 1500's own store, and the
    decoy is empty;
  - on main, `roles/audit/roles/ops/p-ops_0.0.json.superseded` lands in the
    decoy, the incident's write.
- **The guard,** in a Python child and in a namespace child:
  - it refuses chown, mkdir, write, rename and exec chown under a live root,
    naming the command;
  - it allows a write but no chown in a writable root;
  - it is invisible to an `os.lstat` recorder.
- **Runtime shell effects:** variable, function, `eval`, script-file,
  substitution, `find -delete`, `sed -i`, and Bash-array commands are expanded
  by the real shell and refused at their actual operation. Safe arrays and
  quoted heredocs continue to run.
- **`isolate()`** leaves a child none of the pane's variables.
- **The sentinel** fails a run with status 97 when a namespace command chowns the
  live store, naming that command.
- **tmux:** the socket each command line reaches, by tmux's rules. Refusal of the
  account's own socket, bare, explicit and in a shell, in a Python child and a
  namespace child, while a disposable socket is allowed. Every shared
  `/tmp/tmux-<uid>` directory is refused. An isolated run's bare tmux reaches a
  server of its own. The sentinel fails a run that stops a server, recorded or
  not.
- **Process rules,** in a Python child and a namespace child: sudo, a wrapped
  systemctl, loginctl inside a shell script, pkill, killall, `kill -- -1`, a
  foreign group, `os.kill(-1)` and a foreign `os.killpg` are refused. `grep`
  naming sudo, and signals to the run's own group, are not. Every broad signal
  is SIGCONT, so a guard that failed would harm nothing.
- **Coverage:** every suite that maps root in a user namespace isolates itself
  first. That includes `role_cutover_writes_test.py`, the incident's suite as
  merged by SYRD-612. Its `Host.make` now refuses a path outside the fixture's
  homes instead of chowning every ancestor of it.
- **Two suites built their tenant in a real home.**
  `legacy_root_owned_provision_upgrade_test.py` and
  `privileged_plan_read_no_follow_test.py` ran, as namespace root, in a
  directory inside the invoking account's own home (SYRD-611's
  `root_stage_in`). Each tenant now has a fixture home.
  `owner_home_sandbox.stand_in_home` makes it the owner's home for root's
  writes and for every account lookup. The suites also stand in the new
  tenant's control repository, which `new_project_artifacts` places under
  `/home/<owner>` by name.
- **Four suites that run as the invoking account wrote its live pane stores.**
  `control_repository_placeholder_test.py`,
  `team_launcher_control_repo_test.py`,
  `team_launcher_layout_upgrade_commands_test.py` and
  `team_launcher_viewer_test.py` each wrote `pgu-ticket-board` provider state
  or resume logs into the real `~/.local/state`. These are the same shape as the
  Otto artifacts. They now isolate themselves too.
  - Found by running, in a disposable VM, every suite that touches the otto or
    pgu stores or loads `team_launcher_test_helpers`, and listing the new files
    in the real home after each.
  - Three of the four already assert that no provider state was written. That
    assertion fails on main and here alike: the files now land in the disposable
    home instead.
  - The scan sees only what a suite writes before it stops. A suite that is red
    in the VM may write more where it passes.

`tests/live_boundary_sentinel_test.py` proves cross-account isolation. It runs
in a sandbox (`unshare --user --map-auto --map-root-user --mount --pid`, private
/tmp and /run, the home read-only) with sentinel tmux servers for the runner's
UID and two other UIDs, each with two sessions. Commands run in the old sweep's
environment against a clone of the committed tree.
- **Controls:** a bare `tmux kill-server` and an unprivileged pkill each lose the
  runner's server and no other. A privileged pkill loses all three.
- **Behind `isolate()`:** the bare tmux is the run's own, every broad kill is
  refused, and every sentinel survives.
- **runtime_switch_stall_test.py behind the boundary:** it passes, and every
  sentinel survives.
