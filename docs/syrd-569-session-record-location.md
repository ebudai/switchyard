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

## Recurrence: MEFP's Main, 2026-10-13

MEFP's Main (Codex) came up fresh after `switchyard mefp` on release `3d410cad`,
which contains the fix above, and the User restored it by hand again. The only
log left, Konsole's, was empty: a start's decision went to the stderr of
whatever started the role, never to Konsole.

### The trap a manual `/resume` falls into

Once a role has started fresh, for whatever first reason, every later start was
fresh too, however often the User resumed it:

1. A fresh start clears the role's record. The pane's provider records its new,
   unused conversation: Claude at once, at SessionStart; Codex at its first
   input, which the listener's nudge provides.
2. The User types `/resume` in the pane. The provider announces the restored
   conversation with SessionStart (`resume`; Claude and Codex both report
   `startup`, `resume`, `clear` and `compact`).
3. The hook refused it as foreign. The record already had an id, and the pane
   was launched with no `TICKET_BOARD_PANE_SESSION_ID` to match. The guard
   exists so that a CLI a role starts inside its pane (`claude -p`,
   `codex exec`), which inherits the pane's environment, cannot replace the
   pane's record (PGU-620).
4. So the record still named the unused conversation. The next start found it
   empty (Claude, agy: the preflight refuses it, and the role starts fresh) or
   resumed the nudge (Codex), and the User had to resume it again.

MEFP's own records are not readable from the agent account, so which first
reason put Main there is not known. The trap is what kept it there.

### The fix

- **A conversation the pane's own provider announces is recorded.** The hook
  accepts a SessionStart with another id when the provider that ran it is the
  pane's own. Claude Code 2.1.294 and Codex 0.162.0 run a hook as their direct
  child (measured on this host), so the hook walks `/proc` from its parent: the
  first process that is not a shell (`sh`, `bash`, `dash`, `zsh`) is the
  provider that ran it. That provider is the pane's own when only shells lie
  between it and the tmux server.
- **Unchanged:**
  - a CLI started inside the pane has the pane's provider above it, so it is
    still refused;
  - a hook run outside any pane is still refused;
  - a turn event (not SessionStart) carrying another id is still refused.
- **Every decision is kept.** `<session_dir>/<pane>.resume.log`, beside the
  record, holds one JSON object per line (`scripts/resume_history.py`).
  - **The launcher** writes what each start or attach-role decided:
    - `no_record`, `adopted_shared` or `fresh_per_ticket`;
    - `preflight_refused`, with the preflight's reason;
    - `resumed`, `resume_unverified`, `resume_failed` then `fallback_started`;
    - `fresh_started`.

    Each entry carries the entry point, the message it printed, and every
    record file the start could see (the role's own, the set-aside ones, the
    shared store's) with the id, time and transcript each names.
  - **The hook** writes `recorded` when the record changes (the new id, the
    one it replaced, the provider's pid), and `kept` when it refuses one.

  The file is private to the account and kept under 256 KiB, dropping its older
  half past that. A failure to write it never fails a start.

`tests/manual_resume_record_test.py` runs the real installed hooks from a
stand-in provider in a real tmux pane, in a user namespace with no network,
with the environment the launcher builds. It covers Claude, Codex and agy.
- **On main:** each provider's record keeps the unused conversation after the
  User's `/resume`, and the next start does not resume the restored one. A
  `/clear` in a resumed pane is refused, and no start leaves a history.
- **Candidate:**
  - the restored conversation is recorded and the next start resumes it;
  - a `/clear` in a resumed pane is recorded;
  - CLIs started inside the pane, turn events and hooks outside a pane are
    refused, and each refusal is kept;
  - every start decision and the history's bound are checked.
