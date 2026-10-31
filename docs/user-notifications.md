# Phone notifications for the User

Switchyard can send a few high-value board events to the operator's phone through [Pushover](https://pushover.net).
Each notification links back to the ticket on that board's own page, over the tailnet (Tailscale Serve). It is off
until you turn it on.

## What is sent, and what is not

**Sent** (each kind can be turned off):

| Kind | When |
| --- | --- |
| `user_review` | A ticket enters User Review and needs you. |
| `operator_blocker` | A ticket gains an unresolved `operator:<you>` blocker. Another operator's blocker is not sent. |
| `operation_failed` | A privileged host operation recorded on a ticket fails, times out or is rolled back. |
| `project_outage` | A board's service has failed, or has been running without answering for `outage_minutes` (15 by default). Sent at high priority. A board stopped on purpose is not an outage. |
| `director_ad_hoc` | The Director sends you a short note of its own (see "The Director's notifications"). |

**Not sent:** worker assignments, pane nudges, reminder snoozes, routine comments, test progress, and every other
transition.

**Each notification carries:**
- the project;
- the ticket ID;
- the ticket's title, cut short to one line;
- the kind of event;
- a fixed sentence saying what is waiting.

It never carries the ticket's body or comments, a shell command, a credential, a write token, a provider transcript
or an attachment.

## What a notification is not

- **Advisory only.** Receiving, opening or dismissing a notification signs nothing off, acknowledges nothing and
  authorizes nothing. You act on the board, as before.
- **The link is not a login.** It opens the board's own page. The board decides what you may do there:
  - over the tailnet you act as the User, exactly as anyone who reaches the board page does;
  - the board lends no Director operation over the tailnet, because that connection is not your desktop account's.
- **No write token in the link.** No link ever contains a write token.
- **Private only.** Links go only to a tailnet host (`*.ts.net`, or a tailnet address), so who can open them is
  whatever your Tailscale ACLs allow. A board that Tailscale Funnel would publish to the internet gets no link, and
  nothing here ever turns Funnel on.

## The Director's notifications

The Director can send you a short note through the same bridge:

```
ticket-board-write notify-user --title "Deploy finished" --message "The release is live." [--ticket SYRD-12]
```

- **Preview first.** The command first answers with a preview, exactly as your phone will show it, and its digest.
  Nothing is sent until the Director runs it again with `--confirm <digest>`.
- **Labelled as the Director's.** Your phone shows it as "<project> Director: <title>".
- **Only the Director's own live pane can send one.** The board checks the calling process: it must be the control
  role's registered pane, reaching the board's socket. A retired or replaced Director process, another role, or a
  request over the board's web port is refused. The ledger records which process sent it.
- **No link but a ticket.** The Director never sees the Pushover credential and cannot choose a recipient or supply
  a link. The bridge links only to the ticket the Director named, on that board. A note without a ticket has no
  link, exactly as its preview says.
- **Refused text.** The board refuses, rather than cleans, text with:
  - a line break or control character;
  - a URL;
  - anything shaped like a credential or token;
  - another board's ticket. Even a bare ID like `OTTO-5` counts, so write `UTF8`, not `UTF-8`.
- **Rate limits.** At most 5 an hour per board, and at most 1 urgent (`--urgent --reason "..."`). The bridge also
  stops sending past 10 an hour from one board.
- **Urgent needs your say-so.** An urgent note reaches you at high priority, past your phone's quiet hours, only if
  you allow it: `"director_urgent": true` in the configuration. Otherwise it arrives at normal priority.
- **Advisory, like the rest.** It writes no comment, moves no ticket, clears no blocker and acknowledges nothing.

Turn them off with `--event director_ad_hoc=off`.

## How it works

There is one host service for every tenant board, `switchyard-user-notify.service`, running as root. It is not one
daemon per tenant.

1. **Each board records its own User events.** It does this in its database, in the same transaction as the change
   that caused the event (migration `pgu1021`). A board never waits on the phone: if Pushover or Tailscale is down,
   board work carries on, and messages wait in the outbox.
2. **The service reads the boards.** Every 30 seconds it reads each registered board's `GET /api/user-events`. It
   connects over the board's own socket, which root's unit names. It reads nothing else, and it never touches the
   role panes' notifications.
3. **Events go into a durable outbox.** The outbox is root's, at `/var/lib/switchyard/user-notify/outbox.sqlite3`.
   Each event is stored under a stable key, so a restart or a repeated read never sends anything twice.
4. **The service sends what is due**, one tenant at a time in turn, so a busy board cannot hold up another.

### Delivery

- **Success needs a valid receipt.** A message counts as sent only when Pushover answers with one.
- **Retried:** HTTP 429, a server error, a timeout or no connection. Retries wait 30 s, then double each time up to
  an hour, for at most 8 attempts.
