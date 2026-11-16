# SYRD-801: the operator's restart-role starts, and a Director's own piped restart is not refused

Reported from tenant pie on release 3dfb073b (2026-11-15): neither route the guide gives for restarting the
Director after an upgrade worked. The User stopped and started the whole tenant instead.

## `sudo switchyard restart-role` died before doing anything

Root runs the operator's restart as the project's owner: `runuser -u <owner> -- python3 -I -B
scripts/role_restart.py operator ...`. In isolated mode (`-I`) neither the script's directory nor the tree is on
`sys.path`, and the module's first tree import (`from scripts import launch_environment`) ran before the path insert
in its `__main__` block. It died with `ModuleNotFoundError: No module named 'scripts'` and changed nothing.

The path insert now sits above that import, still only when the file runs as a script, and the bottom block only
calls `main`. The report also described a duplicated `__main__` block: there is only one, in 3dfb073b as on main.

**Tested:** role_restart_test runs the exact command root builds, minus its `runuser` prefix, from outside the
tree with a bare environment. It starts, reads the board, and refuses in words ("Nothing was changed"). The old
module, run the same way, prints the reported traceback.

## A Claude Code Director's restart of itself named its own command line as work

From its own pane, the Director's restart counts as refusals every process in the pane that is not this command,
one of its ancestors, or one of its children. A pipe's other ends are none of those. In `switchyard restart-role
pie director 2>&1 | tail`, the `tail` is a sibling, so the Director was told its pane "runs work besides this
command".

**Measured with Claude Code 2.1.318, offline:** a stand-in API, Claude Code as a tmux pane's root.
- Each Bash tool command runs in a session of its own, led by the tool's shell.
- An unpiped command leaves nothing besides itself.
- A piped one leaves exactly the pipe's other ends, in that session.

The report's two pids were not recorded with names, and pie's transcript is not readable from this account. A pipe
is the cause measured here.

**Now:** when this command's session is led by one of its own ancestors below the pane's root, the rest of that
session is this command line too, and it ends with it. That includes an `&` job on the same line, which the
restart ends with the pane; a separate tool command, such as a background task, is a session of its own. A command typed at a shell that is itself the pane shares the
pane's session, and gets no exemption. Work in any other session still refuses: another tool command, or a
background task. The refusal now names each process (`pid 4110 pytest`), so a Director can see what it would end.

**Tested:**
- role_restart_test, on a fake kernel:
  - a pipeline in the tool's own session passes;
  - another session's command still refuses, beside the pipeline;
  - in the pane's own session, the pane shell's background job still refuses.
- Against real Claude Code: its tool runs `look.py | tail`. The session is its own, the `tail` is beside it there,
  and `other_work` finds nothing.

**The probe cleans up after itself** (Audit of e282897f): its end stops the stand-in API, and its EXIT trap does too
when a run fails early. The check asserts that no process naming the run's own directory, and no socket directory,
remains after a run.
