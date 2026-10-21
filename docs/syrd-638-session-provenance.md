# SYRD-638: a role resumes only its own conversation

## What happened (syrd, 2026-10-21)

Measured read-only from the pane-session store, Inspector's resume history,
agy's own store and logs, and the Director's transcript:

- **The launcher started Inspector fresh.** The Director's
  `set-role-runtime syrd inspector --cli agy` ran at 09:30:19Z. At 09:30:26Z
  the launcher declined agy's map (`agy_map_not_taken`: the map predated the
  record it had set aside) and logged `fresh_started`. It passed agy no
  conversation.
- **The pane then resumed another role's conversation.** At 10:58:48Z agy's
  `history.jsonl` records a `/resume` typed in Inspector's workspace. agy's log
  then shows `ListConversations`, followed by the conversation
  `f4e8f4e1-377f-4630-b9b4-cde47677fff1` streaming. Nothing reached the pane
  from the board or the Director in that window.
- **f4e8f4e1 is Audit's.** agy's own summary of it
  (`cache/conversation_metadata.json`) gives its workspace as
  `syrd-worktrees/audit`, and so does the conversation's database. Its history
  holds SYRD-591 to SYRD-594 entering Audit, and then Audit's "SYRD-594 is
  still yours" prompt, which is what Inspector showed.
- **ac53bccd is Inspector's own.** That earlier conversation is filed under
  `syrd-worktrees/inspector`. The launcher had resumed it under Inspector from
  10-15 to 10-20, which was correct.
- **agy also remapped Inspector's worktree.** It is keyed by directory, so the
  `/resume` pointed `syrd-worktrees/inspector` at f4e8f4e1 in
  `cache/last_conversations.json`.

Two things would have resumed Audit's conversation as Inspector's at the next
start:
- SYRD-622's map adoption, stopped only because the record set aside last came
  from Copilot;
- the pane hook, which records the conversation a turn names.

Nothing between a record and a resume asked whose conversation it was.
Provider ids are not scoped to a role, and a record's `target` says only which
pane wrote it.

## The check: `scripts/session_provenance.py`

Before any resume, the provider's own account of the conversation must make it
the role's:

| Runtime | Where the provider records the conversation's workspace |
|---|---|
| agy | `WorkspaceURIs` in `cache/conversation_metadata.json`. Where that summary does not hold the conversation, the `file://` URIs in its database's `trajectory_metadata_blob` (a cutover copies agy's `conversations` and `brain`, not its cache). |
| Codex | the `cwd` of the rollout's `session_meta` |
| Copilot | the `cwd` of `events.jsonl`'s `session.start` |
| Claude | the `cwd` its transcript's entries carry |
| Hermes | nothing to compare: each role's sessions are in its own home |

The verdicts:
- **`runtime`**: the record was written by another runtime's hook, for
  example `codex.SessionStart` for a role now on agy.
- **`workspace`**: the provider files the conversation under another
  workspace.
- **`no-proof`**: the provider holds the conversation but records no workspace
  for it.

A conversation the provider does not hold at all is left to the existing store
check, which already refuses it in its own words.

### Whose it is, where the workspace cannot say

A workspace names a worktree, not a role. The loader allows several roles in
the repository's own root, and pgu's example configuration puts every role
there. So the check also asks who owns the conversation, from durable records
in the project's session store:

- **Owner records.** `conversation-owners/<session id>`, one per conversation.
  It is created with `O_EXCL | O_NOFOLLOW`, mode 0600, in a 0700 directory that
  is not followed if it is a link. The first claim stands and is never
  replaced. Every resume the launcher accepts claims its conversation for the
  role.
- **Claims at birth, by the pane's own hook.** When the hook records a
  conversation that its SessionStart payload says the provider has just
  opened, it writes that conversation's owner record for the pane's own role.
  New means `source` is `startup`, `clear` or `new`, and Copilot's
  `copilot_source` is not `resume`; for agy, any SessionStart, since agy sends
  none for a `/resume` (SYRD-622). The role is the pane's `roles/<role>`
  store, which must agree with its `TICKET_BOARD_CALLER_ROLE`. A `/resume`
  says `resume` and a compaction `compact`; a turn event (agy's PreInvocation)
  is no SessionStart at all. None of them claims anything.

  The claim is made in the hook, at that moment, because nothing later can
  make it: measured on the live store, the hook rewrites a role's record at
  every SessionStart, and the records now say `compact`. An earlier round
  inferred births from the pane history and the provider's creation time.
  Audit showed both could be satisfied by another role's conversation, so that
  inference is gone.

The rules, in order, for a conversation the provider holds:
1. Another role's owner record: refused, `owner`.
2. Filed under another workspace: refused, `workspace`; no workspace recorded:
   refused, `no-proof`.
3. The role's own owner record (its hook's claim at birth, or a resume the
   launcher accepted): accepted.
4. Any trace of it in another role's store (records, set-asides, `.foreign-*`,
   histories, in its own directory or the shared parent): refused,
   `other-role`.
5. A worktree shared with other roles (`RoleConfig.workdir_peers`, computed
   when the configuration is loaded) and nothing to prove it: refused,
   `shared-worktree`. This fails closed.
