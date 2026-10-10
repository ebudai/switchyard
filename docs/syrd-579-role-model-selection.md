# SYRD-579: a role keeps the model it chose across every restart

## What Otto showed

On 2026-10-10, Otto was upgraded from 2951c5c9 to a025c35 and then started.
Every Claude role came back on `--model claude-opus-5 --effort high`. Before
the upgrade, the operator had moved those roles to Opus 5.5. Ops resumed the
right conversation, but on the old model.

## Why

- **How the operator chose.** The operator picked Opus 5.5 with Claude's own
  `/model`. That picker says "Your pick becomes the default for new sessions".
  Picking Opus writes `{"model": "opus"}` to the account's
  `~/.claude/settings.json`, and the session goes on as Opus 5.5.
- **What Switchyard passes.** Every launch passes the configured model and
  effort on the command line (`role_command.cli_command_for_role`).
  `switchyard new` had written `claude-opus-5` for Otto's Claude roles. That was
  the only Claude model in `runtime_catalog.RECORDED_MODELS`.
- **Which one wins.** The flag beats the saved pick. This was measured on
  Claude Code 2.1.294, in a sealed home with no network and a placeholder key:
  - with settings `claude-opus-5-5` and `--model claude-opus-5`, the banner
    reads "Opus 5";
  - without the flag, it reads "Opus 5.5".

  The flag is passed on a fresh start and on `--resume` alike.
- **What the upgrade did.** No upgrade phase writes a role's model. The only
  writers are `new`, first-run model confirmation, `set-role-runtime`, and a
  reload's sync from a live pane's argv. So nothing rewrote the configuration.
  The upgrade restarted the roles, and the restart put each one back on its
  configuration.

The choice is still on record. Claude writes `model` and `effort` on every
assistant turn of the transcript that the role's session record names.

## The change

`scripts/role_model_selection.py`:

- **`last_turn`** reads a transcript backwards from its end, at most 64 MiB,
  without following a link. It returns the model and effort of the last
  assistant turn, and skips:
  - subagent (sidechain) turns;
  - Claude's own `<synthetic>` turns.

  Only an identifier is taken from a transcript, never text.
