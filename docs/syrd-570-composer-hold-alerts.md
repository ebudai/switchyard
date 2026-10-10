# SYRD-570: the Director hears when a composer holds a role's notices

## What otto saw (report item 1, SYRD-550)

Hermes redraw residue made idle panes' composers look typed into. The anti-clobber
gate rightly refused to type over what might be a person's half-written line, and
held OTTO-71's and OTTO-79's notices as `pane busy` for hours, while each
runtime's own trusted hook said its turn was over. Nobody was told.

SYRD-550 stopped that residue reading as input. Any other ambiguous or genuinely
stuck composer could still hold a notice indefinitely, silently.

## The hold stays exactly as it was

Nothing here submits into a composer, clears it, sends a redraw or any other key,
kills anything, or calls a notice delivered. A person may be writing there: the
alert is information, not permission to type over it.

## Composer holds

A gate deferral is a composer hold when both are true:
- its reason is the composer's: `human_composing` (the composer looks typed into)
  or `cursor_state_unavailable` (the cursor cannot be read to tell);
- the role's hook state is `idle` from a trusted source
  (`pane_activity_gate.TRUSTED_IDLE_SOURCES`): the runtime's own word that the turn
  is over.

`NotificationActivityHold._activity_trace` marks such a trace
(`ActivityTrace.trusted_idle`), reading the hook state from the gate's own store.
The dispatch's diagnostic detail carries the mark, and the ledger, which already
sees every gate deferral (as SYRD-557 uses it), notes it through `composer_hold`.
`notify_listener.py` is unchanged.

## Episodes (pgu983)

`ticket_board.composer_hold_episodes` keeps one open episode per role.
`note_composer_hold(notification, reason, composer, alert_after)` is called for
every gate deferral:
- **A composer hold** opens the role's episode, or joins it with that notice and
  ticket.
- **Alert.** Once the episode is older than the threshold, one `ticket_update`
  goes to the Director (payload kind `composer_hold`, dedupe `composer_hold:<id>`).
  It names:
  - the role, the tickets, the delivery attempts, and the minutes held;
  - the gate's reason, and that the role's own hook says its turn is over;
  - that nothing was typed, cleared or sent;
  - the supported action: leave a person's line alone; for leftover text, look with
    `switchyard attach <project> <role>` and empty the composer yourself.
- **A deferral that shows the role at work** (`composer_hold.WORK_REASONS`: a busy
  or blocked hook, a changing screen, a working timer, a child at work) means
  current work is authoritative. That closes the episode as `work_resumed`. A
  reason that is neither, such as no hook state yet, leaves the episode alone and
  never reaches the board. The ledger asks about this once per role until a composer hold
  is noted again, so ordinary busy deferrals do not each reach the board.
- **Leaving the queue.** When the episode's last notice leaves the queue, the
  trigger `composer_hold_notice_left` closes it as `delivered`, `superseded` (for
  example, the ticket moved) or `dead_lettered`.

On closing, an alert not yet sent is withdrawn (a `discard` trace with reason
`composer_hold_cleared`). One already sent is followed by one line saying how the
hold ended. An episode costs the Director at most two notices.

The Director's own pane opens no episode: an alert about it would wait in that
same pane.

## Configuration and restarts

The threshold is `TICKET_BOARD_COMPOSER_HOLD_ALERT_SECONDS` in the listener's
environment, defaulting to 1800 s. An empty, negative or unreadable value falls
back to the default.

The episode, its start and its alert live in the database. A restarted listener
joins the open episode, and the dedupe key holds across restarts. A board older
than pgu983 is detected once per listener, and nothing is asked of it.

## Tests

`tests/composer_hold_alert_test.py` uses the SYRD-557 harness: a production-built
board, the real HTTP server, listener, PaneActivityGate, hook-state store and
pane_composer. Every pass is a new listener. tmux, the process table and `/proc`
are stood in for, and the fake tmux refuses anything that could type into or
clear a pane.

- **On main (`81a9c37e`):** both the residue hold and a human draft hold their
  notices silently.
- **Hermes-style residue.** main runs hermes with a trusted `hermes.post_llm_call`
  idle hook:
  - repeated `pane busy` deferrals open one episode;
  - at 29 minutes, nobody has been told; at 31, one alert with the attempts, the
    reason and the attach command;
  - three restarted passes later, there is still one alert, the notice is unsent,
    the pane is untouched, and the composer is as it was;
  - once the composer is emptied, the notice is delivered, the episode clears as
    `delivered`, and the Director hears that it ended.
- **A real human draft** (threshold 10 minutes):
  - it is held and reported once, and never typed over;
  - the person sends the line, the turn starts, and the episode closes as
    `work_resumed`;
  - the notice goes out after the turn, with no second episode.
- **An unreadable cursor** is a composer hold too. When the ticket is cancelled,
  the episode clears as `superseded`.
- **A waiting alert** (the Director's own composer holds it) is withdrawn when the
  role starts working. The Director's own hold opens nothing.
- **An untrusted idle source** opens no episode.
- **Configuration, the per-role settling of busy deferrals, a failing board, the
  grants, and the copy parity** between schema.sql and the migration.

## Otto

When this is deployed to Otto, tell Otto's Director, so any local workaround for
held composer notices is removed rather than left to drift.
