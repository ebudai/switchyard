# Board supervisor: one host service for every tenant board (SYRD-586)

Every Switchyard tenant used to run its board as its own system unit,
`<project>-ticket-board.service`. The operator asked for one host-level service
that owns every board's lifecycle. That service is
`switchyard-board-supervisor.service`.

## The decision: a supervisor with an isolated child per tenant

The alternative was one process serving every tenant's board. It was rejected
because it cannot keep what each tenant has today:

- **Build identity.** Each tenant pins its own release
  (`/home/<owner>/<project>-ticketboard-live/current`) and upgrades it on its
  own schedule. One interpreter cannot run two releases of the same modules.
- **Unix identity.** Each board runs as `boardsvc` with its tenant's primary
  group. That group is what lets its 0750 runtime directory and 0660 socket
  reach that tenant's accounts and no other tenant's. A process has one gid.
- **Filesystem sandbox.** Every board runs as the same `boardsvc` uid, so what
  stops one tenant's board writing another tenant's files is the mount
  namespace: `ProtectSystem=full`, `ProtectHome=read-only`, and
  `ReadWritePaths=` naming only its own asset and frame directories. One
  process has one mount namespace.
- **Credentials and process-bound authorization.** Each board's database URL,
  database role, report token and `TICKET_BOARD_TENANT_USER` are in its own
  environment. The peer-credential checks on its socket are made against those
  values. Sharing a process would mean sharing them.
- **Fault isolation.** A crash, a hang or a bad migration in one board stops
  that board alone.

So the supervisor is a parent and nothing else. It holds no tenant's data or
credentials between starts, and each child is built from that tenant's own unit.

## How a child is built

The tenant's reviewed unit stays installed at
`/etc/systemd/system/<project>-ticket-board.service`, exactly as provisioning
and the release review left it. On every start, the supervisor reads its
`[Service]` section (`scripts/board_supervisor_spec.py`) and runs what it
says. The exec stage (`scripts/board_supervisor_sandbox.py`) applies it in
systemd's order:

