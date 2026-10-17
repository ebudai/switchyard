# SYRD-598: the User's confirmed board-UI action may use the Director's operation permission, for that one action

## What was there

**Over HTTP, a role was whatever the header said.**
- The board's TCP port hands its write token to anyone who asks:
  `GET /api/client-config` returns it, and the page inlines it.
- A write then acted as whatever role the `X-Ticket-Board-Caller-Role` header
  named, `director` included.
- Only the Unix socket took a caller's role from the kernel (process authority).

**So the board UI acted as the Director by saying so.**
- Create, merge, cancel, reassign, Director Edit, publication rejection, the
  gate toggles and the manual-control toggle all sent `director`.
- A "Comment author" picker let the human act as any role, and the declared
  action buttons followed it.
- The record then said `director`, for a decision the User made.

**The database trusted the session's `ticket_board.caller_role`,** which the
board service sets freely.

## What it is now (the Director chose B: narrow TCP, and lend through the UI)

### Over HTTP, a request is the User's

`caller_role()` on TCP admits only `user` (`ui_delegation.http_role`). Any other
role named in the header is refused: "over HTTP a request acts as the User, not
director; role panes act through the board's Unix socket". This covers
director, inspector, audit and every other role.

- **Unchanged:** role panes reach the board through its Unix socket, where the
  kernel says who is calling, exactly as before. Report-token filing is
  untouched.
- **The test flag.** `ui_delegation.TRUST_HEADER_ROLES` exists for in-process
  test fixtures only. The shared board fixture
  (`tests/ticket_board_write_api_test.py`) sets it, so the existing suites can
  keep naming a role in a header as stand-ins for socket panes. No request,
  environment variable or configuration can set it.
  `tests/board_ui_delegation_test.py` turns it off and proves the production
  refusal.

### A Director operation is the User's, confirmed, under a grant

1. **Describe** (`POST /api/ui/delegations {describe, ticket_id}`). The server
   answers what may be offered for the ticket now: the User's own declared
   actions (done directly), and the control role's lendable operations, each
   with its payload contract. It computes this from the live workflow (or a
   legacy board's `workflow_transitions` and stage owners) and
   `ticket_board.ui_delegable`, so the page keeps no table of its own.
2. **Prepare** (`{prepare}`). The ticket as it is now (ID, title, state,
   assignee, and both tickets for a merge), plus the version a grant will be
   bound to. The page shows exactly this in its confirmation.
3. **Mint**. The User confirmed; `ticket_board.mint_ui_delegation` stores a
   single-use grant. It is minted only by the board service, only with caller
   `user`, and only over HTTP with:
   - this server's Host and Origin, and Sec-Fetch-Site same-origin when sent;
   - the page's session cookie (HttpOnly, SameSite=Strict, issued with the
     page, kept in server memory);
   - the write token.

   The grant binds:
   - sha256 digests of the token and the session;
   - the ticket and the operation;
   - the exact payload;
   - the ticket version: a digest of the row, its blockers, its awaiting role
     and the workflow revision;
   - an expiry of two minutes (the database caps it at five).
4. **Act**. The action's own request carries `X-Ticket-Board-UI-Delegation`.
   The grant is consumed in the first transaction the action writes as `user`,
   before the action runs (`ticket_board.consume_ui_delegation`). That refuses:
   - an unknown token, another session, replay, expiry;
   - another ticket or operation, an altered payload;
   - a stale ticket, or an operation no longer lendable.

   The Director-only checks then accept the User only for the operation the
   grant consumed **in this same transaction** names
   (`ticket_board.ui_delegated_authority`):
   - `require_actor` and `require_workflow_transition_actor`;
   - `reassign` (and the declared update rule, through `workflow_actor`);
   - stalled-ticket recovery, publication control, `withdraw_signoff`;
   - merge's Python check.

   A delegated declared transition runs as `perform_workflow_action_as(control
   role, narrated by user)`.

Before any of that, when the action's request arrives, the server checks
read-only that the token names a grant for exactly this ticket and operation,
and that the board still lends that operation. Some operations act before
their first write (a git measurement, a file), so a grant for one action must
never open another, even for the moment before its transaction refuses it.
Session, use, expiry, payload and staleness are checked where the grant is
consumed.

Every request starts with no confirmation pending (`begin_request`, first in
`do_GET` and `do_POST`). The board speaks HTTP/1.1 through a threading server,
so one thread serves every request on a kept-alive connection. Without the
reset, a confirmation that one request left unspent (refused before it wrote
anything) would be spent by the next request on that connection, in whatever
transaction first wrote as the User.

