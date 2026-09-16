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

The script closes the tenant's own source tree before it grants anything a
way through the home. The home is 0710 and a principal that must reach one
thing beneath it -- the board service reaching its release -- gets a named
`--x` entry; traversal was meant to be the whole grant, but everything created
beneath the home was 0755, so traversal implied read and
`sudo -u boardsvc test -r /home/<tenant>/Projects/<project>` succeeded through
ordinary mode bits with no ACL involved. Every directory between the home and
the checkout is now named at 0750 owned by the tenant rather than left to
`install -d` to create on the way past -- which is also why the parent was the
worse half, since `install -d` applies `-m`, `-o` and `-g` only to the last
component and the intermediate takes root's umask (SYRD-156).

Which directory that is comes from the plan's `project_repository`, and it is
not `source_repo`: `source_repo` is the audited RELEASE the artifacts are
rendered from, which on a provisioned host is `/opt/switchyard/releases/<sha>`
and is outside every tenant home. Passing it where the checkout was meant
produced a packet that confined nothing while looking finished. `switchyard
new` records the checkout it creates; `switchyard upgrade` and `switchyard
resume-provision` record it from the location of the generated configuration --
`<checkout>/.switchyard/provision/<slug>.json` -- which is structural rather
than a field the tenant could choose. A plan that records no checkout confines
nothing and says so, naming those two repairs.

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

The database phases are re-runnable for the same reason, and one of them had
to be taught it. `schema.sql` seeds the built-in workflow -- eleven stages and
the moves between them -- and a provisioned project then replaces those rows
with its own and narrows `workflow_stages_name_check` to the stage names it
declares. Replaying the base file over such a board tried to insert `backlog`,
a name that board no longer admits, and the supported recovery died there
(SYRD-160). The seed now establishes a workflow for a board that has none and
leaves a configured board alone; changes to the built-in workflow reach boards
that already have one through migrations, which is how `dat` was added. The
constraint itself is untouched -- a board that has narrowed it still refuses
`backlog`, which is what makes the replay safe rather than merely quiet. The
same guard closes the silent half: where a project's stage names happen to
match built-in ones, the seed's `ON CONFLICT DO UPDATE` used to reset that
board's labels and owner_roles without raising anything at all.

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

### Approving this host's desktop, without a prompt

A project's scoped Wayland policy is generated from this host's standing
desktop approval, kept root-owned 0600 at `/etc/switchyard/desktop-approval.json`
because an approval a tenant could write is an approval a tenant could give
itself. Until now the only thing that wrote it was the `switchyard new` prompt,
so pre-authorizing a host for unattended provisioning meant calling into the
library or hand-writing root's JSON -- both of them the thing everything else
here avoids.

    pkexec switchyard approve-desktop --gui-user USER --reference '<why>'
    switchyard approve-desktop --show
    pkexec switchyard approve-desktop --revoke --reference '<why>'

Approving and revoking need root and need a person: `approved_by` comes from
the mechanism that elevated the run -- `PKEXEC_UID`, then `SUDO_USER` -- and
never from a flag, so the record cannot be made to name somebody who did not
ask for it. `--reference` is required, because an approval nobody can
attribute is worse than none. The GUI user is inferred only when exactly one
account is signed in; several, or none, and it must be named. The record is
read by fd with no symlink at any component and required to belong to root,
written the same way and left 0600, and one that cannot be read is never
written over. Revoking keeps the record with nobody approved in it, carrying
who withdrew it and why, so a withdrawal is evidence rather than an absence --
and every reader already answers "no approval" for that shape.

This is a standing host-level grant and is not `--desktop-policy`, which
supplies a policy for one launch. It grants nothing by itself: each project
still gets its own scoped policy, installed by its own GUI owner (SYRD-174).

### Adopting an existing project's declared workflow

A project provisioned before root kept that record has its declared workflow in
one place only: the tenant's own generated plan, writable by the account every
role runs as. Its packets seed nothing rather than seeding the default workflow
over it, which is safe and stuck. `pkexec switchyard adopt-workflow <slug>` is
how it stops being stuck, and it is deliberately not automatic:

- the document is read the way root reads anything it did not write -- by fd,
  refusing a symlink at every component, required to belong to the project
  owner or root and to be unwritable by anybody else -- and validated the way
  provisioning validates it;
- it is checked against the workflow the running board is actually enforcing,
  read over the board's own socket. Every line where the two differ is printed,
  and a difference is a refusal. `--despite-board '<why>'` is the narrated
  recovery for a board that lost its configuration, and the reason is recorded
  with everything else;
- the run has to be authorized by a person through Polkit. Root alone is not
  authorization: a script that inherited root has nobody to record the decision
  against, and the decision is the point;
- nothing is written without `--apply`, and what was shown and decided goes
  into the rollout journal either way;
- the record is written atomically, root-owned and 0644, and read back before
  the command says it adopted anything.

Afterwards `switchyard upgrade` and `switchyard resume-provision` regenerate
that project's declared workflow from root's copy, byte for byte (SYRD-166).

A project that declares its own workflow has that document recorded where only
root can write it -- `workflow.json`, beside root's plan record, carrying a
digest of what it holds -- when root first generates that project's artifacts.
Regeneration consults that copy and no other. The tenant's configuration
carries the document too, and it is never read here: it decides which roles
exist and what each of them may call, so a copy the account every role runs as
can write is a copy that account could grant itself with. A record that exists
and cannot be used -- a digest that does not match, a mode anybody else could
write through, a document the validator rejects -- is a refusal rather than a
reason to fall back. A project that declares a workflow root holds no record of
seeds nothing at all: not the declared one, which root cannot vouch for, and
not the default one, which is somebody else's (SYRD-165).

