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

### Who the User is: the kernel's account of the connection

Headers cannot say a person is at the page. GET / and /api/client-config hand
the write token to any TCP client, and Host, Origin and Sec-Fetch-Site are
whatever the client sends. Audit's finding showed it: a Python client could
fetch the page, copy its cookie, invent the headers, and mint and spend
grants.

What a client cannot choose is the account that owns its end of the
connection. The human's browser runs as the human's own desktop account (on
this host `eric`, outside the roles group), and every agent runs as the
project account or a role account. The kernel records the owning uid of
each socket in /proc/net/tcp(6), and the board shares the host's network
namespace, so `ui_user_identity.tcp_peer_uid` reads who is on the other end.

- **Per request:** the session cookie is issued with the page only to an
  approved board UI user's connection. Prepare, mint and every delegated action
  re-check the connection's account, so a cookie or grant carried to another
  account's connection is refused, and the grant stays unspent.
- **Fails closed:** the board refuses when the client or the server end is not
  loopback, when /proc/net/tcp(6) cannot be read, or when the kernel names no
  owner or more than one. A proxied connection is owned by the proxy, not a
  board UI user.
- **Describe stays open.** The User's own declared actions are offered to any
  page. Lent operations are offered only to an approved user's connection;
  any other page is told why ("Director operations: ...").

### Who decides who the User is: root's record

The Director's decision (A): trusted UI principals come from root
provisioning, recorded where only root writes.

**The record.** Root records them in the project's registry record,
`/etc/switchyard/projects/<slug>.json`, under the key `board_ui`
(`scripts/board_ui_record.py`). Each user is recorded by account name and by
the numeric uid root resolved at the time. Root's exclusions are recorded
beside them: the project account, the roles group, the role accounts and the
board service.

**Who is recorded.**
- On a desktop host: the host's approved desktop owner. This is `gui_user`
  from the host's root-owned desktop approval (`approved_desktop_policy`),
  never the tenant's own configuration. A tenant configuration that names
  another desktop records nobody, and says why.
- On a headless host, or to choose another account: the operator names it
  with `switchyard new --board-ui-user ACCOUNT` or
  `switchyard upgrade --board-ui-user ACCOUNT` (repeatable).
- Otherwise nobody is recorded, and the board UI lends nothing.

**Refused, never broadened.**
- Root, the board service, the project account and every role account are
  never recorded, even when named.
- An account that does not exist is refused the same way.
- A refusal leaves the record as it was and is reported. `new` and `upgrade`
  go on: delegation simply stays as it was, or stays off.

**Each decision is journalled.**
- It is written to the project's rollout journal before the record is
  replaced. If the journal cannot hold it, nothing is recorded.
- An unchanged decision rewrites and journals nothing.
- An unprivileged run, such as a test, journals only where it is told to and
  never reaches for root's journal.

**Existing tenants** get the record only through an operator-run
`switchyard upgrade`. Until then their board says "an operator must configure
an approved board UI user (switchyard upgrade records the desktop owner; a
headless host names one with --board-ui-user)".

**The board reads it from its command line.**
- The unit root renders runs `ticket-board.py ... --board-ui-record
  /etc/switchyard/projects/<slug>.json`.
- The path is not read from the environment: systemd lets the owner's
  `EnvironmentFile=` override the unit's `Environment=`, and that file is the
  tenant's.
- The board trusts the record only while it, and every directory above it, is
  owned by root, is not a symbolic link, and is not writable by group or
  others.

**Recorded is not enough.** The board refuses the whole record, and lends
nothing, in any of these cases:
- a recorded account no longer has the uid root recorded;
- an entry is malformed;
- the record names an excluded account. The board excludes root, its own
  account, and the project and role accounts as root recorded them. Its
  environment (`TICKET_BOARD_TENANT_USER`, `TICKET_BOARD_ROLE_ACCOUNTS`,
  `TICKET_BOARD_SOCKET_GROUP`) may only add to those exclusions.

### Router (SYRD-599)

SYRD-599's board-UI Router controls depended on the role picker, which is
gone.
- **Router's writes** (assign an Implementation owner, return to triage) are
  Router's own, made from its pane through the board's Unix socket. The page
  offers neither.
- **The Director's two routes** into Implementation are what describe offers
  for a triage ticket where Router is active, each confirmed by the User:
  - **Route through Router** names no owner, and Router chooses.
  - **Assign directly** requires one of the active Implementation owners.

