# SYRD-786: `switchyard teardown <slug>` removes the whole tenant

The User: "the flags are supposed to be used when I want to deviate from the norm. I should only need switchyard
teardown test12". Before this, removing a tenant needed `--confirm <slug> --destroy-registered-tenant
--remove-owner-home --remove-owner-user` (and `--drop-nonempty-board` when the ticket count could not be read). Even
then, the test11 and test12 teardowns left half a tenant behind:
- the account, its home and its linger file;
- its panes' tmux servers and agents, still running, so `userdel` refused the account;
- the desktop grant's ACL entries on the desktop user's runtime;
- root's rollout journal, group-owned by a gid the next account could be given.

## What it does now

`sudo switchyard teardown <slug>`:

1. **Refuses**, before asking or running anything:
   - the Switchyard project's own board (registry name `Switchyard`);
   - the project the command runs from (`TICKET_BOARD_PROJECT`);
   - an owner account that is not a tenant's agent account: a member of `root`, `sudo`, `wheel`, `adm` or `admin`, or
     what `owner_removal_refusal` already refused;
   - **an owner that is another tenant's account.** Every other registry entry's owner is resolved: its
     configuration's `run_as_user`, else its plan's `owner_user`, else `<slug>-agent`. A match is refused by name:
     "stellaris-agent is mefp's account; tear down mefp instead". The typed slug confirms the slug, not the account,
     so this check is what keeps a leftover's teardown (`teardown stellaris`, or `--owner-user`) off a live tenant.
     An entry whose owner cannot be read fails closed, naming the entry. Files of another schema, and links, are not
     entries. An owner whose home holds Switchyard state for another slug is refused too, which covers leftovers
     whose registry record is gone.

   The owner checks apply with `--keep-user` too, because its processes are still stopped and its home still removed.
   A dry run shows the refusal. A real run exits with it and changes nothing.
2. **Prints one preview**:
   - the owner (or "already gone");
   - the home and its size, the checkout, the board's ticket count, and whether the tenant is registered;
   - what is running as the owner now;
   - what is kept, and by which flag;
   - every action with its exact command.
3. **Asks for the slug to be typed once**: `Type <slug> to remove it: `. A wrong answer changes nothing. `--yes`, or
   `--confirm <slug>`, answers for a script. With no terminal and neither flag, it refuses and names `--yes`.
4. **Runs, in this order**:
   1. stops the board service and the listener;
   2. **withdraws the desktop grant**: each grant the desktop user recorded in its own home is uninstalled, then every
      ACL entry naming the tenant's uid on `/run/user/*`, their Wayland sockets and `/run/switchyard-desktop/*` is
      removed;
   3. **stops every process of the owner**:
      - disables linger and removes `/var/lib/systemd/linger/<owner>`;
      - stops `user@<uid>`, terminates the session, and stops `user-runtime-dir@<uid>`;
      - sends SIGTERM, then SIGKILL, to every process of the uid, each with up to five seconds to exit. A tmux server
        started for a pane is under neither the user manager nor the session.

      The step fails if any process remains. It refuses the account running it, the one behind `sudo`, and any uid
      below 1000;
   4. removes the units, the configuration, the database, the board root and the registry entry;
   5. **settles root's records**:
      - removes the staged tooling (`/usr/local/lib/switchyard/<slug>`) and root's provisioning record;
      - removes its publication grant: the sudoers rule naming its account (`/etc/sudoers.d/48-<slug>-publish`), root's
        push grant and key (`/etc/switchyard/publish/<slug>.json`, `<slug>-publish-key` and `.pub`), and its staging
        (`/var/lib/switchyard/publish/<slug>.git`). A later account given the same name would otherwise inherit the
        rule;
      - keeps root's rollout journal (`/var/lib/switchyard/rollout/<slug>`), re-owned to root and mode 0700, as the
        record of what root ran;
      - re-owns to root anything else under root's Switchyard directories that the uid still owns, or the gid when
        that group is the account's own (named after it, and nobody else's primary group);
   6. removes the home, then the account.
5. **Reads back** the account, its user manager and processes, the linger file and the home. If any is left, it exits
   non-zero with "did NOT remove all of <slug>; it is not safe to recreate under the same slug", naming each one.

`--keep-home`, `--keep-user` and `--keep-board-data` keep their part. With `--keep-user`, linger and the account stay,
but the tenant's processes are still stopped. `--destroy-registered-tenant`, `--drop-nonempty-board`,
`--remove-owner-home` and `--remove-owner-user` are accepted for old scripts and change nothing.
`scripts/team-launcher <slug> teardown` keeps its opt-in home and account removal.

The steps that need Python run `scripts/teardown_tenant.py` as an isolated program (`python3 -I`).

## Tested

- `tests/whole_tenant_teardown_test.py`: the real command, unprivileged, against a disposable home. The slug is typed
  on a real pty to the real `input`. It covers:
  - the typed slug, a wrong slug, `--confirm` naming another slug, no terminal, and `--yes`;
  - each `--keep-*` flag, and the old flags;
  - the refusals of the Switchyard board, the running project and an administrator's account;
  - the linger file and home read back as residue;
  - root's records (another tenant's publication grant kept), and the desktop grant and its ACL entries, on
    disposable trees.
- `tests/project_teardown_boundary_test.py` and `tests/team_launcher_teardown_owner_removal_test.py` pin the action
  order, the preview and the owner removal steps.
- 44 mutants, all killed by a FAILED CHECK. They include skipping linger and the session stop, no SIGKILL, signalling
  only the session, skipping the typed confirmation, ignoring each keep flag, and leaving root's records, the ACL
  entries or the publication grant. Round 2 (after Audit) adds: the other-tenant check dropped, not matched, skipped
  under `--keep-user`, or skipping an unreadable entry; each owner-resolution fallback; the home-state check; and the
  private-group gid sweep. The process stop's refusal of its own account is not mutated, because a surviving mutant would signal the
  account running the tests.
- **In a disposable Arch VM, as root:**
  - Setup: a lingering account with a running user manager, a tmux server and a SIGTERM-ignoring agent outside its
    session, desktop ACL entries for its uid, root's records group-owned by its gid, a publication grant (sudoers
    rule, grant, key, staging), and a board database.
  - Base: `--confirm --destroy-registered-tenant --remove-owner-home --remove-owner-user` stopped at the unknown ticket
    count. With `--drop-nonempty-board` added, it failed at the wait step. It left the account, the processes, the ACL
    entries, the staged tooling, the provisioning record and the gid-owned journal.
  - Candidate: `switchyard teardown t786 --yes`, on a fresh tenant and on base's leftover. Each time the account, home,
    linger file, logind user, processes, ACL entries and publication grant are gone (another tenant's grant stays),
    the journal is root:root 0700, and nothing under root's directories is owned by the uid or gid.
  - A mutant that skips linger and the session stop leaves the linger file. The read-back names it and the teardown
    exits 1.
  - Round 2: with a registered tenant `live` whose configuration (root-only, 0600) runs as t786-agent, these are all
    refused with "t786-agent is live's account; tear down live instead", and the account, its processes and everything
    else of it are untouched: `teardown t786 --yes`, the same with `--keep-user`, `teardown leftover --owner-user
    t786-agent --yes`, and the dry run. With `live` deregistered, `teardown t786 --yes` removes it all as before.
