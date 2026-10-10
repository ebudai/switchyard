# SYRD-569: a restarted role resumes its last conversation without a manual /resume

## What went wrong

With `role_state_isolation` (every project `switchyard new` creates, and every
project repatriated onto the project account), the launcher keeps each role's
resumable session in its own store, `<session_dir>/roles/<role>/`
(`role_session_dir`). It reads the record from there at every start, and gives
the pane that same directory as `TICKET_BOARD_PANE_SESSION_DIR`.

The provider hooks that write the record are installed once per account, by
`ticket-board-install-pane-hooks`, with `--session-dir <session_dir>`. They
cannot name a role, because one account runs every role. The hook let that flag
beat the pane's own directory, so every role recorded its conversation in the
shared parent, where no launch looked:

- **Otto**, created isolated, found no record at any launch. After its
  2026-10-10 upgrade, every role logged `no recorded session id ...; starting
  fresh session`. Its real conversations were in
  `pane-sessions/otto-<role>_0.0.json`.
- **SYRD**, repatriated on 2026-09-09, kept the copies made that day. Roles that
  still used those same conversations (ops, app, director) resumed. Main's and
  Audit's copies had since been set aside, so they started fresh at every
  restart. The hook then refused each new conversation as foreign to the stale
  parent record. Only the User's manual resume put the old one back in use.

## The fix

- **The hook records where its pane says.** For the pane's own target
  (`TICKET_BOARD_PANE_TARGET`), the pane's `TICKET_BOARD_PANE_SESSION_DIR` wins
  over the installed `--session-dir`. A hook run for another target, or in a
  pane that names no directory, keeps to the flag as before. The installed hook
  configuration is unchanged; the hook binary reaches each account through the
  installer's usual refresh at upgrade.
- **A start takes back what is already stranded.** `_start_role_session` (every
  launch, reload, start, viewer and recovery path) and `attach_role_to_slot` call
  `stranded_session_records.adopt_stranded_session_record` before reading the
  record. When the role's store is isolated and the shared store holds a record
  for the role's target that is newer than anything the role's store holds
  (its current record, or the one it last set aside), it is copied in.
  The normal resume preflight then decides whether the provider still has that
  conversation.

## Nothing is lost

- The shared record is never changed or removed.
- A record of the role's own that is replaced is set aside as `.superseded`,
  exactly as a failed resume sets one aside.
- A conversation taken back and then refused by the preflight is set aside with
  its own time, so the next start does not take it back again.
- A role already running is not started, and nothing is taken back for it.
- A project without isolation reads its one store, unchanged.

## Tests

`tests/stranded_session_record_test.py` runs the real installer and the real
hook binary, as Claude, Codex and Antigravity run them at SessionStart, with the
environment the launcher gives the pane and no network. It then runs the real
`_start_role_session` and `attach_role_to_slot`, with the real resume preflight
over provider stores in a test home. tmux is stood in for, and any other process
is refused. The real account's `pgu` store is checked unchanged.

- **On main (`a025c35b`):** each provider's hook records into the shared store,
  and the next start begins fresh. Otto's stranded records are never read.
- **Candidate:**
  - each provider's hook records into the role's store, and the next start
    resumes it once, with that provider's resume arguments;
  - Otto's case resumes, with Claude, Codex and agy;
  - Main's case (only a set-aside copy) resumes;
  - an older other conversation is set aside, while the role's own copy of the
    same one is left alone;
  - the role's newer record wins;
  - a later set-aside is respected;
  - a record for another target is ignored;
  - a record without a time is dated by its file;
  - a missing conversation is fresh once and never retried;
  - attach-role resumes;
  - a running role is untouched.

## Not changed here

- No live tenant was restarted or had records copied.
- Otto takes this with its next upgrade. At its first start, each role resumes
  from `otto-<role>_0.0.json` if the provider still has that conversation.
- SYRD's Main resumes at the next restart that runs this release. Audit's
  shared record names its earlier Codex conversation, which agy cannot resume.
  So Audit starts fresh once, its new conversation is recorded in its own store,
  and every later restart resumes it.
- Provider prompts that block a resumed pane (login, trust, model retirement,
  quota) are SYRD-566's provider-prompt alert, unchanged.