1. a private mount namespace (propagation from the host only);
2. the protected trees made read-only, keeping each mount's other flags;
3. `ReadWritePaths=` made writable again;
4. `User=`, then `Group=` (or the user's primary group) and the unit's own
   `SupplementaryGroups=`, and nothing else (see "Groups" below);
5. `NoNewPrivileges=`, then a parent-death signal;
6. `WorkingDirectory=` and umask 022;
7. `exec` of `ExecStart=`. The process the supervisor watches is the board itself.

The environment is systemd's own:
- `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `RUNTIME_DIRECTORY` and the
  manager's locale;
- then the unit's `Environment=`;
- then each `EnvironmentFile=`, whose values win, as in systemd.

`RuntimeDirectory=` is created fresh for each start with its mode and owner,
and removed when the board exits.

### Groups: deliberately stricter than systemd

systemd gives a service with `User=` every group that `/etc/group` lists that
account in (`initgroups`), and then adds `SupplementaryGroups=`. That is wrong
for boards, because every tenant's board runs as the one `boardsvc` account,
so one tenant's membership becomes every tenant's.

On the live host, `boardsvc` is listed in `syrd-roles` (941), so under
systemd the Otto, MEFP and PGU boards all carry 941. Through it they can reach
SYRD's resources, `/run/syrd-ticket-board` among them.

A supervised child gets exactly its unit's `Group=` (or `boardsvc`'s primary
group when the unit names none) and the unit's `SupplementaryGroups=`, and no
other group. A board that needs a group names it in its own unit, as
`syrd-ticket-board.service` names `syrd-roles` in `Group=`, so SYRD keeps 941
and the other tenants lose it.

| board | under systemd | under the supervisor |
|---|---|---|
| syrd | `[941]` | `[941]` |
| otto | `[1005, 941]` | `[1005]` |
| mefp | `[1002, 941]` | `[1002]` |
| pgu (no `Group=`) | `[944, 941]` | `[944]` |

Rolling a tenant back to its unit returns it to systemd's computation, until
`boardsvc` is taken out of `syrd-roles` on the host.

**Refusals.** The supervisor will not start a board when:
- the unit uses a key it does not apply (for example `PrivateTmp=`);
- the unit has a drop-in other than the supervisor's own;
- the unit names `User=root`;
- the environment file is a link, is owned by anyone other than root or the
  tenant's owner, or is writable by its group or others (a role could
  otherwise set the board's credentials);
- systemd reports the unit active.

Each refusal names the file and line and shows in status. The supervisor does
not silently skip a setting systemd would have applied.

## Lifecycle

- **Restart policy.** Any exit the supervisor did not ask for is restarted
  after the unit's `RestartSec=`. That includes a clean exit 0. The board exits
  0 on SIGTERM from any sender, and under `Restart=on-failure` that read as a
  finished service. That is how `otto-ticket-board.service` stopped while
  Otto's panes and listener kept running.
- **Start limit.** The unit's `StartLimitBurst=` and `StartLimitIntervalSec=`
  still hold. A board that keeps failing is left `failed`, not flapping.
  `start` or `restart` clears it, as `reset-failed` would.
- **Intentional stops persist.** A stop through the supervisor is recorded in
  `/var/lib/switchyard/board-supervisor/<project>.desired`, so a suspended
  tenant stays stopped across supervisor restarts and reboots. Stopping the
  supervisor itself stops every board but leaves each desired state alone.
- **Upgrades.** An upgrade is `restart` of one tenant. The unit and the
  `current` link are read again, so the board comes up on its new release. No
  other child is touched.
- **Stopping.** A stop sends SIGTERM to the board's process group, then
  SIGKILL after `TimeoutStopSec=` (default 90s). Whatever is left in the group
  after the board exits is killed too.
- **Supervisor restart.** Restarting the supervisor restarts every board. It
  runs from `/opt/switchyard/current`, so this happens only with a host
  install. A tenant's own upgrades never restart it.

## Control and visibility

`/run/switchyard-board-supervisor/control.sock` takes one JSON request per
connection. The caller's uid comes from the kernel (`SO_PEERCRED`):

| verb | who |
|---|---|
| `status [project]` | anyone |
| `start`, `stop`, `restart <project>` | root, and that tenant's owner for that tenant only |
| `reload` | root (adopts added tenant records, releases removed ones) |

The owner grant matches the polkit rule each unit tenant already has (start,
stop and restart of its own board).

**Status.** `/run/switchyard-board-supervisor/status.json` (0644) is rewritten
on every state change. For each tenant it gives:
- its desired state and current state;
- its child's pid and start time;
- its automatic restarts;
- its release and port;
- its last exit: pid, status or signal, time, and whether the supervisor
  requested it.

`switchyard status` prints that record as a `BOARD SERVICES` section. Its
"from root-owned records" fallback reads a supervised tenant's board state from
it, not from the disabled unit.

**Journal.** Every supervisor line in the journal carries `tenant=<project>`
and, once the board runs, `pid=<pid>`. The board's own output is re-tagged the
same way.

## Using it

Everything plans by default and changes nothing. `--apply` runs as root.

```
switchyard-board-supervisor install [--apply]
switchyard-board-supervisor migrate <project> [--apply] [--owner <account>]
switchyard-board-supervisor rollback <project> [--apply]
switchyard-board-supervisor status [<project>] [--json]
switchyard-board-supervisor start|stop|restart <project>
switchyard-board-supervisor reload
```

### install

Writes `switchyard-board-supervisor.service` and runs `enable --now`. The
supervisor runs `/opt/switchyard/current/scripts/board_supervisor.py serve`
with `python3 -I`. With no tenant records it supervises nothing, so installing
it changes no tenant.

### migrate, one tenant at a time

1. Installs `<unit>.d/50-switchyard-board-supervisor.conf`, containing
   `AssertPathExists=!/etc/switchyard/board-supervisor/tenants/<project>.json`,
   and reloads systemd. While the tenant record exists, systemd refuses to
   start the old unit, so a stray start cannot contend with the child for its
   port and socket. On systemd 262, a negated assertion on an existing path
   fails the start job with `Assertion failed on job for <unit>.`
2. `systemctl disable --now <unit>`. The board is down from here until step 4.
3. Writes the desired state and the tenant record. The owner is taken from the
   unit's `TICKET_BOARD_TENANT_USER`, else from the owner of the board root,
   else from `--owner`. A unit that was inactive (a suspended tenant) is
   recorded `stopped` and stays down.
4. `reload`. The supervisor starts the child.
5. Verifies that:
   - the child runs;
   - `http://127.0.0.1:<port>/api/board` answers;
   - the unit is inactive;
   - systemd lists the drop-in.

A failure after step 1 undoes every step in reverse and starts the unit again.
The run is recorded in the tenant's rollout journal
(`/var/lib/switchyard/rollout/<project>/`), naming the child's pid.

### rollback

Stops the child, then removes the tenant record, the desired state and the
drop-in. It enables the unit again if it was enabled, starts it if the tenant
was meant to be running, and verifies that the board answers. It is journaled
like a migration.

### Where a supervised tenant is handled

| caller | what happens for a supervised tenant |
|---|---|
| `scripts/board_services.py` (suspend, resume, upgrade activation, restoring units after a failed cutover) | reaches the child through the supervisor |
| `scripts/ticket-board-service.sh` (`deploy-restart`, `start`, `stop`, `restart`, `status`) | `systemctl_system` sends the board's own unit actions to the supervisor; canary units are unchanged |
| role-add's board restart | reaches the child through the supervisor |
| teardown | first releases the child and removes the drop-in |
| residual-process containment | treats the supervisor's control group as managed |

A tenant without a record takes every one of these paths exactly as before.

## What it does not do

- The deploy canary (`<project>-ticket-board-canary.service`) and the notify
  listener (the owner's user unit) stay systemd units. Neither is a board.
- It does not split children into their own cgroups. A board's resource use
  shows under `switchyard-board-supervisor.service`, and kill-on-stop reaches
  its process group. A descendant that leaves that group with `setsid`
  survives the stop, until the supervisor itself stops.
- Host-wide `service.d/` drop-ins are not applied to supervised boards.
  Drop-ins for the board's unit refuse the migration.
