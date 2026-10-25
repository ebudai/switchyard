# SYRD-669: a page kept open across a board restart keeps its User actions

## What happened

A board process mints two things for its pages when it starts, and a restart
replaces both under a page that stays open:

- **its write token,** which the page holds as `window.TICKET_BOARD_WRITE_TOKEN`
  and `/api/client-config` hands out;
- **its board UI sessions** (SYRD-598), which it keeps in memory and issues
  only with the page itself (`GET /`), as an HttpOnly cookie. A lent operation's
  preparation and grant need one, and each grant is bound to the session it was
  minted under.

The page's ticket writes went through `postTicketAction`. Refused for a stale
token, it asked `/api/client-config` for the new one and sent the write once
more. Its board UI requests went through `boardUiRequest`, which posted directly
and did neither. After a restart, describing a ticket (`/api/ui/delegations
{describe}`) was refused, so the UAT panel dropped Accept and Send to Director,
and the generic panel dropped the Director operations the board lends the User.
A lent operation's own request was sent with `retriedToken: true`, so it was
never retried either.

The Director's second finding, a 400 "missing X-Ticket-Board-Caller-Role", does
not reproduce from this code. Since SYRD-598, `ticketWriteHeaders()` sets the
caller role to `user` whatever it is passed, and the live release contains
SYRD-598. A page loaded before SYRD-598, or a request built without the page's
headers, sends no caller role. The regression checks the header on every write a
fresh page sends, and a mutant that drops it reproduces the 400 exactly.

## The fix

### One write path in the page

`sendBoardWrite(path, payload, options)` sends every write the page makes. It
posts as `user`, with the current token and same-origin credentials:

- refused for a stale token, it asks `/api/client-config` once;
- it sends the request once more only if the token changed;
- anything else refused throws, marked as the board's refusal.

`postTicketAction` keeps its own policy on top: the new-build check before a
write, and reload-and-replay when a write is refused on a new build.
`boardUiRequest` is `sendBoardWrite('/api/ui/delegations', body)`. The UAT
module and the generic delegated-action panel describe, prepare and mint through
it, so neither holds a token policy of its own.

`/api/client-config` also reports the build, so the existing full refresh on a
new build is unchanged. A write that meets a new build behind the same token
still reloads the page and is sent after the reload.

### A lent operation's grant is minted for each try

A delegated request takes its grant from a function, `options.delegation`.
`sendBoardWrite` calls it for the first try and again for the second. A grant
minted by the old process is bound to a session the new process never issued, so
resending it would be refused. The second try mints a new grant under the new
session, and only that grant is spent. The first is never consumed: a request
with a stale token is refused before anything reads its grant, and that grant's
session is gone.

### `/api/client-config` renews a lost session

When a request to `/api/client-config` carries no session this process issued,
the response sets one (`ui_delegation.renew_session`). The conditions are
`issue_session`'s own: over HTTP, from a connection the kernel says a board UI
user owns. This is what the page would get by reloading. Anyone who can fetch
`/api/client-config` can fetch the page too, so it grants nothing new, and every
other check on a confirmation still holds: same origin, the User's connection,
the write token, and a single-use, payload-bound grant. A request that already
holds a valid session gets no new one.

## Tests

`tests/board_write_token_rotation_browser_test.py` runs the production-built
board against a real board process, started as a child process. A restart kills
that process and starts a new one on the same port with a new token, while
Chromium keeps the real page open. Main runs first from a git archive.

- **Main:**
  - a fresh page offers both decisions;
  - after a restart the panel says "Accept and Send to Director are unavailable:
    missing or invalid X-Ticket-Board-Write-Token", having asked only with the
    old token;
  - a lent operation's confirmation is refused.
- **This tree:**
  - every write the page sends names the User as its caller;
  - after a restart, each description refused with the old token is asked once
    more with the new one. The redrawn panel offers Accept and Send to Director
    without a reload, and the UAT message and the comment draft are kept;
  - a restart between discovery and Accept: one refused try, then the new token;
  - a lent operation after a restart: prepared with the new token and session,
    minted, done;
  - a restart between a lent operation's grant and its request: refused once, a
    second grant minted under a second session and used. The database shows two
    grants, two sessions, one used;
  - a new build behind a restart: the token is recovered and the page is still
    left to refresh. A write that meets a new build behind the same token, or is
    refused on a new build, reloads the page and is sent once after it, as the
    User;
  - a token still refused after the refresh is not tried a third time;
  - no token appears in the page's storage or console, and every write goes to
    the board's own origin.

The board's own `loadBoard`, asked by an event as a board process stops, can
fail with "Failed to fetch" while no board is listening. That is existing page
behaviour, and the test exempts exactly that error.

### A neighbour test's timing

`tests/manual_control_toggle_browser_test.py` checked the Manual control
toggle the moment the refusal's status line appeared. The page shows the
refusal, reloads the board, then puts the toggle back, in that order on main
too. A refused board UI request now asks `/api/client-config` first, because
every 403 counts as a possible stale token, as it already did for ticket writes.
That stretches the moment between the status line and the reset from about a
millisecond to about forty. The test now waits for the toggle to be put back
and checks it stays so. It still fails against a page that reports the refusal
but leaves the toggle checked.