Changing only the HTTP layer gets nowhere: without a consumed grant the
database refuses the User exactly as before.

### What may be lent, and what never

- **Lendable:**
  - route and reassign;
  - set and clear blockers, and release an external blocker;
  - hold and release manual control;
  - recover a stalled ticket;
  - dismiss that ticket's notifications;
  - resolve that ticket's publication requests;
  - cancel, defer and mark done where the workflow permits;
  - declared transitions available to the control role that are not
    approvals, relays or commit-carrying (a commit is measured in a
    transaction of its own first, SYRD-541);
  - merge, bound to its source and destination;
  - `withdraw_signoff`.
- **Never lendable:**
  - force_move and override_move;
  - raising or manufacturing any sign-off;
  - the review gates and `commit_exempt`;
  - generic Director Edit;
  - size-review approval;
  - workflow configuration;
  - every host, root, polkit, runtime or release control;
  - a Director relay of another role's decision.

### Sign-off withdrawal, purpose-specific

`ticket_board.withdraw_signoff(ticket, signoff, reason)` lowers exactly one
named sign-off that is currently given, with a reason, recorded as "Withdrew
<sign-off>: <reason>".

- Only the control role, or the User under a grant, may call it.
- On a declared board it writes through a one-statement trigger window. That
  window lets only that sign-off fall: no stage, owner or other flag moves, and
  nothing is raised.

### The record

The action runs with caller role `user`, so the record names the User:
- **Comments and audits:** every comment and field audit.
- **Notices:** "changed by user (through the board UI, using the control role's
  operation permission)".
- **History:** consuming a grant appends "Through the board UI the User
  confirmed <op> <payload>, using the operation permission of director. The
  decision is the User's."
- **Journal:** each grant row records decided_by `user`, the authority lent,
  and when it was used.

### The page

- **No role.** It asserts no role; comments, attachments, crops and field edits
  are the User's.
- **One panel.** It lists what the server describes. Manual control, the
  blocker controls and merge use the same confirmation.
- **The dialog.** It asks for the fields, then confirms the concrete ticket,
  action and values. A refusal shows the board's reason and reloads the board;
  a cancelled dialog mints nothing.
- **Removed:** the role picker, Director Edit, the gate toggles (and the create
  form's gate inputs), the commit_exempt toggle, the inspector and audit
  sign-off toggles and kick-back buttons, and the legacy Advance button.

### Clients

`ticket-board-write withdraw-signoff <id> --signoff <name> --reason <why>` (and
`TicketBoardWriteClient.withdraw_signoff`) is the Director pane's way to the
same operation, over its Unix socket. A client that names a role over HTTP is
refused by the server with the message above.

## Tests

- `tests/board_ui_delegation_test.py`: a production-built board (schema,
  migrations, rbac), the real app, HTTP server and Unix-socket server, on a
  declared and a legacy board, against a git-archive run of main. Forged role
  headers are refused here and accepted on main. The suite covers each refusal
  (origin, host, session, cross-site, replay, expiry, stale ticket, stale
  version, restart, another ticket, operation or payload, a grant used for a
  different operation before it runs, a move the ticket's stage no longer
  offers, minting without the version shown, a confirmation left unspent on a
  kept-alive connection, unlendable operations), each lent
  operation done, the record, sign-off withdrawal and merge. Its database cases
  call the functions directly: same-transaction binding, a grant lent to no
  other caller the transaction turns into, grantees, the lendable map, a
  control-role approval or relay never lendable (planted and rolled back,
  since the validator forbids sign-off authority only to a role literally
  named director), a withdrawal window that raises nothing, and that a fresh
  schema equals a migrated one.
- `tests/board_ui_delegation_browser_test.py`: Chromium on the real page
  against that board, on both kinds. It checks the controls the server
  describes, the two-step confirmation, a cancelled dialog, a ticket that
  changed under the dialog (refused, reloaded, then done), merge with both
  tickets, a User's own edit, and a blocker added and the last one removed.
- Browser suites whose board is an in-memory fake answer the page's
  describe, prepare and mint through `tests/board_ui_stub.py`, which checks
  that the confirmed action carries exactly the granted token.
- Suites that stand in for a role's socket pane over HTTP set
  `ui_delegation.TRUST_HEADER_ROLES` (the shared write-API fixture does);
  suites that covered the removed page controls now cover their absence and
  the refusal.