- **`role_model_drift`** finds the Claude roles whose own session last ran
  another model or effort than the configured one. A role is skipped when:
  - it starts a fresh session per ticket;
  - it configures neither a model nor an effort (no flag is passed, so the
    provider's own choice already stands);
  - it has no recorded session or transcript.

  An alias such as `opus` or `opus[1m]` matches the family it names.
- **The `model_selected_at` stamp.** Each role's configuration can carry the
  time a choice was last recorded through Switchyard. Turns before that time
  do not count, so a choice is asked for once, and a later pick counts again.

Three places act on drift. None of them picks between the two values; each
asks:

1. **`switchyard upgrade`** checks before its first phase.
   - A dry run lists each drifted role and says that apply would refuse.
   - Apply refuses before anything is stopped or written, unless
     `--role-models running` or `--role-models configured` names what each
     role keeps.
   - After its phases, the upgrade compares every role's model and effort
     with what it held before them. It exits 1 and names any change that
     nothing asked for.
   - A role whose runtime changed is not counted, because the model belongs
     to the runtime.
2. **A launch** refuses if it would start or reload a drifted role. This
   covers `switchyard start`, the bare `switchyard <project>`, and
   team-launcher start and reload.
   - Start leaves a running role running, so only the roles that are down
     count.
   - Reload restarts every role, so all of them count.
   - The refusal comes before anything starts, and it prints the two repairs.
3. **`switchyard reconcile-role-models <project> [--role R] [--keep running|configured | --model M] [--dry-run]`**
   settles the choice:
   - `--keep running` (the default) records what each drifted role's session
     last ran;
   - `--keep configured` records the configuration as chosen;
   - `--model M` names a model outright for the roles given with `--role`.
     This is the repair for a role whose session no longer shows its choice,
     for example one that already took turns on the wrong model.

   It records the choice and its time in the tenant configuration. Where the
   project artifact's `role_models` and `role_efforts` name the role, those
   are updated too. The workflow document carries no model.

   It then restarts only the live roles whose process runs something else,
   through the ordinary reload, which resumes the recorded conversation:
   - a role that is down takes the choice at its next start;
   - a busy role is left running and named;
   - a restart that comes back on another model fails the command.

   It shares `set-role-runtime`'s dispatch block and, like that command, runs
   without escalation.

**Why a reconciled live role must restart:** a reload copies a running pane's
`--model` back into the configuration. A live role still running the old flag
would hand the old model back to the configuration at the next reload.

## Across the paths the ticket names

- **Stopped and running tenants.** A start of a stopped tenant checks the
  roles it would start. An upgrade checks every role before it stops any of
  them.
- **Role-state-isolation migration.** The migration copies each role's
  session records and Claude conversation into the project account. It
  changes no model or effort, and the post-phase check holds it to that.
- **`finish-upgrade`, `adopt-workflow` and every workflow apply.** These
  project the declared workflow into the configuration. The projection keeps
  each existing role entry, including its model, effort and stamp. The test
  runs the projection to show it.
- **Stop and start.** The recorded choice is the configuration, so every later
  start passes it.

## Not covered

- **Codex.** Its own model picker is not read. Codex roles are left as they
  were.
- **Effort chosen with `/effort` and no turn since.** Such a pick leaves no
  turn behind, so it is seen only once the role takes a turn.
- **A session-only pick.** Claude's picker can apply a model to the current
  session only. That counts as drift like any other pick; `--keep configured`
  answers it.

## Tests

`tests/role_model_selection_test.py` builds an Otto-shaped tenant in a scratch
home:
- four Claude roles on `claude-opus-5`. director, audit and ops last ran
  `claude-opus-5-5`, a model newer than the template, and director also ran
  effort medium;
- research, whose recorded session is missing;
- one Codex role.

Its transcripts carry the record keys Claude Code 2.1.294 writes, followed by
a synthetic turn and a subagent turn.

The before-run is main a025c35 from a git archive:
- director and ops resume with `--model claude-opus-5`;
- a start and an upgrade both go on.

On this tree:
- **Refusals.** A start and an upgrade refuse before anything starts, and
  write nothing. A dry run lists each change.
- **The printed remedies.** Both run through switchyard's own parser. After
  each:
  - director, audit and ops resume on the kept model;
  - research starts fresh;
  - a start and an upgrade go on;
  - the workflow projection keeps the choice;
  - a pick made later counts again.
- **Live roles.** Only the live, idle role whose process runs another model is
  restarted, through the reload, with its recorded session. A busy role is
  named, and a restart that comes back wrong fails.
- **The upgrade options.** `--role-models running` and
  `--role-models configured` each record their choice.
- **After the phases.** Using the real upgrade command with its phases
  replaced:
  - a phase that puts the template model back fails the upgrade and is named;
  - a runtime change does not.
- **Transcript reading.** Turns are read correctly when split across read
  chunks, and back no further than the limit. A link is not followed, and
  nothing but an identifier is taken.

## Evidence

- `tests/role_model_selection_test.py`: 41 checks pass on this tree; its
  before-run on main a025c35 reproduces the reset.
- Mutation, through `tests/bounded_run.py mutate`: 24 mutants, 24 killed, each
  by the check written for it.
- 177 related suites were run serially, with provider stubs that refuse to
  start, on this tree and on main a025c35. Every suite red here is red on main
  too, with the same output. The guards updated here pass:
  - `project_launch` 125 checks;
  - `project_upgrade_command` 103;
  - `upgrade_phases` 409;
  - `switchyard_dispatch` 252;
  - `switchyard_commands` 119;
  - `switchyard_parsers` 209;
  - the four suites whose launch-read lists gained the gate.