### Out of scope, reported on the ticket

The live board's release directory and its `EnvironmentFile`
(`~/.config/syrd/ticket-board.env`) are owned by the account every agent pane
runs as. An agent could therefore change what the board service runs. The
board then refuses a record it does not find root-owned, but that release
ownership is a separate fix. It is recorded on the ticket for the Director.

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
   `user`, and only over HTTP from a board UI user's connection with:
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
   grant consumed **in this same transaction** names, and only for its ticket:
   - The checks that know the ticket ask `ticket_board.ui_delegated_authority`
     with it: `require_workflow_transition_actor` (each transition function
     checks its own action on its own ticket), `reassign` (and the declared
     update rule, through `workflow_actor`), stalled-ticket recovery and
     `withdraw_signoff`.
   - `require_actor` knows neither the ticket nor which function called it,
     and other functions check the same action names (`approve_size_exception`
     and `enable_size_review` check `merge`; `snooze_reminders` and
     `clear_reminder_snooze` check `set_manually_controlled`). So it lends only
     to a grant the operation's own function entered: immediately before its
     check, the function calls `ticket_board.enter_ui_delegation(operation,
     its ticket, ...)`, which refuses a grant for another ticket and marks the
     grant entered on its row, and immediately after, it calls
     `leave_ui_delegation()`. `require_actor` asks `ui_entered_authority`. The
     board service cannot call either function; only the operations' own
     functions do. The functions that enter are `set_blockers`,
     `release_external_blocker` (whose grant covers its `set_blockers` check;
     a `set_blockers` grant enters only `set_blockers`), `set_manually_controlled`,
     `merge` (which also checks its destination against the grant's
     `target_id`), `dismiss_notification`, `dismiss_notification_by_key`,
     `recover_stalled_ticket` and `require_publication_control` (called only by
     `resolve_publication`, whose request the trigger below binds). Their copies
     are in the same migration, after the authority checks.
   - Merge's Python check.

   A delegated declared transition runs as `perform_workflow_action_as(control
   role, narrated by user)`.

   `require_actor` and publication control are told the operation, not the
   object, and two lent operations act on an object the payload names: a
   notice, and a publication request. In the same transaction, each of those
   is checked against the grant that lends it. `ticket_board.ui_delegation_for`
   (which `enter_ui_delegation` uses) finds the grant consumed in this
   transaction for exactly the operation; when there is one, the object must
   belong to the grant's ticket ("notification 5 is
   PGU-2's, not the ticket this board UI confirmation was for"), and the
   caller then compares the object with the grant's stored payload, so it must
   be the very object confirmed:
   - `dismiss_notification`, which `dismiss_notification_by_key` also goes
     through: the notice's id equals the grant's `notification_id`, or its
     ticket, target role and kind equal the grant's key, normalised as
     `dismiss_notification_by_key` normalises them (ticket upper-cased, role
     and kind lower-cased and trimmed, kind defaulting to `transition`).
     Another notice on the same ticket is refused: "notification 6 is not the
     notice this board UI confirmation was for".
   - a publication request, by a BEFORE UPDATE trigger, whichever function
     writes it: the request's id equals the grant's `request_id`, and the
     state it is decided to equals the grant's `outcome`. A grant to reject
     one request decides no other request, and does not record that one
     published.
   (A trigger rather than a copy of `resolve_publication` also keeps the
   migration from growing further.) For every
   other lent operation the object is the ticket itself, which the grant
   already binds.
   A notice is named one way only: by
   `notification_id`, or by `ticket_id`, `target_role` and `kind`. The mint
   refuses a payload naming both or neither, and the server refuses both for
   every caller, so the notice the mint checked is the notice dismissed.

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

### One migration, over the size review's threshold

The database side is one migration, `pgu999_syrd598_board_ui_delegations.sql`
(1585 lines), installed atomically: the grants, the checks that lend them and
every operation that enters one. That is over the size review's 1250-line
threshold. Splitting it is not possible: migration numbers from 920 up must be
unique, and 999 is the last number the runner's name-ordered sort applies last
(SYRD-617 takes numbering past it). By Director decision, the candidate is
submitted with its crossing finding open, for a bounded exception covering this
one write-once file. It does not cover any other growth.

## Tests

