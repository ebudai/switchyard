# SYRD-694: adopting a legacy control-role conversation

## Why

MEFP's Director conversation began before roles had worktrees of their own, so Codex files it under the
project repository rather than the Director's worktree. SYRD-638 refuses a conversation filed under another
workspace (`foreign-workspace`), so every start set it aside and began fresh. The User could get it back only
with Codex's in-pane `/resume` picker, under *All* conversations, since it was not in the local list.

SYRD-638 is not weakened. A foreign conversation is still refused, unless root's own record adopts exactly that
conversation for exactly that role.

## Whose proof it is

Every role of a tenant runs as the same account. So nothing that account can write tells the control role apart
from a sibling, including a well-formed adoption file in the project's own store. Audit reproduced exactly that
against the first candidate. The proof a start trusts is therefore **root's**:
- `/var/lib/switchyard/conversation-adoptions/<conversation>.json`, at a fixed path that no configuration names;
- written only by the catalogued privileged action `adopt-conversation`, and removed only by
  `withdraw-conversation-adoption`. Before root does anything, their helper proves the caller is the control
  role's registered pane, by pid, start time and uid under process authority
  (`privileged_helper.require_registered_control_caller`). A sibling role is refused there;
- proven against root's own board. The helper normally reads a project's board URL from the tenant's
  registered configuration, which every role can rewrite. So a sibling role could point it at a board of its
  own that names the sibling's pane as the control role. Both actions therefore declare
  `board_from_unit=True`, and the helper asks only the board root's unit
  `/etc/systemd/system/<project>-ticket-board.service` names (`ticket_board/board_identity.py`):
  - the unit, and every directory above it, is read by descriptor walk from `/`, root's and writable by nobody
    else;
  - its `--unix-socket` is the one connection used;
  - the kernel (`SO_PEERCRED`) must show the unit's own `User=` account and the unit's `MainPID` holding that
    connection.

  The tenant's configuration is never consulted for these actions;
- bound to that role. Each action declares `requester_argument="role"`, so the helper refuses unless the role
  named is exactly the one it proved asked. The Director cannot have root attest an adoption for Ops;
- bound to the confirmed plan. The caller names only the project, role, conversation and the digest of the
  preview it confirmed. Root runs that plan again with the shared release's own code, as the tenant account its
  registry records (`board_ui.tenant_user`, through `runuser` in a clean environment, `adopt-conversation
  --plan-json`). It records the plan's own values, and only when the plan has no problems, is for exactly that
  project, role and conversation, and has exactly that digest. So the catalogued primitive, called directly,
  can no more skip the source rules than the command can;
- reached through descriptors from `/`, refusing a link at every component, on every read, write and removal
  (`scripts/conversation_adoption_record.py`). Every directory and the record itself must be root's and writable
  by nobody else. Anything else is refused, not read: a record the tenant account wrote, one reached through an
  intermediate link, or one in a directory others can write.

Root reads no tenant file itself, and no path reaches it. The catalogue's values are the project, the role, the
conversation id (letters, digits and `-`, so no glob and no separator), and the 64-hex digest. The plan is
computed as the tenant, over the tenant's own stores, exactly as the preview was. The record binds the
workspace and worktree by the sha256 of each normalized path.

The operation runs from the shared release, before the launcher is imported, like `job-selftest`. It writes the
record next to a private temporary file, mode 0644, and links it into place, so never over an existing one.

## The command

```bash
switchyard adopt-conversation <project> <role> <session-id>                      # preview; changes nothing
switchyard adopt-conversation <project> <role> <session-id> --confirm <digest>   # ask root for exactly that
switchyard adopt-conversation <project> <role> <session-id> --rollback           # preview the rollback
switchyard adopt-conversation <project> <role> <session-id> --rollback --confirm <digest>
```

The preview prints:
- the provider, and where it holds the conversation (Codex: its rollout file);
- the source workspace the provider files it under, and why that is approved;
- the target role, its pane and its worktree;
- the evidence for every check;
- every state change;
- a digest over all of it.

`--confirm` runs every check again. If the digest still matches, it asks for the privileged action from the
control role's own pane, through `switchyard privileged-action`'s front door. That front door's pre-flight
refuses an action this host's installed boundary predates. Anything else asks for nothing.

