# SYRD-727: what "fresh" means for a Hermes role

An ephemeral role starts each ticket on an empty conversation and carries nothing from the last one
(`roles[].ephemeral`, SYRD-135; `docs/syrd-11-declarative-workflow.md`). For Hermes, emptying the conversation
is not enough. The SYRD-693 canary saved a fact with Hermes's own `memory` tool. After the board's
fresh-conversation command, a new session recalled it, because Hermes puts `memories/MEMORY.md` and `USER.md`
into every new session's prompt. Its `session_search` tool reads every earlier session as well. This note says
what Switchyard does about that, for cloud and local (SYRD-692) Hermes roles alike.

The code landed with SYRD-693 (`6f4067e1`). SYRD-727 changes the board's command to `/new now` and adds the
end-to-end proof below.

## Fresh, for Hermes

A Hermes role is fresh per ticket when the board clears it into each ticket: the workflow declares it
`ephemeral`, or its launcher entry says `fresh_session_per_ticket`
(`hermes_role_config.starts_each_ticket_fresh`). For such a role:

- **Its Hermes runs with no durable memory and no session search.** The role's own `config.yaml` sets
  `memory.memory_enabled`, `memory.user_profile_enabled` and `memory.provider` off, and lists `memory` and
  `session_search` in `agent.disabled_toolsets` (`close_ticket_boundary`). Hermes then neither loads
  `MEMORY.md`/`USER.md` into a prompt nor offers either tool, so nothing can be saved for a later ticket or
  searched from an earlier one. Disabling is what Hermes 0.15.2 supports reliably, so memory is not moved or
  rotated per ticket.
- **The board's boundary is `/new now`.** That is Hermes's own fresh-conversation command, with its skip token
  so that no confirmation menu swallows the ticket typed after it. It starts a new session in the same
  process.
- **The board checks the pane before typing anything.** Hermes reads its config once, when it starts, and a
  new session keeps the same agent. So the board first reads what the pane's own process started from
  (`ticket_board/hermes_ticket_boundary.open_reason`):
  - the HERMES_HOME in its tmux start command;
  - that home's `config.yaml` carrying `TICKET_BOUNDARY_MARK`;
  - the file being older than the process.

  If any of these fails, no continuation is decided, nothing is typed, and the hand-off waits as
  `hermes_ticket_boundary_open`, with the reason and "restart the role". **A failure to establish the boundary
  fails closed:** the ticket waits, and never arrives on a pane with memory open.

## What is kept

- **Same-ticket resume:** a restart resumes the ticket's own conversation (SYRD-626 recovery). That
  conversation is its history, not Hermes's memory, so it is intact.
- **Same-parent continuation:** when SYRD-626 decides the next ticket continues the pane's conversation, the
  board types no `/new now` and the conversation carries on, still without memory or session search. The
  check above runs before that decision, so only a closed pane is ever continued.
- **Persistent roles:** a role that is neither ephemeral nor fresh keeps the owner's shared `config.yaml` (or
  its effort-only copy, SYRD-546), with Hermes's memory exactly as the owner configured it. A project that
  wants a stricter policy for persistent roles sets Hermes's own `memory` keys in the owner's shared config;
  Switchyard does not override that.

## Recovery and upgrade

- **A pane started open** holds its next hand-off until the role is restarted. That covers a pane started
  before SYRD-693's release (`6f4067e1`), and a persistent role the workflow has since made ephemeral.
- **The restart that first closes a home is fresh** (`boundary_cutover`). The conversation it would have
  resumed was held with memory open, so it is set aside: the record moves to `.superseded`, recoverable, and
  `ticket_boundary_cutover` goes into the pane's resume history. A ticket still bound to it may be parked for
  the Director (SYRD-540). Later restarts resume as usual.
- **A Hermes upgrade:** the policy relies on Hermes honouring `memory.*` and `agent.disabled_toolsets`.
  `tests/hermes_memory_boundary_test.py` and the boundary probe in `tests/hermes_local_profile_test.py` run
  against the installed Hermes, so an upgrade that stopped honouring them fails those suites. Rerun them
  after upgrading Hermes.

## The filesystem boundary

- **What Switchyard manages:** only the role's own `config.yaml`. It is written as a new file in the role's
  private home and renamed over whatever was there, so a link is replaced, never written through. The file
  carries Switchyard's mark on its first line and the boundary mark after it. A local profile's home is
  written only by descriptor (SYRD-692).
- **A `config.yaml` Switchyard did not write is never replaced.** The start says so, and since the file
  carries no mark the board holds that pane.
- **Memory files are never read, moved or deleted.** An operator's `MEMORY.md`/`USER.md` and earlier sessions
  stay where they are, unused while the boundary is closed.
- **Who decides:** the workflow's declaration, which the Director applies, decides ephemerality. The launcher
  writes the role's home as the role's owner. The board's listener runs as that owner too, and only reads.

## Rollback

- **Making a role persistent again** (declaring it not ephemeral, or removing `fresh_session_per_ticket`)
  changes its config at the next start. A cloud role's private `config.yaml` is removed and the shared one
  linked back (or the effort-only copy written). A local-profile role's own config is rewritten without the
  boundary. Either way, Hermes's memory, still on disk, is used again.
- **Reverting SYRD-727** restores `/clear now` as the board's Hermes command and removes this note and its
  suite. Nothing else changes: SYRD-693's closed boundary stays. Ephemeral homes keep memory, user profile,
  memory provider and session search off, and the board's check before any continuation or clear stays. Both
  commands start a new session the same way, so isolation is unchanged.
- **Reverting past SYRD-693 (`6f4067e1`)** is a different, older boundary. It restores the bare `/clear` and
  the open homes, which reopens the memory leak the canary found, and the swallowed tickets with it. Do not
  roll back that far for an ephemeral Hermes role.

## Proof

- **`tests/hermes_memory_boundary_test.py`** runs the installed Hermes in its own network namespace, with a
  stand-in model that calls `memory` whenever Hermes offers it. Ticket A and unrelated ticket B are two
  sessions, and the homes are written by Switchyard:
  - a persistent home saves the fact and B recalls it, which is the canary's leak;
  - an ephemeral home is never offered `memory` or `session_search`, saves nothing, and B cannot recall it.
- **End to end, offline:** interactive Hermes in a private tmux, with the board's own check, command and
  settle time. On the ephemeral home:
  - ticket A could not save the fact;
  - A's same-ticket follow-up saw it in its conversation;
  - the board's check passed and `/new now` started a new session;
  - unrelated ticket B neither saw it nor could search for it;
  - A, resumed after a restart, saw it again from its own history.

  On a persistent home, B saw it (memory) and found it (`session_search`), and the board's check held that
  pane with "leaves Hermes's memory and session search on".
- **Unit coverage:**
  - `hermes_ticket_boundary_test`: the board's check against a real tmux pane, and the cutover;
  - `ephemeral_role_sessions_test`: holding before a continuation, then the retry;
  - `hermes_local_profile_test`: the rendered homes, read by the installed Hermes.
