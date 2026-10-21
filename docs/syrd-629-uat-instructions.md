# SYRD-629: the User's UAT steps at the top of the ticket, and Send to Director

## The problem

A ticket in User Review showed the User a banner whose "what to check" was
derived from the first line of the implementation or the body, followed by a
summary of the body, the implementation, the Audit prompt and every sign-off.
The steps the Director actually meant were in some comment, if anywhere. The
User had to rebuild them from history, or ask the Director to say them again.
The User's way out was `user_kick_back`, which sent the work straight back to
the implementer, with no Director in between.

## What the User sees

At the top of the ticket detail, before the body and the comments, the panel
**UAT: What you need to do** has three parts:

- **Do this**: the exact command or UI action, with any real prerequisite.
- **You should see**: the concrete result that means it worked.
- **Then choose**: by default, "Accept if that happened. If it did not, or
  you need help, use Send to Director with a short message saying what you
  saw."

Below the parts are who prepared them and when, a **Questions or observations**
box, and three buttons:

- **Ask Director**: the question becomes an attributed comment, and the ticket
  stays in User Review. The Director gets one notice (`ticket_update`, payload
  kind `uat_question`, which delivery always sends to the Director, though the
  ticket is the User's). The panel shows the User's latest question with every
  Director comment after it, or "Waiting for the Director's answer." "After"
  means after the question's own comment, not "at or after its time": times are
  whole seconds, so an earlier note can share the question's.
- **Accept**: the User's ordinary sign-off. The confirmation asks "Accept this
  ticket?" and says where it goes next.
- **Send to Director**: it needs a short message. The confirmation asks "Send
  this ticket and your message to the Director?" and says the Director reviews
  the message and decides; nothing goes back to the implementer unless the
  Director sends it.

Text in backticks renders as code. A fenced block renders as a block with a
**Copy** button.

When the ticket has no current steps, the panel never invents any:

- If the Director's comments include an explicit UAT request (a comment that
  begins "UAT" or "User UAT"), the panel shows the latest one, marked as coming
  from the comments.
- Otherwise it says **Director must prepare UAT steps**.

Tickets already in User Review before the upgrade land in one of these two
cases.

After Accept, the ticket moves to the Director's final review and the panel goes.
A ticket accepted but still in User Review, as an older row can be, reads **UAT:
Accepted ✓** and offers nothing. After Send to Director, the panel reads **UAT:
The Director is reviewing your message**, shows the User's message, and says what
happens next.

The message box is one of the detail's draft fields. The board redraws an open
ticket whenever anything changes, and a redraw keeps what the User is typing.
After an Ask or a Send succeeds, the box is empty again. This exposed an older
race in `loadBoard`: it captured drafts before fetching `/api/board` and
redrew after, so whatever was typed while the request was in flight was lost.
That was true of every detail field, the comment box included. It now captures
them once the response is in, just before the redraw.

The page acts as the User over HTTP. Its Accept and Send to Director are the
User's own declared actions out of a User-owned review stage, which
`ui_delegation` marks with placement `uat_panel`. On a legacy board they are
`user_sign_off` and `user_reopen`, the latter with the message as its comment.

## Steps are ticket data, and they belong to one round

`ticket-board-write uat-instructions <id> --do-this ... --you-should-see ...
[--then-choose ...]` calls `set_uat_instructions`. It is also spelled
`set-uat-instructions`, after the operation, as every operation's subcommand is.
`ask_director` has no subcommand: it refuses every caller but the User, who has
no pane. Only the Director may call
it (it needs `director_edit`). It is refused on a terminal ticket, and it
requires all three parts, none blank and none over 1500 characters. The steps
are stored in `ticket_uat_instructions` for the ticket's commit at that moment
(`''` for no code). Every version, including reconfirmations, is appended to
`ticket_uat_instruction_history`. `--reconfirm` restamps the current text for
the ticket as it is now.

Steps stop being current when:

- the ticket leaves a User Review stage without the User's acceptance (Send to
  Director, a withdrawal, a defer or cancel, a forced move); or
- the ticket enters an implementation stage; or
- the ticket's commit is no longer the one they were written for.

The trigger `tickets_zzzzzz_uat_instructions_required` refuses any state change
into a User Review stage unless the steps are current. That covers declared
transitions, legacy transitions, Director edits and forced moves. The refusal
names the gap and the command that closes it. A returned or resubmitted ticket
therefore reaches the User again only after the Director replaces or
reconfirms its steps. The guard fires on UPDATE only: `create_ticket` starts a
ticket in draft, analysis or backlog, never in a review. The trigger's name
makes it run after every other BEFORE trigger, so it sees the state they
settled.

A User Review stage is:

- on a declared board, a review stage the User owns;
- on a legacy board, `user_review`.

The page shows the panel in exactly these stages (`ticketIsUserReview` reads the
same shape from the workflow), so a tenant that names its User review stage
differently gets both the guard and the panel.

