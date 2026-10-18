# SYRD-622: an agy pane's resumed conversation is recorded, and found again at the next start

## What happened

Otto's designer runs on agy. After a power cut on 2026-10-18 it came up fresh,
though the User had `/resume`d its real conversation in the pane. The User had
to `/resume` it by hand again, for the second time in three days.

Two gaps lined up:

1. **The hook refused the switch.** agy opens a conversation the User
   `/resume`s in the pane without a SessionStart. Only the next turn's
   `PreInvocation`, `PostInvocation` and `Stop` follow, and they name it.
   `ticket-board-pane-idle-hook` accepted a conversation switch only from a
   SessionStart run by the pane's own provider (SYRD-569). So all 292 turn
   hooks of the resumed conversation were refused as foreign, and the record
   kept the throwaway conversation from the fresh start. This host's syrd
   audit pane did the same while it ran agy: 1960 refusals, all naming its own
   conversation.
2. **Nothing recorded a resume with no turn after it.** The Director's model
   change set the record aside. The User resumed again, and the designer took
   no turn before the outage. With no hook run there was no record, and the
   start was fresh. Yet agy's own `cache/last_conversations.json` mapped the
   designer's worktree to the conversation throughout.

## What it is now

### The hook: on agy, a turn from the pane's own provider can be the switch

`PANE_SWITCH_TURN_SOURCE_PREFIXES = ("gemini.",)`: on agy, a turn hook whose
session id differs from the record counts as a pane switch. It needs the proof
SYRD-569 already asks of a SessionStart: `_started_by_pane_provider` finds
nothing but shells between the hook's provider and the tmux server. These are
still refused:
- an agy started from the role's tool shell, which has the pane's provider
  above it;
- a hook run outside any pane;
- a Claude or Codex turn with another id. Both announce a `/resume` or `/clear`
  with SessionStart, so their turns are never the switch.

agy 1.4.2 is a single Go binary. It runs a hook command through `/bin/sh` as
its own child, and it runs directly under tmux (`ps`: no helper process between
them), which is the shape `_started_by_pane_provider` accepts. Nothing on this
account's agy store is nested (`conversation_summaries.parent_conversation_id`
is empty for all 10 conversations). Every refusal in both incidents named the
pane's own conversation. So no other conversation's turns reach these hooks in
practice.

### The start: no record, so ask agy

`scripts/agy_last_conversation.py`, called just after SYRD-569's stranded-record
adoption in `_start_role_session` and in `attach_role_to_slot`. agy writes
`cache/last_conversations.json` (working directory -> conversation id) at the
moment it opens a conversation. On agy 1.4.2 a resume sets the file's mtime to
its own second, and does not touch the conversation's database.

When a role that resumes with `agy --conversation` has no record, the
conversation agy maps to its worktree becomes the record (source
`agy.last_conversations`). The normal preflight then checks that agy still has
it. The map is taken only when:
- the role resumes at all, and not fresh for each ticket;
- agy wrote the map **after** the launcher last set this role's record aside
  (`.superseded`'s ctime). A record set aside on purpose stays aside unless agy
  has opened a conversation since. That covers a runtime or model change, a
  failed resume, and a conversation the preflight refused, so a refused
  conversation is not retried at every start.
- the record set aside last, if any, was agy's own (a `gemini.*` hook source,
  or one taken from the map). A role that came to agy from another runtime does
  not inherit an older agy conversation.
- the file is a regular file read without following a link, at most 1 MiB,
  is a JSON object, and maps the worktree to an id of agy's form.

Every decision, taken or not, is kept in the pane's resume history:
`adopted_agy_map` or `agy_map_not_taken`, with the reason.

A role that has a record is left alone: the map is consulted only when there
is none. A resume after which the pane takes a turn is recorded by the hook.
One case is not covered: a resume with no turn after it, on a role that already
has a record. agy runs no hook at the resume itself, and its map carries no
per-directory time to say which is newer.

## Tests

- `tests/agy_resume_record_test.py` (new) uses SYRD-569's harness: the real
  installed hooks, run by a stand-in provider that is the pane's own process in
  a real tmux server on a private socket, inside an unshared namespace with no
  network. It also uses the real `_start_role_session`, `attach_role_to_slot`
  and resume preflight over a test home.
  - Otto's sequence: a fresh start records its conversation at SessionStart,
    then the resumed conversation's turns arrive. The record changes on the
    first turn, the history names the turn and the provider, and the next
    start resumes it with `--conversation`.
  - Still refused: an agy started from the tool shell, a Claude turn and a
    Codex turn with another id, and an agy hook outside any pane.
  - Starts: no record with a map; a map written after an agy record was set
    aside; a map written before it; a role new from Claude; a conversation agy
    no longer has (taken, refused, then not taken again); a linked, malformed,
    list-shaped, oversized or non-id map, or one with no entry; a Claude role;
    a fresh-per-ticket role; attach-role; an existing record that the map does
    not replace.
  - Before-run on main: the record keeps the throwaway conversation, each turn
    is refused, and starts with a map are fresh.
- `tests/manual_resume_record_test.py`: section 3's agy case now expects the
  pane's own agy turn with another id to be recorded. The CLI started inside
  the pane is still refused.
