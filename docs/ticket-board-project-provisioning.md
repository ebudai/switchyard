# Per-Project Ticket Board Provisioning

Render reviewable artifacts for a new project board:

```bash
scripts/ticket-board-provision-project \
  --project stellaris \
  --owner-user stellaris-agent \
  --port 8871 \
  --output-dir /tmp/stellaris-board-provision
```

Projects that want source-control work after final sign-off can insert a VCS
stage and make a project role responsible for the terminal close:

```bash
scripts/ticket-board-provision-project \
  --project stellaris \
  --owner-user stellaris-agent \
  --implementer-role ops \
  --vcs-close-role ops \
  --output-dir /tmp/stellaris-board-provision
```

The output directory contains:

- `plan.json`: resolved project name, owner, port, database, unit names, socket,
  asset paths, frame paths, and connection strings.
- `<project>-ticket-board.service`: system board service unit with unique port,
  runtime directory, Unix socket, database, and owner-home asset/frame paths.
- `<project>-ticket-board-notify-listener.service`: owner user unit for the
  LISTEN/notification shim, pointed at the same project database and pane-state
  runtime namespace.
- `<project>-ticket-board.conf`: tmpfiles entry for the project frame inbox.
- `<project>-database.sql`: PostgreSQL database/role bootstrap.
- `<project>-workflow.sql`: per-project workflow seed. For new non-`pgu`
  projects this is rendered as a projection of the authoritative workflow seed
  in `scripts/ticket_board/schema.sql`, with project-specific role lists
  substituted for implementation and draft ownership. Tenant boards
  deliberately omit the pgu-only `Backlog` and `Inspection` stages plus the
  `defer`, `start_task`, `submit_to_inspection`, and `request_commit_exempt`
  actions; future schema workflow additions that are not part of that explicit
  omission policy flow into generated tenant workflow SQL. The generated
  `Audit` stage uses `entry_gate_field=needs_audit` and `gate_skip_to=dat`, so
  each ticket can opt out of audit while still using the same workflow gate
  machinery as UAT.
  This derivation also intentionally aligns tenant director routing with
  `schema.sql`: newly generated tenants no longer get the historical tenant-only
  `in_progress -> audit` or `audit -> director_review` `route` edges, and they
  do get the schema-authoritative `audit -> in_progress` `route` edge. Existing
  tenants are not re-seeded by this path; if an existing tenant needs those old
  escape edges, handle that with an explicit migration or a director override.
  Passing `--vcs-close-role <role>` inserts a `VCS` stage after
  `Final Sign-Off`, routes final-signoff tickets there by director action, and
  changes the generated terminal `mark_done` workflow transition to
  `<role> -> done`. The board service unit also exports
  `TICKET_BOARD_OPERATION_ALLOWED_ROLES=mark_done=<role>` so the HTTP gate
  permits that deployment-specific close request to reach the database.
  For an existing provisioned project, first add the close role if necessary,
  then run `switchyard set-vcs-close-role <project> <role>`. That command
  refuses unknown roles, updates the generated plan and board unit, applies the
  workflow transition update in Postgres, and restarts the tenant board. The
  built-in `pgu` workflow remains fixed; this command is only for provisioned
  tenant projects.
  The default implementation-stage owners are `app` and `main`; pass
  `--implementer-role <role>` more than once to seed a project-specific
  implementer set instead.
- `operator-commands.sh`: ordered privileged commands to review and run.

The order in that script is part of what it does. Every grant the board service
account needs is made before the board is deployed, because the deploy exports
an immutable release into the board tree and then starts a canary as that
account: a release exported into a tree the account cannot enter is one the
canary cannot serve. The grant on the board root carries a default ACL, which is
what reaches releases that do not exist yet, so a deploy months later inherits
it at creation without anything walking the tree again. Granting after the
deploy looked equivalent on every host that had already been provisioned, where
the tree carried the entries and the deploy succeeded; on a fresh host the
deploy was the first thing to touch the tree, failed, and stopped the script
before the grant it needed (SYRD-145).

