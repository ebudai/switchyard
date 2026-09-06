# Changing a role's agent runtime

`switchyard set-role-runtime <project> <role> --cli <runtime>` moves an existing
role from one agent CLI to another. It is a Director command and needs no root:
the board authorizes the workflow change by caller role, and everything else it
touches belongs to the project owner.

```bash
switchyard set-role-runtime syrd audit --cli agy --dry-run   # every check, no change
switchyard set-role-runtime syrd audit --cli agy
switchyard set-role-runtime syrd audit --cli agy --force --reason "provider outage"
```

## Why it is one command

Changing a role by hand means editing generated JSON, applying a workflow
change, restarting one worker, and putting its display slot back — four steps
that fail independently. The failure that prompted this is the quiet one: a
display slot's proxy is an attach to a worker session, so when that session is
replaced the proxy's session-closed hook parks it on a recovery message and
nothing re-attaches it. The replacement worker runs perfectly well behind a
blank pane, and every other signal says the change succeeded.

## What it does, in order

Nothing is written until everything that can be checked has been:

1. The role exists, is one the launcher starts, and the requested runtime is
   supported. Switching to the runtime already configured is reported as
   nothing to do, not an error.
2. The new runtime could actually start — installed, logged in, and trusting the
   role's worktree — using the launcher's own first-run checks, so a runtime
   that passes here is one the launcher would also accept.
3. The role is not mid-turn. This is the launcher's activity gate, which fails
   closed: a live session whose hooks have written no state reads as busy,
   because the alternative is ending a turn on the strength of an absent
   record. `--force` requires `--reason`, which is recorded with the change.
4. The board validates the proposed workflow document without applying it.

Then, journalling each step so it can be undone in reverse:

5. Apply the workflow document, at the revision the preflight read, so a
   concurrent change is refused rather than overwritten.
6. Point the launcher config at the new runtime. Resume settings are rewritten
   from the new runtime's defaults, because a resume flag belonging to the old
   CLI would be handed to one that cannot read it.
7. Stop the old session, clear the resume record — it belongs to the runtime
   being left behind — and start the replacement.
8. **Only then**, reconnect every display slot mapped to that role. Reconnecting
   into the gap between stop and start attaches the proxy to nothing and parks
   it again for the same reason.

A detached role, or one no slot is showing, simply skips the last step; that is
reported as "no display slot showed it" rather than passed over in silence.

## When something fails

Every applied step is undone in reverse and the command says the switch was
undone. If the undo cannot finish, the command says so explicitly, names what is
still wrong, and leaves the journal in place:

```
.../role-runtime-<role>.journal.json
```

That file holds the previous runtime, the previous workflow document and its
revision, the previous launcher config, and which steps were applied — enough to
finish the repair by hand. It is deleted on success and on a clean rollback, so
its presence means an operator is needed.

## What it records

The result distinguishes configured state from live state: whether the runtime
changed, and whether a live session was actually replaced. A role that was not
running is reconfigured without claiming its session was restarted.