- **Dead letters** are listed by `status` and never retried:
  - a refusal (a bad credential, a rejected message);
  - an unexpected redirect;
  - a "success" without a valid receipt, since that message may have arrived;
  - anything still unsent a day after the bridge recorded it.
- **A crash can repeat a message.** If the service is killed in the middle of a send, that message is retried after
  it restarts, so the phone may get it twice. It is never lost.

## Setting it up

Every command runs as root. The program is `/opt/switchyard/current/scripts/switchyard-user-notify`; the examples
below call it `notify`. On its own, each change command only previews; add `--apply` to make the change.

1. **Install the service** (it starts disabled and sends nothing):

   ```
   sudo notify install            # shows the plan
   sudo notify install --apply
   ```

2. **Store the Pushover credential.** In Pushover, create an application to get its API token, and copy your user
   key. Then:

   ```
   sudo notify set-credential     # prompts for the token, then the user key
   ```

   You can also pass two lines on standard input, or `--from-fd N`. The command never takes the credential as an
   argument.
   - It is written to `/etc/switchyard/user-notify/pushover.json`, readable by root only.
   - To rotate it, run the same command again. The next message uses the new credential, with no restart.

3. **Serve each board on the tailnet.** The configuration preview prints the one command each board still needs,
   for example:

   ```
   tailscale serve --bg --https=23326 http://127.0.0.1:23326
   ```

   Use `serve`, never `funnel`.

4. **Configure** your operator name (as `operator:<name>` blockers write it) and the tailnet base URL, with no port:

   ```
   sudo notify configure --operator eric --base-url https://<machine>.<tailnet>.ts.net
   sudo notify configure --operator eric --base-url https://<machine>.<tailnet>.ts.net --apply
   ```

   - `--event KIND=on|off` turns a kind on or off.
   - `--project SLUG=on|off` opts a board in or out.
   - `--outage-minutes N` sets how long a running board may stay silent before it counts as an outage (5–1440).
   - The preview prints the configuration and, for each board, whether its private link is served.

5. **Test**, then **enable**:

   ```
   sudo notify test               # sends one test notification now, even while disabled
   sudo notify enable
   ```

## Day to day

| Command | Does |
| --- | --- |
| `sudo notify status` | Configuration, whether a credential is present (never the credential itself), each tenant's cursor and health, link status, outbox counts and the latest dead letters. |
| `sudo notify disable` | Stops sending. Anything pending is drained, and events that arrive while disabled are recorded but never sent later. |
| `sudo notify enable` | Starts sending again, from new events on. |
| `sudo notify drain` | Drops every pending message without sending it, for example after an outage left a backlog. |

The service logs one JSON line per action to the journal (`journalctl -u switchyard-user-notify`). A line never
contains the credential or a ticket's text.

## Upgrades

- **Configuration is kept.** Installing or upgrading Switchyard never writes the configuration, so an enabled bridge
  stays enabled and a disabled one stays disabled.
- **Restarting on the new release.** After an upgrade, run `sudo notify install --apply`. It restarts the service on
  the new release only if it was running, and changes nothing else. It never starts a tenant's panes.
- **Boards running an older release** are listed in `status` as predating user events, until they are deployed with
  a release that has them.

## Adding another provider

A transport has a `name` and `send(message, credential)`, and returns `sent`, `retry` or `dead`
(`scripts/user_notify_pushover.py`). A self-hosted ntfy adapter would implement the same two, and be added to
`PROVIDERS` in `scripts/user_notify_setup.py`.

## UAT

You need: the release with this change installed and deployed to the board you test with, the Pushover app on your
phone, and the phone on your tailnet.

1. **Set it up.** Follow "Setting it up", steps 1–4.
2. **One test push.**
   - Run `sudo notify test`.
   - Within a few seconds your phone shows "switchyard: test notification".
   - The terminal prints `test sent` and a receipt.
3. **One Director notification** (optional). Ask the Director to send one with `ticket-board-write notify-user`.
   It arrives as "<project> Director: ...".
4. **One User Review event.**
   - Run `sudo notify enable`.
   - Ask the Director for a throwaway ticket, moved into User Review.
   - Within about a minute your phone shows "<project>: <ticket> needs your review", with the ticket's title.
   - Tap "Open <ticket>". The board opens over the tailnet with that ticket's details showing.
   - Nothing on the ticket has changed because you received or opened the notification.
5. **Tidy up.** Ask the Director to cancel the throwaway ticket. If you do not want notifications yet, run
   `sudo notify disable`.

**Pass:** both notifications arrived, the link opened the right ticket on your board over the tailnet, and
`sudo notify status` shows two sent messages and no dead letters.
