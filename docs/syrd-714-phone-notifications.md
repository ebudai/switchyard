# SYRD-714: the User's high-value events reach a phone, over Pushover, with private tailnet links

The operator guide is [user-notifications.md](user-notifications.md). This note records the design and the evidence.

## The event source comes first

**The board records the events itself** (migration `pgu1021_syrd714_user_events.sql`; `schema.sql` ends with the
same objects). Triggers write `ticket_board.user_events` in the same transaction as the change that caused each
event, so nothing a crash or an outage does can lose one or invent one.

- **`user_review`:** a ticket enters a human's review. That is a review stage assigned to a role of kind `user`
  (SYRD-582's definition), or, with no declared workflow, `user_review`. Leaving and returning is a new event.
- **`operator_blocker`:** a ticket gains an unresolved `operator:<name>` blocker.
  - `set_blockers` deletes and re-inserts every standing blocker, so a wait is tracked per (ticket, operator) in
    `user_event_operator_waits`.
  - The wait ends only when, at commit, no unresolved blocker names that operator. That check is a deferred
    constraint trigger.
  - So re-setting the list beside another blocker is the same wait, while releasing the blocker and setting it
    again is a new one.
- **`operation_failed`:** a privileged job recorded on a ticket finishes `failed`, `timed_out` or `rolled_back`.

**What a row holds.** A row holds the kind, the ticket and a sanitized detail (the operator, the action or the
stage), never free text. Recording one never fails the change that caused it: an error becomes a warning. Rows older
than 30 days are pruned as new ones arrive. Only the triggers write the table; the service and the listener cannot.

**The one read** is `ticket_board.user_events_after(after, limit)`, which only the board service may execute, served
as `GET /api/user-events`.
- It returns at most 50 events, oldest first.
- Each event carries a stable key (`kind:ticket:id:time`), its short title (120 characters) and whether it still
  stands, plus the latest id.
- It is a read: it never touches `ticket_notification_queue`, so pane delivery cannot be acknowledged, suppressed,
  duplicated or changed by it.
- A cursor that is not a number is a 400 (the route table answers `ValueError` that way), and a board from before
  this change answers 404.

**The Director's ad hoc notification** was added to this slice on the ticket.
- **The action.** `notify_user` is a board action (`scripts/ticket_board/director_notice.py`, `ticket-board-write
  notify-user`).
- **Process-bound.** It is refused unless the request reaches the board's Unix socket under process authority. The
  kernel names the calling process, and it must be the registered pane of the role it acts as. So HTTP, a role
  header, a legacy uid-authority board and a detached shell are all refused.
- **Preview, then confirm.** Without `confirm` it answers the notification exactly as the phone will show it
  (`phone_text.ad_hoc_message`, which the bridge also uses) and a SHA-256 digest of it, and records nothing. With
  the matching `confirm` it calls `record_director_notice`.
- **The database checks again** that the caller is the control role (`role_controls_project`) and that this pid,
  start time and uid are that role's registered pane. It then:
  - records the ledger row (`director_notices`: requester role, pid, start time, uid, generation, text, ticket,
    urgency, reason, digest);
  - holds the rate (5 an hour, 1 urgent with a reason) under an advisory lock;
  - treats the same digest from the same role within ten minutes as the notice already recorded;
  - writes a `director_ad_hoc` user event.
- **Refused text** (`phone_text.ad_hoc_problem`): control and format characters, URLs, credential shapes (a run of
  20 token characters mixing letters and digits, `syrdr1.`, private keys, `password:`-style names), qualified or
  other-prefix ticket references, and a ticket not of this board. The socket handler never holds the HTTP write
  token, so the shape is what catches it, or any 20-character piece of it.
- **The link is what the preview says.** A notice with a ticket links that ticket's page, only while the host serves
  that board; a notice without one previews `link: none` and is sent with no link (Audit's finding on ec679648: the
  bridge had sent the board's front page).
- **The bridge** checks the notice again by recomputing the preview digest from the text it will send. A changed
  notice, text the board should have refused, or a malformed requester is suppressed. It caps one board at 10 an
  hour, records the requester and digest in the outbox, and sends priority 1 only when the operator's
  `director_urgent` is set.
- **Role policy.** `notify_user` joins `OPERATION_ALLOWED_ROLES` (director) and `COMPOSED_OPERATION_CAPABILITIES`
  (the control role's `set_manually_controlled` and `merge`, as `withdraw_signoff`). The client method and the CLI
  command keep `ticket_board_write_client_test`'s parity.
- **File size.** `write_client.py` stays under its 1250-line review limit because one constant was folded onto a
  single line.

**The page opens a linked ticket.** `/#ticket=<ID>` opens that ticket's detail when the page loads, once, and again
when the hash changes (a second notification tapped in an open tab). An ID the board does not hold opens nothing.

**Root's side of the boards** (`board_identity`):
- `unit_board_address` reads, from root's unit alone, the board's loopback port and ticket prefix. A unit that does
  not name exactly `--host 127.0.0.1` and one `--port` is refused.
- The board reader's `get` takes a byte cap.

## One host bridge

`scripts/user_notify.py` is the service, run as `serve` by `switchyard-user-notify.service`, with root's command
`scripts/switchyard-user-notify`. It is one process for every project root has registered.

**Each pass:**
1. **Read.** For each project, in rotating order, it reads at most 20 events after that tenant's cursor. The read
   goes over the board's own socket, proven as SYRD-694 does (`board_identity.board_get_for`), with a 64 KiB cap.
   - Every field is checked. An answer not of the form is that tenant's failed read.
   - An event whose ticket is not that project's prefix, or whose key is not its own, is recorded as suppressed and
     never sent or linked.
   - One tenant's failure never stops the next tenant's read.
2. **Record.** Each event goes into the outbox (`scripts/user_notify_store.py`, root's SQLite file, 0600). It is
   stored as pending or as suppressed, with why:
   - not configured, or disabled;
   - the project or the kind opted out;
   - another operator's blocker;
   - already resolved.

   The cursor moves in the same transaction.
   - A tenant seen for the first time joins at its latest event, so history is never sent.
   - A board whose record was recreated (its latest id below the cursor) is joined again.
3. **Notice an outage.** A board whose unit has `failed`, or that is active and has not answered for
   `outage_minutes`, is one `project_outage` event per outage. A board stopped on purpose is none.
4. **Expire and send.** At most 10 messages go per pass, one per tenant per round, the tenant served least recently
   first.
   - Opt-outs are checked again at send time.
   - The credential is read at each send.
   - An attempt is recorded before the provider is asked (at-least-once).
   - Backoff is 30 s doubling to an hour, at most 8 attempts.
   - Anything unsent a day after it was recorded is dead as expired.

**Messages** (`scripts/user_notify_pushover.py`):
- **Content.** The title is `<project>: <ticket> <label>`. The body is the ticket, its title cut to 80 characters,
  and a fixed action sentence. Control, format and bidirectional characters are removed and whitespace collapsed.
  Pushover gets `html=0`, a 3-day TTL, and priority 1 for an outage only.
- **The link** is `https://<base host>:<board port>/#ticket=<ID>`.
  - The port and prefix come from root's unit, never from the board's answer.
  - It is sent only while `tailscale serve status --json` shows that exact host and port proxied to the board's
    loopback port.
  - It is never sent while Funnel is allowed for that host and port.
  - With Tailscale unavailable, the message goes without a link.
- **The adapter** POSTs over HTTPS to the fixed endpoint, with the credential in the form body only and redirects
  refused.
  - `sent` needs HTTP 200 with `"status": 1` (an integer) and a request UUID.
  - HTTP 429, 5xx, a timeout or a failed connection is a retry.
  - Any other answer, including a 200 without a valid receipt, is dead and never retried, since it may have arrived.
  - Every detail is redacted of both secrets, and of anything shaped like one, before it is stored or logged.

**Configuration and credential** (`scripts/user_notify_setup.py`):
- `config.json` is root's (0644, not secret), read every pass.
- `pushover.json` is root's, readable by nobody else, read through `O_NOFOLLOW`, and refused when it is a link or
  any other account can read or write it.
- `set-credential` reads a descriptor, a prompt or stdin. No option can carry a secret.
- The base URL must be `https://` with a `*.ts.net` name or a tailnet address, and no port, path, query or userinfo.
- Nothing here runs `tailscale serve` or `funnel`: the preview prints the one `serve` command each missing board
  needs.

**The unit.**
- It is `Restart=always`, with `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome` and `PrivateTmp`, and its
  state directory is 0700.
- `install` plans by default. `--apply` writes the unit:
  - a first install runs `enable --now`;
  - a reinstall only runs `try-restart`.
- It never writes the configuration, so an enabled or disabled bridge stays as it was across installs and upgrades.
  It never starts a pane.

**Not done here:**
- the ntfy adapter (the transport boundary is `name` plus `send`);
- any live install, credential, Tailscale or tenant change; the User does those in UAT.

**A supervised board** (SYRD-586) is read only when its unit runs the board, as `board_get_for` requires today. Its
outage is read from the supervisor's `status.json`.

## Tests

**`tests/user_events_board_test.py` (66 checks).** It uses production-built boards with the shipped workflow, the
real server and listener, and runs main first from a git archive of main's scripts, examples and tests.
- **The same history on each tree:**
  - a ticket to User Review, back to the Director and returned;
  - one signed off before it is read;
  - `operator:eric` set, re-set beside another blocker, released and set again;
  - `operator:alice`;
  - privileged jobs failed, timed out and succeeded;
  - comments, routing, submission and a kick-back.
- **Main:** has no read (404) and no record.
- **Here, the events:**
  - exactly two entries to User Review, plus the signed-off one, which no longer stands;
  - eric twice, and alice;
  - two failed jobs;
  - in order, keyed, with the title only. No body or comment text appears.
- **Here, the read:**
  - read again under the same keys;
  - a limit of 500 is capped;
  - `after` and `limit` page;
  - a non-numeric cursor is a 400;
  - the service cannot insert, and the listener cannot call the read.
- **Separation:** the notification queue, the trace and the listener's sends are identical to main's.
- **Upgrade:** main's board, upgraded with this tree's migrations, records from then on.
- **Mirror:** `schema.sql`'s last copy of these objects is the migration's, byte for byte.
- **A no-op rewrite** of a waiting ticket's stage and assignee is not a second entry.
- **Volume, failure and age:** a read returns at most 50 of 74 events; a record that refuses every row does not fail
  the blocker that caused it; events older than 30 days are pruned.
- **The Director's notification**, from this process registered as the Director's pane, over a real Unix socket
  under process authority:
  - over HTTP with a director header: refused;
  - the preview is exact and records nothing, and `ticket-board-write notify-user` previews the same digest;
  - a wrong or stale digest is refused; the confirmed send is recorded once, and confirming again is the duplicate;
  - the ledger names the pane's role, pid, start time and generation;
  - no comment, move or pane notice comes of it;
  - 16 refusals: newline, URL, `www.`, token shape, a 20-character piece of the write token, the whole token,
    `password:`, a qualified and another-prefix reference, another board's ticket, too long, empty, a bidi control,
    an unknown field, a missing ticket, urgent without a reason, a reason without urgency; none is recorded;
  - one urgent an hour, five in all;
  - the events carry their notice and digest;
  - a retired Director process and another role's pane are refused, and the database itself refuses a stale
    process and a non-control role.

**`tests/user_notify_bridge_test.py` (98 checks).**
- **Main:** has no bridge.
- **Sending:**
  - unconfigured tenants join at their latest event and nothing is sent;
  - once configured, each kind is sent once with its exact text and link, and the receipt is recorded;
  - only `GET /api/user-events` is ever asked.
- **Replay and restart:**
  - a restart and a lost cursor still give one message per event;
  - a crash mid-send is retried once after its backoff;
  - a recreated board record is joined again, not replayed.
- **Every provider answer:**
  - success;
  - malformed 200s (status `true`, no or bad receipt, not JSON, oversized);
  - 400, 403 and a redirect are dead;
  - 429, 500, 503, a timeout and a refused connection retry.
- **Backoff:** waits of 30…1920 s, a dead letter after 8 attempts, and expiry after a day.
- **The real urllib POST** against a local server: the answer is read, a redirect is not followed, a stall is cut off.
- **Redaction:** the credential is absent from every log line, status and the outbox files, even when the provider
  echoes it.
- **The credential:**
  - its mode is 0600;
  - a rotation is used at once;
  - a 0644 or linked credential is refused without spending an attempt;
  - `set-credential` reads only a descriptor, and only as root.
- **Hostile input:**
  - a hostile title (newlines, ANSI, bidi, zero-width, markup, 5000 characters) stays one plain line, and the link
    does not move;
  - another project's ticket, a mismatched key, a bad detail and a path-shaped ticket are suppressed;
  - malformed answers are failed reads.
- **Outages:**
  - one board down while the others send;
  - no outage before `outage_minutes`, one high-priority outage after it;
  - a failed unit after recovery is a new outage;
  - a deliberately stopped board is none.
- **Fairness:** a tenant with 60 events does not hold back another's one.
- **Controls:**
  - disable drains and records;
  - enable resumes;
  - kind, project and operator filters, and resolved events, are suppressed;
  - an opt-out made while a message waits holds at send;
  - drain works;
  - the controls are root only;
  - status shows the boundary.
- **Links:**
  - Funnel gets no link;
  - with Tailscale down, the message goes without a link;
  - only an exact Serve proxy counts;
  - bad base URLs are refused, and tailnet names and addresses are accepted;
  - `configure` previews without writing.
- **The unit address:**
  - port and prefix come from root's unit;
  - a public host, two ports, or no or bad prefix is refused;
  - a unit others may write is refused.
- **Install:**
  - a first install runs `enable --now`, a reinstall only `try-restart`;
  - the configuration is untouched, so a disabled bridge stays disabled;
  - the unit carries no secret and starts no pane;
  - root only.
- **The test push:**
  - it is sent while disabled;
  - a failed test is not retried.
- **The Director's notifications through the bridge:**
  - attributed as `<project> Director`, linked to their ticket or their board, with requester and digest in the
    outbox;
  - urgent is high priority only with `director_urgent`;
  - a notice changed after confirmation, refused text, a malformed requester or another board's ticket is recorded,
    never sent;
  - each delivered note matches the board's own preview of it: title, body, and a link exactly when the preview
    names a ticket's page (and none when that board is not served);
  - the kind filter works, and the hourly cap holds.
- **Redaction** covers a rotated-out token and each current secret on its own.

**`tests/ticket_link_browser_test.py` (6 checks).** The real server and page in headless Chromium, main first.
- On main, the link opens nothing.
- Here:
  - the link opens its ticket;
  - a missing, malformed, lowercase or other fragment opens nothing;
  - once closed, the ticket stays closed through a refresh;
  - a second link in an open page opens its ticket;
  - a link to an unknown ticket leaves the open one open.

**`tests/ticket_board_operation_role_policy_test.py`** pins `notify_user` in both tables.

**Mutants.** All 57 are killed: 21 on the board (triggers, rate, duplicate, cap, failure, pruning, process and
control checks, text rules, route and handler), 33 in the bridge, and 3 in the page.
- `L03` (an unknown link) first survived. The new open-ticket check kills it.
- `B22` (a stopped board as an outage) first survived. The stopped-board case now fails first and then waits.
- A Python re-check of the caller's registration was equivalent to the role resolution before it, and the database
  makes that check anyway. It was removed.

**Neighbour sweep, main against this change.** 44 suites:
- **Pass on both:** 37.
- **Fail on both:** 5 fail at the same line on each side, so this change adds no failure to them:
  `board_supervisor_test`, `field_audit_grants_test`, `ticket_board_postgres_triggers_test`,
  `ticket_board_write_api_test` and `write_cli_entry_points_test`.
- **Fixed here:** `ticket_board_operation_role_policy_test` counted 60 operations against its pin of 59; it now
  pins `notify_user`.
- **One commit:** `ticket_board_schema_function_migration_test` wants each commit's schema functions in that
  commit's added migration. It passes on the squashed commit.