The ticket JSON carries `uat`, built by `ticket_uat`: the steps, whether they
are current and why not, and the last ten questions.

## Send to Director goes to the Director, not an implementer

The shipped workflow (`examples/workflows/inspection.json`) gains the stage
`user_feedback`, labelled "Director reviewing the User's message". It is owned
by the Director, is not a review, and notifies its assignee. Its transitions:

| action | from → to | who | notes |
|---|---|---|---|
| `user_send_to_director` | user_review → user_feedback | user, owner-scoped | message required; clears nothing |
| `director_return_to_uat` | user_feedback → user_review | director | the steps must be current again |
| `director_send_to_implementer` | user_feedback → in_progress | director | reason; a return, clears Audit and User sign-off |
| `director_route_feedback` | user_feedback → analysis | director | reason |
| `defer`, `cancel` | → backlog, cancelled | director | |

`user_kick_back` is removed. `relay_user_kick_back` is removed with it: the
relay fence requires a relay to mirror a move the User could make, and the
User no longer returns work.

A move out of a review normally needs that review's sign-off, unless it goes to
parking or is a declared review return. A declared review return clears the
commit. Sending the ticket to the Director should do neither, because nothing
has been rejected yet. So `enforce_declared_ticket_update` gains one exemption,
`declared_user_handoff`: a move **out of a User Review stage into a Director
hold** keeps the review's record and passes any blocker. A Director hold is a
non-terminal, non-review stage owned by the Director alone; see
`declared_director_hold` and `workflow_config.director_hold_stage_names`.

The source matters. Analysis and Draft have the same shape as `user_feedback`, so an
exemption keyed only on the destination would also let Audit's work move to
Analysis without Audit's sign-off, or let an implementer's kick-back to Analysis
ignore a blocker. The VM comparison caught the second of these through
`legacy_workflow_equivalence_test`. The Python side, which decides what the
board offers, is scoped the same way. The ticket arrives at the Director with its commit and Audit's sign-off. The
Director's notice is the only one sent; no implementer is told. If the Director
sends the ticket on to implementation, that is a return like any other: Audit
runs again on the next candidate, and the implementer gets one notice.

### The upgrade

pgu1002 creates the tables, functions, triggers and grants. On a declared board
it applies this workflow change only when the stored document has the shipped
shape:

- a User-owned `user_review` review stage carrying `user_kick_back`;
- stages `in_progress`, `analysis`, `backlog` and `cancelled`;
- exactly one control role, named `director`;
- no `user_feedback` yet.

It applies the change through `apply_declared_workflow` as the service, the way
the Director configures a workflow, so the stage is projected and every rule
checked. Every board whose document was configured has the service's
privileges. A board seeded some other way and not yet given rbac.sql's grants
is left unchanged with a NOTICE, rather than failing the whole upgrade. It records
the revision under actor `pgu1002`, and marks the document with
`migrations.user_feedback`. Running it again changes nothing. A board of any
other shape is left alone: its User keeps whatever return it had, and that
board's relay and withdrawal machinery is unchanged.

## Tests

| test | what it covers |
|---|---|
| `tests/uat_instructions_test.py` | Declared and legacy boards. Who may write steps and ask. Blank, long and reconfirm refusals. The guard on every path into User Review. Superseding on Send to Director, return and commit change. A coded ticket, a no-code ticket, a resubmission with changed steps, a legacy ticket. The question notice. |
| `tests/user_feedback_workflow_test.py` | The shipped transitions. Send to Director keeps the commit and Audit's sign-off and gives exactly one notice, the Director's. Every Director decision from `user_feedback`. The upgrade on a shipped-shape board, its idempotence, and that it leaves other shapes alone. |
| `tests/uat_upgrade_parity_test.py` | A board installed from the tree before pgu1002 (the parent of the commit that added it), running that era's shipped workflow, then upgraded the way deploy upgrades: `scripts/ticket-board-migrate` alone, with no rbac.sql. It ends with the same UAT columns, constraints, indexes and triggers as a fresh install, the same function bodies and EXECUTE grants, and the same table grants to the service and the listener. It also gets the same User Review and decision stage as today's shipped document. The per-role SELECT on every table comes from install's rbac.sql sweep, which no migration mirrors, so it is not compared. |
| `tests/uat_panel_browser_test.py` | Chromium, declared and legacy boards. On a ticket with a long body and many comments, the action, the expected result and the controls are visible at the top of an 800px-high viewport without scrolling. Each panel state. Copyable commands. Ask Director, and the reply shown under it. Both confirmations, with where the ticket goes next. |

Neighbour suites that relied on the User's return now run on the earlier
shape. `workflow_document_eras.before_user_feedback` provides that shape, and
`before_relaying` now builds on it. The suites that move tickets into User
Review prepare steps first.
