# SYRD-521: an explicit `unassigned` is honoured or refused, never quietly handed to a worker

## What happened

MEFP's audited importer ran:
```
ticket-board-write create-ticket --draft --assignee unassigned --parent-id MEFP-6 --needs-audit --needs-user-signoff
```
It expected inert intake. MEFP-129 was created in Draft **owned by
`designer`**. In MEFP's workflow (revision 51) the Draft stage is owned by
`designer` and notifies its assignee, and the designer acted on the ticket
before release. MEFP has since moved its intake to Backlog, which works.

## Cause, reproduced on current main (262fd5c)

The reproduction used a production-built board with MEFP's live document,
through the real server.
- **The insert trigger substitutes the owner.** On a declared board,
  `enforce_ticket_workflow_insert` gives every new ticket in a `draft`-kind
  stage that stage's sole owner (`stage_default_assignee`). A stage that
  notifies its assignee then tells that owner at once. That is SYRD-11's
  declared Draft ownership, and it is intended for a ticket whose owner is
  left to the board.
- **An explicit request could not be told from a default.**
  `create-ticket --assignee` defaulted to `unassigned`. The Python client
  always sent `assignee`, defaulting to `unassigned`. The web form always
  sent its select's value. So "explicitly unassigned" and "no preference"
  arrived identically, and both were silently given to the owner.

Both the explicit and the omitted request produced a Draft owned by
`designer` plus a transition notice to it. The **shipped** workflow behaves
the same way with its own Draft owner, `director`, so a team without a
designer is no safer.

## The contract

- **Omitted assignee means "the board places it".** Ordinary Draft
  ownership is unchanged: the stage's owner receives the ticket and its
  notice, and release works as before.
- **An explicit `unassigned` into a stage that would not keep it is
  refused.** That is a `draft`-kind stage with a single owner. The refusal
  comes **before anything is written or sent**, and says where inert intake
  works:
  > an explicitly unassigned draft ticket would not stay unassigned: this
  > workflow gives every new draft ticket to its owner, designer. For inert
  > intake create it in backlog (owned by nobody, notifies nobody); to hand
  > it to designer, leave the assignee out
- **Everywhere else an explicit `unassigned` is honoured as before.** That
  includes an unowned Draft and Analysis creation from the web form.
- **Inert intake is the workflow's parking stage** (MEFP's `backlog`: kind
  draft, no owners, notify none). It keeps `unassigned`, the parent and both
  review gates, notifies nobody and reserves nobody.

**Explicitness is now carried honestly:**
- `ticket-board-write create-ticket --assignee` defaults to *not sent*;
- the Python client sends `assignee` only when given one;
- the web form omits it for a Draft, which can take no assignee anyway, and
  sends the selected one otherwise.

**Unchanged:**
- **Stage semantics:** the insert trigger, the notification policy and the
  reservation rules.
- **The importer's placement guard:** it now receives a refusal before
  anything is written, instead of a placement mismatch after the fact.
- **Review gates and legacy boards:** the check reads the declared workflow.

## Evidence

- **`tests/explicit_unassigned_draft_test.py`: 16 checks.** Every board is
  built the production way and driven through the real `ticket-board-write`
  CLI and HTTP server, with the pane's board identity cleared. It passes
  under `env -i` and in the pane environment.
  - **Before** (262fd5c code and SQL, child process): the importer's exact
    request creates a Draft owned by `designer`, and the designer is
    notified. Reproduced.
  - **MEFP's shape, this tree:**
    - the importer's request is refused, naming `designer` and `backlog`,
      with nothing written or queued;
    - an omitted assignee gives a designer-owned Draft with its parent and
      gates, which releases to analysis/director as ever;
    - Backlog intake stays unassigned with its parent and both gates, and
      no notice or reservation for anyone;
    - the web form's draft request (no assignee field) and its Analysis
      request (explicit `unassigned`) both still work;
    - the served `createTicket`, run under node, sends a draft without an
      assignee and anything else with the chosen one.
  - **The shipped workflow** (Draft owned by `director`, no designer): the
    same request is refused, naming the Director.
  - **A Draft with no single owner:** an explicit `unassigned` is honoured,
    and nobody is told.
- **Mutation: 6/6 killed.** Covered: the refusal removed; the CLI default
  restored; the client always sending an assignee; the refusal ignoring
  stage kind; the form sending the draft its assignee; an unowned Draft
  refused too.
- **Sweep.** 113 suites touching ticket creation, the write client and CLI,
  the server, the web create form, drafts or `file_bug` were run serially
  under `env -i`, against `262fd5c`.
  - Pass/fail is identical for all 113.
  - The 31 red on both trees end on the same line; most are browser suites
    stopping at the missing Playwright build.
  - Where they have `test_` functions, the red suites match case by case
    (146 cases, failure lines included).
