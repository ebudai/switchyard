# SYRD-895: a tenant's panes never inherit the launching user's real-time scheduling

## What happened

On 2026-11-27, between about 09:37 and 12:34 EST, an ocstab stress run started from its ops pane ran its 33 worker
threads as SCHED_RR priority 1, at nice -4. No code in the run asked for either.

This host runs with real-time throttling off: `kernel.sched_rt_runtime_us` equals `sched_rt_period_us`, a CachyOS
default the User keeps. So the threads took all 32 CPUs:

- load average reached 143;
- board reads took 4 to 7 seconds, and a board write timed out;
- every tenant's board service, Postgres and listener starved, and so did the User's desktop.

The Director demoted the threads by hand.

## Why

The ocstab panes run as `ocstab-agent`, which no limits rule grants anything. Yet their limits were
`Max realtime priority 99 99` and `Max nice priority 31 31`, the User's own from
`/etc/security/limits.d/20-audio.conf` (`@audio - rtprio 99`, `@audio - nice -11`).

Measured on this host, read-only from `/proc`:

- every tenant's tmux server (`tmux: server`, running as the tenant's account) is reparented to the User's
  `systemd --user`, and carries rtprio 99:99 and nice 31:31;
- PID 1 carries 0:0 for both;
- syrd's own server carries the same limits as ocstab's.

The chain that carries them:

- A process inherits its parent's resource limits.
- sudo, the tenant-control bridge and `runuser` cross to the tenant account without lowering them. pam_limits sets
  only what a rule names for the target, and no rule names these for a tenant.
- A tenant's tmux server keeps whatever the process that started it had, for every pane it ever opens.

## The fix

`scripts/tenant_limits.py`, `drop_inherited_scheduling()`:

- lowers the calling process's RLIMIT_RTPRIO and RLIMIT_NICE to 0:0, PID 1's, so no real-time priority and no nice
  below 0;
- raises a negative nice to 0;
- leaves a real-time scheduling policy for SCHED_OTHER.

Any process may lower its own limits and priority without privilege.

A pane inherits from its tmux server, and the server from whichever process started it. So the drop has to happen
in every process that may start a tenant's server, not in one caller. In round 1 it was only in
`_start_role_session`. Audit then started the server first with break-glass's tmux start, through its real owner
runner, and the role pane opened afterwards had rtprio 99:99 and nice 31:31.

It is now called in three places:

- **At the entry of `scripts/switchyard` and `scripts/team-launcher`,** before anything else runs. That covers every
  command and every path: start, restart-role, break-glass, Director rebind, worker starts (which run the launcher's
  pane command), the presentation viewer and display sessions, and Copilot's status probe, which is an owner-side
  command the launcher starts.
- **By the owner-account runners,** `process_inspection.role_process_runner_for` and `_owner_process_runner`. They
  cover a caller in a longer-lived process that runs tmux as the project, break-glass's start among them.
- **At the top of `role_sessions._start_role_session`,** as in round 1.

The listener and the board run as services under systemd, whose PAM session for the tenant grants none of these
limits. The pane's command line doesn't change. Processes the launcher starts as the User, such as its terminal
windows, also run without real-time priority. Nothing they do needs it.

RLIMIT_NICE is set to the default, 0. A project-configured value was offered by the ticket as an alternative and was
not added.

## Servers already running

A tmux server started before this change keeps the old limits until every role of its tenant stops. The host
readiness report that root sees after every `switchyard install-shared-release` (`host_diagnostics`) now names such a
tenant under its own heading: "tenant readiness: these tenants' panes can take real-time scheduling or a negative
nice". It gives the restart that clears it, `switchyard stop <project>` then `switchyard start <project>`; the server
exits once every role has stopped, and the next start runs a new one.

The check matches each registered tenant's owner by the owner of its configuration file, and reads each of that
owner's `tmux: server` processes' `/proc/<pid>/limits`. On 2026-11-27, run as switchyard-agent, it named syrd's
server (rtprio 99:99, nice 31:31).

## Pinned by

- **`tests/tenant_limits_test.py`** (24 checks):
  - **A server break-glass started first** (Audit's case): break-glass's tmux start, through the real
    `role_process_runner_for`, from a process holding 99/31, starts the server. Its pane and the role pane opened
    afterwards both read 0 0.
  - **The owner runner:** `_owner_process_runner` hands sudo a process already at 0:0.
  - **Every command's entry:** `scripts/switchyard` and `scripts/team-launcher`, run as themselves, leave their
    process at 0:0.
  - **The real start:** the real `_start_role_session`, run by a process holding this host's launching limits (rtprio
    99:99), starts a role on a private tmux server whose pane writes its own `/proc/self/limits`. It shows
    `Max realtime priority 0 0` and `Max nice priority 0 0`. With the drop taken out, the pane inherits 99:99, so the
    check sees a leak. That half is skipped, and says so, where the launching user has no rtprio.
  - **The drop**, from nice -4 and SCHED_RR: limits 0:0, nice 0 and SCHED_OTHER, inherited by its children.
  - **Reading /proc's `limits` text.**
  - **The readiness check** on a fixture tree.
- **The VM kit** (`tests/vm_kit`, `tests/vm_kit_test.py`):
  - the kit's user gets `@audio - rtprio 99` and `nice -11` from limits.d, as this host's User has, and a fresh ssh
    session must show `ulimit -r` 99;
  - after `switchyard new`, a command typed in the tenant's Director pane must read `Max realtime priority 0 0` and
    `Max nice priority 0 0`.
