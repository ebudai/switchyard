# SYRD-854: a deploy proves the release's served board and listener first, and contains a listener that does not hold

## What happened

Release bceacabf passed every suite, the Librarian, Audit, the Director's Final
Sign-Off and every check the deploy makes. preview-upgrade said READY, and
deploy-release exited 0. Then syrd's notification listener crash-looped 31 s
after the switch, more than 578 times: no notices, no Publisher, no stall alerts.
`configure_workflow` failed on the board too.

The served processes run with the release's `scripts/` on the path, so
`scripts.ticket_board` is not importable there (fixed in SYRD-853). Nothing had
run them that way before they were live:

- every suite builds its boards from the repository root;
- every workflow dry run goes to the running board, which is the old release
  until the switch;
- deploy-release's canary only asked a throwaway board for `/api/board`;
- the listener was read `active` once, straight after its restart, and a
  `Restart=always` unit in a restart loop reads that too.

## Part 1: the release smoke (`scripts/release_smoke.py`)

The smoke runs the release the way its units do: `<python> -E -s -B`, from the
release directory, with nothing of the repository on the path. It uses a
database of its own and answers READY or NOT READY.

1. **Imports.** Every import statement, at module level and inside functions,
   in every module the board server or the listener can import must resolve in
   the release layout. Every one of those modules must also import there.
   - "Can import" is the closure of every import, from the `ticket_board` entry
     modules of `scripts/ticket-board.py` and
     `scripts/ticket-board-notify-listener`.
   - Importing the modules alone would not run their function-level imports,
     which is how bceacabf got through.
   - An import inside a `try` that handles ImportError, or a fallback inside such
     a handler, is left to that handler. Steps 3 and 4 still catch one that
     re-raises.
2. **Database.** A throwaway PostgreSQL cluster is built in a private directory,
   on a Unix socket only. It is built the way the operator packet provisions a
   board, from the release's own files: the service and listener roles, the
   database, `schema.sql`, `ticket-board-migrate`, then `rbac.sql`.
3. **Board.** The release's real `ticket-board.py` serves that database and
   answers `configure_workflow` dry runs, through its own socket, for:
   - the tenant's declared workflow, read from its database as `postgres`, or the
     release's example workflow when the tenant declares none. The example's roles
     name its own project, so the throwaway board then takes that name, and
     `--project` can be any name;
   - that workflow with each optional composer the release ships: the
     documentation gate, Router, the Publisher, the research stage and the plan
     gate. Each is composed by the release's own composer code, run as the
     repository-root CLI runs it.

   A composer that declines the workflow, for example because it is already
   enabled, is reported and is not a failure. Anything else is.

   **The live board's stages (SYRD-866).** The throwaway board starts with the
   default stages. A tenant's live board has the stages its declared workflow
   left it: sapi removed `inspection` long ago, so on 79280dab every dry run of
   sapi's workflow was refused ("configuration omits an existing stage"), and
   sapi could not take the release. Root now also reads the live board's stages
   as `postgres` and hands them to the smoke (`--stages`). Every trial, and the
   apply in step 4, then:
   - also removes the default stages the live board does not have. This is on the
     throwaway board alone: the tenant's document and live board are not
     changed;
   - is refused if it omits a stage the live board does have, in the board's own
     words, as the live board would refuse it.
4. **Listener.** The workflow with the Publisher is applied and one ticket waits
   in publication. The release's real `ticket-board-notify-listener` must then:
   - establish LISTEN;
   - have its Publisher record a real outcome for that ticket ("was not recorded"
     is not one). The Publisher runs only after the workflow validated
     (`validate_publisher`);
   - claim and route the notice that outcome queued;
   - still be running.

**Delivery stops at the pane gate.** Real delivery needs a live pane: a runtime
assignment with a live process, trusted hook state and a tmux target. A
disposable smoke cannot honestly have one, and the pane gate fails closed by
design. So the smoke checks the delivery path as far as that gate: the notice is
enqueued, claimed (`listener_claim`) and deferred (`gate_defer:no_hook_state`).

**Who runs it.** PostgreSQL refuses root, so root's entry (`run_as`, called
through `for_tenant`) runs the smoke as the tenant owner, with `python3 -I`.
The smoke uses the standard library only, and its stand-in `tmux` and
`systemctl` exit 1, so no live server or unit is ever reached. It changes
nothing outside its temporary directory, which it removes. A run takes about
7 s.

**Nothing outlives it.** The postmaster is the smoke's own child, not a `pg_ctl`
daemon. Postgres, the board and the listener each carry a parent-death signal
(SIGQUIT for postgres, SIGTERM for the others), so a smoke killed outright leaves
nothing running. The smoke carries one too. If root's `run_as` times out and
kills `runuser`, the smoke gets SIGTERM, and it turns SIGTERM into an ordinary
exit: its processes are stopped and its directory is removed.

### Where it runs

We chose both places the ticket offered:

- **deploy-release:** after its refusals, before the listener stops and before
  root migrates the live database. A NOT READY refuses the deploy with the
  traceback and records the release phase blocked. The board, its database and
  the listener are not touched. A converging deploy (the tenant already names
  the release, under another unit) is not smoked: its code is already live.
- **preview-upgrade's `_finish_upgrade`:** for a pinned dry run as root. A NOT
  READY becomes the dry run's reason, so the verdict is NOT READY before
  anything is prepared. If root has not installed the release yet, the preview
  already warns about that, deploy-release refuses it, and the smoke says it did
  not run.