- `tests/board_ui_delegation_test.py`: a production-built board (schema,
  migrations, rbac), the real app, HTTP server and Unix-socket server, on a
  declared and a legacy board, against a git-archive run of main. Forged role
  headers are refused here and accepted on main. The suite covers each refusal
  (origin, host, session, cross-site, replay, expiry, stale ticket, stale
  version, restart, another ticket, operation or payload, a grant used for a
  different operation before it runs, a move the ticket's stage no longer
  offers, minting without the version shown, a confirmation left unspent on a
  kept-alive connection, unlendable operations, a notice named both ways,
  at the mint and on the Director's own pane), each lent operation done, a
  dismissal through the page that leaves another ticket's notice in place,
  the record, sign-off withdrawal and merge. Its database cases
  call the functions directly: same-transaction binding, a grant lent to no
  other caller the transaction turns into, grantees, the lendable map, a
  control-role approval or relay never lendable (planted and rolled back,
  since the validator forbids sign-off authority only to a role literally
  named director), a withdrawal window that raises nothing, and that a fresh
  schema equals a migrated one. Grants for PGU-1 cannot dismiss PGU-2's
  notice, another PGU-1 notice than the one confirmed (another id, role or
  kind), or resolve PGU-2's request, PGU-1's other request, or the confirmed
  request to another outcome; every refused object is still queued or
  requested afterwards, and the confirmed notice (by id, and by an
  unnormalised key) and request still work. A grant for PGU-1 cannot hold,
  block, release, recover or merge PGU-2, merge PGU-1 into a ticket other than
  the one confirmed, or lend its permission to `approve_size_exception`,
  `enable_size_review`, `snooze_reminders` or `clear_reminder_snooze`; a
  release grant does not lend `set_blockers`. The tickets are unchanged
  afterwards, and each operation still works on its own ticket (recovery
  reaches its own check).
- Identity cases in the same suite run a stdlib-only scripted client
  (GET /, describe, prepare, mint, act) under real, distinct kernel uids:
  `unshare --map-auto --map-root-user setpriv` gives it a mapped subuid.
  - Run as the recorded board UI user, it completes a Director operation.
  - Run as another account, it gets no session and is offered nothing, and
    every prepare, mint and action is refused, even carrying the User's own
    cookie and an unspent grant, which stays unspent.
  - The record is refused whole when it names the project account, root or
    a role account; when an account no longer has its recorded uid; when
    others can write it or root does not own it; or when the board was given
    none. Each case is answered with the operator's remedy.
  - The kernel's answer: one loopback owner, or nothing for a connection off
    loopback (even with a matching row), an unreadable table or two owners.
- `tests/board_ui_record_test.py`: what root records.
  - The desktop owner, by name and uid, with root's exclusions.
  - Only its key, written atomically at 0644.
  - The journal entry.
  - The tenant's own desktop refused, and the operator's account recorded.
  - Excluded and missing accounts refused, with the record unchanged.
  - Dry runs, unchanged decisions, an unwritable record, a journal that
    cannot hold the decision, documents that are not registry records, and
    unregistered projects.
  - The rendered unit's command line, and the real `run_server` reading it.
  - A symlinked record and the board's own account refused.
  - Upgrade's U3 handing on the operator's accounts.
- `tests/router_role_browser_test.py`: SYRD-599's page.
  - A ticket Router holds offers none of Router's writes.
  - A triage ticket offers Route through Router and Assign directly, each
    confirmed and done under a grant decided by the User.
- `tests/board_ui_delegation_browser_test.py`: Chromium on the real page
  against that board, on both kinds. It checks the controls the server
  describes, the two-step confirmation, a cancelled dialog, a ticket that
  changed under the dialog (refused, reloaded, then done), merge with both
  tickets, a User's own edit, and a blocker added and the last one removed.
- Browser suites whose board is an in-memory fake answer the page's
  describe, prepare and mint through `tests/board_ui_stub.py`, which checks
  that the confirmed action carries exactly the granted token.
- The boundary suites that pin `switchyard new`/`upgrade`'s parsers,
  dispatch, phases and the board unit were regenerated with their own
  `run()`. Each records that it differs only by `--board-ui-user` and
  `board_ui_users`, given in the every-option cases. The board unit differs
  only by the `--board-ui-record` argument: undoing it, and SYRD-552's lines,
  gives the baseline's unit to the byte.
- Suites that stand in for a role's socket pane over HTTP set
  `ui_delegation.TRUST_HEADER_ROLES` (the shared write-API fixture does);
  suites that covered the removed page controls now cover their absence and
  the refusal.