The worktree base is closed the same way the checkout is, and the socket group
is retired from it. Two independent things reached that tree: `other::r-x` on a
base created 0755, and a named entry to the SOCKET group -- which the board
service is in, because that group exists so role accounts can reach the board
socket and the service must be able to hand it over. Closing either alone left
the other, and the 163 directories under the base owned no ACL of their own, so
traversal into it was read access to the whole source tree across every role and
every ticket. Beside it the control repository still carried that group's `rwX`
with a default entry, so every object git wrote inherited it -- the grant
`repository_group_name()` exists to avoid.

So the packet closes the base and every tree in it, grants the repository group
for a tenant whose roles have their own accounts, and only then retires the
socket group from the base and the control repository. That order is the safety:
nothing loses access in the gap between taking one grant away and making the one
that replaces it. Removal is by entry, so everything else the tenant has is left
alone and re-running changes nothing, and it is guarded on the group existing so
a tenant that never had one is not stopped by an `Invalid argument`. The group
itself is never touched: the board service stays in it, and its named read
grants on the commit store and the board release are independent of it and
untouched (SYRD-171).

A tenant that is already registered cannot receive that repair by being
provisioned again. `upgrade` regenerates the artifacts and runs the unit and
deploy steps without applying the packet; `resume-provision` reads an active
board, a live listener and an exported release as a finished recovery and never
looks at the boundary. The only path that applied it was the whole
first-provisioning packet -- which deploys a release, replays the schema, seeds
a workflow, applies RBAC, installs and reloads units and starts sessions. For a
tenant that is serving that is not a repair, so `switchyard repair-boundary
<project>` exists to apply exactly the boundary and nothing else (SYRD-175).

It is run through Polkit -- `pkexec switchyard repair-boundary <project>
[--apply]` -- and refuses a run that only `sudo` elevated, because what it
changes is an operator's decision and has to be recorded against a person. What
it runs is not re-rendered here and nothing is read from the tenant: the phase
is lifted out of root's own installed packet in
`/etc/switchyard/provision/<project>/operator-commands.sh`, between the
`# >>> switchyard repository boundary` markers the packet writes around it, and
every line is checked against the shapes a boundary phase is made of before
anything runs. A packet root does not exclusively control, or one generated
before the phase existed, is a refusal naming `switchyard upgrade` rather than a
repair from a document somebody else could have written. The guarded blocks are
run whole, the way the shell would group them, so `if getent group ...; then`
still decides whether the retirement runs at all.

Without `--apply` it prints what is open and the exact lines it would run and
changes nothing. With it, each statement runs, the boundary is detected again
afterwards, and a boundary that is still open is reported as a failure -- this
command cannot report success over an open boundary. Everything is written to
the rollout journal under the operator Polkit named. Nothing else is touched:
no deploy, no schema, no workflow seed, no RBAC, no unit installation or reload,
no role registration and no session startup, so PIDs, release pointers, runtime
assignments and panes are exactly as they were. The same detection is part of
recovery readiness, where an open boundary is an objection that names this
command, so a recovery cannot report itself finished over one.

The one phase of the packet that is not a repair is the initial workflow seed.
It deletes the stages and transitions a board has and installs the project's
own, which is what a board being brought up needs and the last thing a running
one does -- its tickets sit in those stages, its roles hold runtime assignments
against them, and its history names transitions by name. Running it
unconditionally is how a supported upgrade of a registered tenant stopped on
`project workflow seed must run before tickets exist` (syrd rollout journal
0050, SYRD-164). The boundary is explicit: a board with tickets, or one
carrying a declared workflow document, is established, and the seed leaves it
exactly as it is while the rest of the packet -- units, ACLs, database,
migrations -- applies as the repairs they are. The guard that refuses a
destructive seed on a live board is untouched and still in the file; the
supported path simply no longer reaches it.

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
   Role startup also has to tell an empty commit store from a repository. The
   confinement work creates every managed directory -- the project checkout and
   the commit store among them -- before anything is granted on it, so by the
   time the launcher runs, `commit_git_dir` exists and is empty. Reading mere
   existence as an initialised bare repository is what produced `fatal: not in
   a git directory` in testing journal 0027; the three cases are now told
   apart, and an empty placeholder is cloned into as the owner while data at
   that path is refused without being deleted or written over (SYRD-161).
4. **It starts the roles through the ordinary launcher path** -- the same
   `launch_project` that `switchyard new` and `switchyard <slug>` use, which
   starts each role as the project owner when the caller is somebody else.
5. **It waits, within a bound, for what its own startup set in motion.**
   Registering a runtime is the pane's own asynchronous work -- the role's CLI
   starts, `ticket-board-register-runtime` announces it, the board records the
   row -- so a readiness check that sampled the instant the launcher returned
   was asking before the answer existed, and told an operator that a successful
   recovery had failed (testing journals 0032 and 0037, SYRD-162). Both places
   that ask now poll for up to 90 seconds, say once what they are waiting for,
   name exactly the roles still missing if the bound passes, and stop
   immediately for a session that has actually exited, because that one will
   not register however long anyone waits. Nothing restarts a pane or clears a
   session to make the answer arrive, and every identity check on an assignment
   -- a foreign target, a runtime that does not match the projection -- still
   refuses on the first reading, because those are not races.
6. **It proves the result before calling it done**: the project is registered
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