## Part 2: after the switch

**A listener that was restarting is started.** A listener that reads
`activating` at the start (a `Restart=always` unit between restarts) is one the
tenant runs. The deploy starts it after the switch, as it does an active one.
Before this change, deploy-release left a crash-looping listener stopped.

**The watch** (`listener_holds`). After the listener starts, it is read through
the owner's manager (`ActiveState`, `SubState`, `NRestarts`, `MainPID`) every
second for `LISTENER_WATCH_SECONDS` (20 s). Its unit restarts a crash within
`RestartSec=2`, so a crash loop shows up well inside that window. It holds only
if all three are true:
- it stays active;
- it never restarts and keeps its main pid;
- its log (the unit's `StandardOutput=append:` file) says `LISTEN …
  established` after the start.

If the owner's manager will not start the unit at all, nothing the release did
caused it. That keeps today's outcome: blocked, with nothing reverted.

### The revert, and the schema rule

When the listener does not hold, the schema decides what happens. The rule is the
one-tenant rollback's (SYRD-855), so the automatic revert and `rollback-release`
cannot disagree. `tenant_release_rollback.schema_problems` reads the migrations
the database has applied, as `postgres`. What the previous release carries is
read from root's installed release of it (`trusted_release_root`), never from the
tenant's staged copy, which the tenant can write. Every applied migration the
previous release does not carry must be declared safe for older code in
`scripts/ticket_board/schema_compatibility.py`, and any condition it is declared
under must hold on the tenant's database.

**When they all are,** the revert is automatic:
1. `revert_release` puts back the tenant's release selector `current`, which
   the listener runs from, and root's previous board and canary units, and
   restarts the board.
2. The listener is restarted on the previous release and watched again.
3. The release phase is recorded blocked with the reason, the output says
   REVERTED, and the exit is 3 (rolled back).
4. If the previous release does not hold either, that is said too, as a
   leftover that needs an operator.

**Otherwise there is no automatic revert.** The previous release's code would
run on a schema nobody has declared it safe on. The listener is stopped to end
its restart loop, and the board stays on the new release. The phase is recorded
blocked, naming each migration and why, the output says NOT REVERTED, and the
exit is 1. The way out is a fixed release, or `rollback-release` once those
migrations are declared safe.

**Migrations are never undone.** Which releases' code is safe against a newer
schema is the table's to say, one declaration per migration from pgu1051 on,
with its condition. A migration that isn't declared, or is declared unsafe,
refuses the revert. The Director first approved a stricter rule here: no revert
across any migration at all. SYRD-855 landed first with this table, and one rule
for both paths is safer than two. In the bceacabf incident, pgu1053 to pgu1055
are declared safe, two of them only while the research stage is not declared, so
whether that revert is automatic depends on the tenant's workflow.

`revert_release` is the one revert deploy-release makes. It also replaces the
attestation failure's inline copy. The one-tenant rollback (SYRD-855) is a full
deploy of the earlier release, which goes through the same smoke and watch.

## Part 3: evidence for served code

A change to code the served board or listener loads is reviewed from the
release layout. Audit, before signing off, and the Director, before Final
Sign-Off, run the smoke on the candidate:

```bash
dir=$(mktemp -d)
git archive <candidate> scripts examples | tar -x -C "$dir"
python3 -I -B scripts/release_smoke.py --release "$dir" --project <any name>
```

Add `--workflow <file>` to smoke a tenant's declared workflow, and
`--stages <file>` (a JSON list) for its live board's stages. Without
`--stages`, they are the workflow's own, which a board that applied it has.
Repository-root suites import `scripts.ticket_board`, so they are no evidence
for what the served processes import. The disposable-VM review for Audit is its own ticket.

## Tests

- `tests/release_smoke_test.py` runs the smoke on releases archived from this
  repository:
  - bceacabf is NOT READY, naming its imports;
  - HEAD is READY, with every step said;
  - a re-raising import that the import check leaves to its handler is NOT READY
    at the dry run, with "No module named 'scripts'";
  - a Publisher that records nothing is NOT READY at the listener.

  It also covers root's `run_as`, `for_tenant` and `preview` through a stand-in
  `runuser`, preview-upgrade's hook, and the watch.

  For the live board's stages (SYRD-866), it uses sapi's, mefp's and syrd's
  declared workflows and live stages, read from their boards
  (`tests/fixtures/syrd866_workflows`):
  - 79280dab's own smoke refuses sapi's;
  - all three are READY on HEAD;
  - a workflow that omits a live stage is still refused, whether the stage is a
    default one or one the throwaway board never had;
  - root's entry reads the live stages and passes them to the smoke.
- `tests/catalogued_deploy_release_test.py` covers the deploy:
  - the smoke refuses before anything live changes;
  - a listener that crash-loops is reverted (3), and its previous release holds;
  - the same crash-loop across a migration not declared safe for the previous
    release is not reverted, and the loop is stopped (1);
  - a previous release that does not hold either is reported as needing an
    operator;
  - a listener restarting at the start is started and watched;
  - a healthy deploy is unchanged except for the smoke and the watch.

  This suite was red on main in a sweep: it added a git worktree to a read-only
  repository. It was also red on this host, because the close asked the real
  installed unit for the board. Both are fixed: the before-tree is a
  `git archive`, and the board opener is the stand-in.