The script is run by its own absolute path, from anywhere, and it is not
preceded by a `cd`. Each artifact that ships beside it is addressed from
`$provision_dir` -- the directory the script itself is in, computed from
`BASH_SOURCE` on its first line -- so the packet root installs under
`/etc/switchyard/provision/<slug>/` reads root's own artifacts wherever the
operator happens to be standing. It used to name most of them by bare file
name, which is resolved against the caller's working directory: run from a
journal, a Polkit transaction or another project's checkout, a root-owned
packet looked for root's artifacts in an unrelated directory and stopped at
`install: cannot stat <slug>-ticket-board.conf`. An instruction that told
somebody to change directory first is what made that dependency look like a
convention rather than the defect it was (SYRD-149).

## Resuming a provision that stopped

A `switchyard new` that fails before it writes `/etc/switchyard/projects/<slug>.json`
leaves a real installation that no ordinary command can name: the account, its
repository, its credentials, its desktop policy, its rollout journal and its
exported board release all exist, and every command that resolves a project
through the registry answers `unknown project`. That failure now names the way
back, and the way back is:

```sh
sudo switchyard resume-provision <slug> [--source-repo /opt/switchyard/releases/<commit>] [--config <path>]
```

It reads root's own provisioning record -- `/etc/switchyard/provision/<slug>/plan.json`,
walked component by component and required to belong to root -- and checks the
identity it names against the kernel rather than believing it. The tenant's own
copy is never consulted for what root installs: it is writable by the account
every role runs as. From that record it rebuilds every artifact root installs,
from the release named on the command line or the installed shared release, and
hands back the ordinary operator packet to run. It refuses rather than
reconciling: a record that names a different project, an owner the kernel does
not know or whose home disagrees with the record, a release that is not
root-controlled, and any rebuild that would change a value root regenerates --
an account, a home, a board root, a unit name.

Rebuilding rather than re-running matters: the preserved packet was rendered by
the release that failed, so re-running it would repeat the defect it failed on.
Nothing the tenant owns is touched and running it twice produces the same
artifacts.

### What happens after the packet

Running the packet is not the end of a `switchyard new`. Registering the
project and starting its roles belonged to the process that had already exited,
so a recovery that ran the packet perfectly still left a project with a live
board that no ordinary command could name and no role sessions at all --
testing journal 0011, where `/etc/switchyard/projects/testing.json` was absent
and the owner uid had no tmux server (SYRD-155).

So the same command continues past the packet, and the same command is the
retry:

1. **It reads whether the packet finished**, from what the packet installs --
   the board unit, the tmpfiles configuration, the polkit rule, the listener
   unit in the owner's home, an exported release, a running board and listener,
   and a board that answers. Not from a marker: a marker says a script reached
   its last line, and what the rest of the recovery depends on is whether those
   things are there. While any of them is missing it names them, points at the
   packet, and **exits non-zero** -- a recovery that has finished nothing does
   not report success.
2. **It verifies the generated configuration before registering it.** The
   registry entry is a pointer, and following it decides which account runs the
   roles, which board they talk to and which tree they work in. So the
   configuration is read without following symlinks, required to belong to the
   project owner and to be unwritable by anybody else, and checked field by
   field against root's record -- project, owner, ticket prefix, board socket
   and port, and the roles it declares. A configuration that disagrees is
   refused, not reconciled. Where the checkout lives is the one thing root
   cannot regenerate, so once verified the path is recorded beside the plan and
   re-verified on every later read; `--config <path>` names it for a checkout
   that has moved.
3. **It installs the desktop access the roles need, before anything verifies
   it.** `switchyard new` installs the scoped Wayland grant and then checks it;
   a launch only ever checks. So a recovery that went straight to launching
   asked the tenant to prove access nobody had given it, and stopped on a
   readiness receipt that nothing had written -- with the approved policy
   sitting intact on disk (SYRD-158). The policy installed is the one this
   host's root-owned approval record covers: the tenant's own configuration
   may carry the grant and the attribution of the consent recorded for it, but
   it may not name a different desktop, and it cannot conjure an approval this
   host never recorded. Either of those is refused, and no grant is made. A
   headless tenant installs nothing. The install itself is the supported one,
   with its own rollback: what it cannot complete it puts back, the persistent
   GUI-owner service reapplies the grant after a reboot or a recreated socket,
   and adding this tenant's entry leaves every other tenant's exactly where it
   was.
4. **It starts the roles through the ordinary launcher path** -- the same
   `launch_project` that `switchyard new` and `switchyard <slug>` use, which
   starts each role as the project owner when the caller is somebody else.