6. Otherwise the role's own worktree is the proof: accepted.

Where the check runs:
- **`_resume_preflight_allows_attempt`** refuses, with no side effects. That
  covers every caller, including the root-run cutover's check of a copied
  record, which runs as the owner.
  The cutover loads the module before it forks that child: a child that has
  dropped to the owner may not be able to read the release to import anything
  new. `provider_resume` itself still loads nothing of Switchyard's at import.
- **`_start_role_session` and `attach-role`** also move the record to
  `<record>.foreign-<reason>`. A later refusal takes the next free
  `.foreign-<reason>.N`, so the record is never overwritten. It is linked to
  the new name, then unlinked, so it keeps its inode and mode (0600).
  The resume history, `foreign_<reason>`, names the conversation, the
  workspace the provider gives for it, and the role's worktree. The role then
  starts fresh; attach-role refuses.
- **SYRD-622's agy-map adoption** skips a mapped conversation whose workspace
  is not the role's worktree, and says so in the history.
- **SYRD-569's shared-record adoption** does not take back a conversation the
  role has already refused, so it is not retried at every start.

Nothing is typed into a pane, and a role's own conversation resumes exactly as
before.

**After deploy, in a shared worktree.** A role whose current conversation has
no owner record yet starts fresh once. From then on its hook claims each new
conversation, and the launcher each resume it accepts. With a worktree of its own, a role's
current conversation is proven by its workspace and claimed at its next start.

## Neighbour suites

Stand-in provider stores in eleven suites were written as `{}`, which holds no
workspace, so they are now refused as `no-proof`. Real stores always carry the
cwd. Each of those stand-ins now writes the provider's own first line, through
`tests/provider_store_fixture.py`, and no assertion changed. Where a suite
builds on pgu's shared worktree and seeds a record of the role's own
conversation, it now writes that record through `owned_record()`, which also
writes the owner record the launcher would hold. That applies to 14 tests, in
`team_launcher_stop_reload`, `_resume_detached`, `_tmux_panes`, `_viewer` and
`_reload_attach`. SYRD-569's and SYRD-622's scenario suites test one role at a
time, so they now give each role its own worktree, as real tenants have. The
suites:
- `agy_resume_record`, `manual_resume_record`, `stranded_session_record`;
- `context_continuation`;
- `team_launcher_stop_reload`, `team_launcher_resume_detached`,
  `team_launcher_tmux_panes`, `team_launcher_viewer`;
- `ticket_board_process_authority` (the legacy-migration cases);
- `copilot_runtime`, `role_model_selection`.

`team_launcher_viewer_test.py` keeps its 1,393 lines: the import shares a line,
as SYRD-627's does.

## Tests

`tests/session_provenance_test.py` runs the real launcher:
- `_start_role_session` and `attach_role_to_slot`;
- the resume preflight;
- SYRD-569's and SYRD-622's adoptions;

over provider stores in a test home, shaped as each provider writes them. tmux
is stood in for and refuses anything that types into a pane. The live agy
store is never read: the launcher's root is pointed into the test home.
The before-run is main `9fe2ae9`, from a git archive.

**On main**, Inspector resumes Audit's f4e8f4e1 from each of:
- its own record;
- agy's map;
- a legacy shared record;
- attach-role.

Main also hands agy a Codex-recorded id, resumes a conversation agy holds no
workspace for, and resumes Codex, Copilot and Claude conversations from
another worktree.

**On this tree**, each of those is refused and kept as `.foreign-<reason>`, and
the role starts fresh. Still working:
- Inspector's own conversation resumes, and again after a restart;
- a map naming it is still adopted;
- each runtime's own same-role conversation resumes;
- attach-role with the role's own record;
- after a cutover, agy's database alone proves a conversation's workspace,
  both ways;
- a second refusal keeps the first record set aside.

**In pgu's own shared worktree**, with Audit and Inspector both on agy, main
resumes each of Audit's conversations for Inspector. This tree refuses each
one, and passes no provider argv the foreign id:
- traced in Audit's store: kept as `.foreign-other-role`;
- with no trace anywhere: kept as `.foreign-shared-worktree`;
- owned by Audit: kept as `.foreign-owner`;
- a `/resume` before the fresh session was recorded, whose first event is
  agy's PreInvocation naming the conversation: refused as `shared-worktree`,
  and not claimed;
- a conversation opened after the Director's fresh start and `/resume`d into
  its pane, whose first recorded event is Claude's SessionStart with
  `source=resume`: refused as `shared-worktree`, not claimed, and kept with
  its reason.

These run the real pane hook, as the pane runs it, with the payload the
provider sends.

Still resuming in the shared worktree:
- Inspector's own conversation, opened by its agy at a fresh start: claimed by
  the hook (`how: hook:gemini.SessionStart`), resumed, and resumed again;
- one Inspector owns, with nothing left in its history.
- a Claude conversation opened at a fresh start (`startup`): claimed by the
  hook, and resumed.

A second claim never replaces the first, and no refused conversation is ever
claimed.
