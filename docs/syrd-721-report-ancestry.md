# SYRD-721: report ancestry, and one report filed safely to several boards

## Why

A tenant had one upstream report receiver. MEFP consumes SAPI as a library, so
one defect can need a ticket on SAPI (the library's fault) and another on
Switchyard (the platform's part). Two unrelated receiver URLs would not say how
those boards relate, and would not scale.

## The model

A project has zero or one **report parent**. A parent may have its own, so the
project's **report ancestors** are an ordered chain, nearest first: for MEFP →
SAPI → Switchyard, they are SAPI, then Switchyard. Ancestry is not ticket
parentage and not a code dependency.

**Root's records.** The parent is `report_parent` in the project's root-owned
registry record (`/etc/switchyard/projects/<slug>.json`). It holds the
project, its board and its verified ticket prefix. Every change is appended to
`report_parent_history` (from, to, when, who, why).
- Only root's `switchyard upgrade <slug> --only upstream-report` (and
  `switchyard new`, and each full upgrade's migration) writes it.
- A tenant cannot rewrite its ancestry: not through its board, and not through
  its configuration, which every role can write.
- The records are world-readable, so a pane and a board service read the
  chain without root and without another tenant's files
  (`scripts/ticket_board/report_ancestry.py`).

**Migration.** A record without the key takes its parent from root's record of
the project's one upstream connection (`upstream_report`, connected). So a
project reports exactly where it did before. The next full upgrade declares it
explicitly (`report_parent.migrate`, reason "migrated from its one upstream
connection"). A declared `{"project": ""}` means no parent.

**Refused, never guessed:**
- a project that is its own parent;
- a cycle;
- an unregistered ancestor;
- two links with the same board or ticket prefix;
- a chain deeper than eight;
- an ambiguous `--to` name;
- a board that does not answer as the project root recorded (checked through
  its public `/api/client-config`).

Nothing is inferred from other boards on the host or from repository remotes.

## Declaring and changing a parent (root)

`scripts/report_parent.py`, run by `--only upstream-report
[--upstream-report-board <slug> | --no-upstream-report] [--dry-run]`.

**Before anything is written**, the proposed parent must be another registered
project whose board answers as itself. That is root's existing check, its
public `/api/runtime-assignments` answer, which now also names the board's
ticket prefix. A board older than this change names none; then no prefix is
recorded, and the prefix is still checked at use. The chain it brings, read from root's
records, must pass every rule above, with each board verified live. A refusal
writes nothing, and the registry is byte-identical afterwards.

**Then** the step:
1. connects the project as before (link and immediate-parent credential);
2. records the parent with a history entry;
3. issues a credential for each ancestor.

After a complete run, the same declaration again changes nothing. If a partial
failure wrote a credential file but not root's issuance record, the next run
repairs the record without rotating the current private credential; the same
reconciliation restores a missing credential file.

**One credential per ancestor.** For each board in the chain, root writes
`report-credentials/<ancestor>.env` beside the immediate-parent credential,
owner-only, holding that board's own credential. It is bound to the project
where the board reads bound credentials (SYRD-677). One board's token is never
written for, or sent to, another. Root notes which it issued in the registry
record (`report_credentials`: when, and whether bound), never the token.
Credentials grant only `file_report` and report follow-ups, as before.

**Removing or changing a parent erases nothing.** Removal declares no parent
and takes the link out of the project's configuration. Credentials already
issued and the reporting project's receipts stay. A report filed before the
change can still be followed up through the credential it was filed with,
while its board accepts that credential and answers as itself.

## Filing (panes)

`scripts/ticket_board/report_fanout.py`.

| Command | Goes to |
|---|---|
| `file-report` (no `--to`) | the immediate parent, exactly as before |
| `file-report --to <project>` | one verified ancestor, named by slug, name or ticket prefix |
| `file-report --to all` | each ancestor, nearest first |

**The SYRD-602 two steps cover the set.**
- The preview writes nothing. It names each destination by project name (never
  a URL) and says how many tickets it will create.
- One digest binds the ordered destinations and each destination's own
  confirmation. Each board checks its own confirmation, as before.

**The report group.** `rg-` plus 20 hex digits, from what the report says and
who says it (source project, title, body, requested state, source reference),
not from where it goes. So filing to the parent and later to all ancestors is
one group.
- It is sent to a board that advertises `report_groups` in its client
  configuration, and bound into that board's confirmation under a new digest
  version.
- A report without a group confirms exactly as before, so older clients and
  boards keep working.
- `file-report` without `--to` sends no group: the same digest and the same
  request as before. Its receipt is still kept, so a later `--to all` with the
  same report skips that board.
- The board records it with the confirmation (`report_confirmations.report_group`,
  pgu1023) and says it on the ticket in a service comment. Related reports can
  be recognised without either board controlling the other.

**Not one transaction.** Boards are written in order.
- After each board, the reporting project writes a receipt
  (`~/.local/state/switchyard/<project>/report-receipts/<group>.json`, 0600)
  naming the destination's ticket, board, prefix and credential file. It never
  holds a token.
- On a failure, the remaining boards are still tried, every outcome is said,
  and the command exits non-zero.
- The same command and digest again skips boards with a receipt and files only
  on the rest.
- A board that took the report but whose answer was lost replays its own
  confirmation record (pgu998) and returns the same ticket. No board ever gets
  a second ticket for the group.

**Follow-ups.** `report-follow-up --group <rg> --to all|<project>`, or `--to
<project> --ticket <ID>`. Destinations come from the receipt, or from the
chain. Each gets SYRD-677's follow-up with its own confirmation, under one
digest, and partial retries are idempotent the same way.

**Reading.** `ticket-board-read report-ancestors [--json]` lists, for the
parent and each ancestor:
- the slug, name and ticket prefix;
- identity verified or not, and reachable or not;
- the credential: usable, and bound to which project.

The board page's **Reports go to** line shows the same ancestors from
`GET /api/report-ancestry`, with one deliberate difference. The board service
reads no tenant file, so its credential column says only whether root's
records show a credential was *issued* (`credential.issued` in the JSON, where
the pane's answer has `credential.usable`). This is the Director's decision
on this ticket (2026-11-01): the board API and UI show only root's issuance
record, and never call it usable. `report-ancestors` in a pane is the live
check. The board service never reads report tokens, and no role's report of
what it saw is shown as proof. The page points to `report-ancestors`. Whether the pane's credential file is
present, readable and bound to the project is `report-ancestors`' to say, in
the pane. The guide says which to trust when they differ. No URL in text, and
no token anywhere.

## Evidence

`tests/report_ancestry_test.py` uses production-built `syrd`, `sapi`, `mefp`
and `pie` boards over real HTTP, root's code against a scratch registry and
home, and the real `ticket-board-write` and `ticket-board-read`. It covers the
ticket's eight acceptance points, plus:
- root's verification, journaling, dry runs and refusals;
- a parent change and a removal;
- the board page;
- digest compatibility with main;
- the upgrade from main's schema.

No token appears in any output, receipt or registry record.