5. **It proves the result before calling it done**: the project is registered
   and the entry points at the verified configuration, the board and listener
   are running, every configured role has a live pane, and every role has
   registered a runtime session with the board. Anything missing is named, and
   the status is non-zero.

Every phase is derived from the world rather than from a progress file, so an
interrupted recovery is finished by running the command again: a project
already registered is not registered twice, roles already running are attached
to rather than started again, and a registration that succeeded before a launch
that failed still stands.

The script is re-runnable, which is how an interrupted provision is completed:
accounts and groups are created only when `getent` does not find them,
directories are installed rather than recreated, the ACL grants are `setfacl
-m` additions that leave unrelated entries alone, and a release that is already
exported is not exported again. Completing a partial run neither deletes nor
duplicates an account, a repository, a credential or a journal.

The provisioner intentionally renders first. It does not mutate the live PGU
board, restart panes, create databases, or install units unless an operator runs
the generated commands during an approved window.

`switchyard new` validates role CLI choices before it reaches this render step.
The accepted CLI names are `claude`, `codex`, and `agy`; retired aliases such as
`gemini` are rejected at prompt/artifact parse time. If the selected owner user
already exists, `new` warns and asks for explicit confirmation, defaulting to No,
before treating that account as the project owner. Pass
`--allow-existing-owner-user` to skip that existing-user confirmation and reuse
the account without prompting. In non-interactive runs, `new` refuses an
existing owner user unless that flag is present.

The `pgu` project is intentionally special-cased to reproduce the deployed
production board: database `pgu`, HTTP port `8770`, and frame inbox
`/tmp/pgu-frames` with a root-owned `1777` tmpfiles entry. It also keeps the
full workflow seeded by `schema.sql`. New projects keep their frame inboxes
under the owner user's home directory and apply `<project>-workflow.sql` after
migrations and before `rbac.sql`.

## Current Schema Constraint

Each project gets its own database. The database roles remain
`ticket_board_service` and `ticket_board_listener` for now because
`schema.sql` enforces those literal actors in `require_actor()` and
`require_ticket_board_listener()`. The separation boundary is the database, not
per-project DB role names. The generated RBAC path reuses `rbac.sql`, including
the listener grant on `ticket_has_unresolved_blockers`.

Changing to per-project DB role names is a separate schema change, not part of
this provisioning step.

Workflow authority role names are still `director`, optional `audit`, and
`user`. Provisioning customizes the implementation-stage owner roles because
those are project/team specific; the board service exports the generated
assignee, caller-role, and implementer-role lists to keep the HTTP gate and
PostgreSQL workflow data aligned.

`TICKET_BOARD_OPERATION_ALLOWED_ROLES` is an optional HTTP preflight override
for deployment-specific operations. Its format is
`operation=role,role;other_operation=role`. Unset keeps the built-in operation
map unchanged. The setting can only decide which callers are allowed past the
HTTP route check; PostgreSQL `workflow_transitions.allowed_roles` remains the
authority for ticket state changes, so the env map cannot grant a transition
the workflow table rejects.

## Switchyard source commit verification

The `syrd` provisioning control repo has unrelated seed history and cannot
verify Switchyard source commits. Select the GitHub fetch cache explicitly
when provisioning it:

    switchyard new ... --commit-git-dir /path/to/switchyard-source-cache.git

The standalone renderer accepts the same `--commit-git-dir` option. The value
is persisted in `plan.json`, the board unit, and operator commands. `add-role`
and `set-vcs-close-role` preserve it. To repair or change an existing tenant,
use `switchyard upgrade <project> --commit-git-dir <path>`; this refreshes the
generated plan and units and includes the same value in the printed deployment
command. With no option, upgrade preserves the recorded selection.

There is no implicit `/data` source choice for new SYRD or `switchyard` tenants.
Legacy PGU, MEFP, and Otto compatibility mappings remain until their archive
dependencies are retired separately. The selected cache must fetch GitHub
feature branches before audit submission so the server can resolve their commits.
The write client separately requires the submitted commit to be pushed to the
caller's `origin`; run it from the real source checkout. Test that a fetched
feature-branch commit is accepted and a nonexistent hash is rejected. Keep
permissions and audit gates intact; GitHub publication and cache refresh are
director/operator actions.