## What is checked before anything is asked for

| Check | Refused when |
|---|---|
| role | it is not the project's control role, runs as another account, or resumes nothing from a provider store (Hermes) |
| id | it is not a provider conversation id |
| provider | its store does not hold the conversation, records no workspace for it, or files it under more than one |
| source | the workspace is any other role's worktree, or where another role's owned conversation ran. Otherwise it must be the project repository (`repository` in the configuration) or an earlier worktree of this role. Anything else is refused: an arbitrary directory, a relative path, or one the provider does not record |
| owner | another role's `conversation-owners` record names it |
| other role | another role's records or history name it (SYRD-638's own check) |
| live | a process of this account names it, or a running pane's record names it |
| once | root has adopted it already, or has adopted another conversation for this role |

The provider's workspace is compared as recorded, normalized but never resolved, so a link made since proves
nothing. An **earlier worktree** of the role is proven only by another conversation that role owns, whose
owner record the role's hook or an accepted resume wrote, and which the provider files there. An adopted
conversation proves nothing, so adoptions cannot be chained.

## What changes, and when

- **Apply:** root writes its record, and nothing else changes. No provider content and no role record is
  touched.
- **The role's next start:** for the role's own project, pane and role, while the conversation has no owner
  record, the start points the role's record at
  it. The record it held is kept as `.superseded`, and the resume history logs `adoption_installed`.
- **`judge`:** SYRD-638's `judge` accepts that one conversation with proof `adopted` (history:
  `adopted_conversation`), and only while root's record says so. The record must name that role, pane, runtime
  and worktree in the role's own project (`TICKET_BOARD_PROJECT`), and the provider must still file the
  conversation under exactly the adopted workspace. The
  runtime, owner, other-role and shared-worktree checks still apply. Every other conversation from that
  workspace, and the adopted one for any other role, pane or worktree, is still refused.
- **The first resume:** it claims the conversation in `conversation-owners`, with `how: adopted`. From then on
  the record is the role's own business, so a role that later moves to another conversation is never pulled
  back.
- **`switchyard status`:** says that the role resumes the conversation by adoption, where it began, and whether
  it has been resumed yet. It never claims the conversation began in the worktree.
- **Rollback:** removes root's record and nothing else. The conversation is kept, and so is the owner record.
  The next start refuses the conversation again, as `foreign-workspace`.

## MEFP

The host needs a shared release with these two actions, and the boundary installed from it
(`install-shared-release`, then `switchyard privileged-action mefp upgrade-tenant`). The front door names that
repair if the boundary predates them. Then:

1. Find the restored conversation's id and its `session_meta` cwd, read-only. The configuration at
   `/home/stellaris-agent/stellaris-bugfix/.switchyard/provision/mefp.json` suggests the repository is
   `stellaris-bugfix`.
2. From the Director's own pane, in a conversation other than the one being adopted, preview. Once the User
   has signed off, confirm with the digest.
3. Stop the Director once, then start or recover it. It resumes the adopted conversation with no `/resume`.
4. Confirm that a conversation of another role's, or of another project's, is still refused.

## Tests

`tests/conversation_adoption_test.py` runs the real start path over a Codex store in a test home. Root's tree
is a directory of the test's own, since a test is not root; what ships is asserted. The front door is stood in
for by the real catalogue's validation and the real root argv. It covers:
- what ships;
- Audit's reproductions:
  - a record a tenant role could write;
  - group- or world-writable directories and records;
  - a link at an intermediate directory or at the record, on read and on write;
  - the real helper asked by the Director for Ops (refused), or with no verified role (refused);
  - root's operation given another digest, a plan with problems, or a plan for another role, project,
    conversation or schema (recorded nothing);
  - another project's adoption of the same conversation (no proof, not installed);
- root's registry as the only source of the tenant account, and the plan computed as that account, in a clean
  environment, with the release's own wrapper;
- the command that asks, which names the id, not counting as the conversation being live;
- root's operation refusing a non-root caller;
- the preview, which writes nothing;
- the digest, and a refused privileged action;
- apply, which writes root's record and nothing else;
- the resume at the next start, without `/resume`;
- the refusals that remain;
- rollback;
- every source and state refusal, and the earlier-worktree proof;
- dispatch, and the wrapper's root operation.
