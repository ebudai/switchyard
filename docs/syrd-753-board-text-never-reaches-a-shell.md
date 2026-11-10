# SYRD-753: board text never reaches a shell

Origin: MEFP, 2026-11-10.

## What happened

Ops, a Codex role, wrote a tool call that returned a delivery finding:

```
ticket-board-write return-delivery-finding MEFP-1075 --reason "... `pkexec /usr/bin/cp --no-clobber` ..."
```

Codex runs every shell tool call as `bash -c <the command the model wrote>`. Inside double quotes bash runs
backticks as a command substitution, so it started the Markdown-quoted `pkexec` before `ticket-board-write` ever
ran. That was a root PolicyKit request in the pane's process group, and it was still waiting after the turn ended.
Switchyard's background-work check reported the leftover process group when Ops was handed its next ticket. The
request expired unanswered; MEFP found no change.

## Where the class lives

| Path | Who builds the command line | Finding |
|---|---|---|
| A role's own shell tool call (every runtime) | the model | **The class.** The model's prose goes inside shell quotes, and the runtime's shell evaluates it. |
| Notice delivery: foreground, background, retry and recovery, held-notice release, image handoff | `notification_dispatch` | `[directorctl, "send", target, message]` as argv. directorctl types the message with `tmux send-keys -l`, which sends literal keys, not a shell. It evaluates nothing (no `eval`). A message over 200 characters, or with a newline, is staged with `mktemp` at mode 0600 (0640 for the roles group), and a one-line pointer is typed instead. |
| A pane whose CLI has exited | the launcher | Held by the inert pane window (SYRD-43), never handed back to a shell, so typed text cannot reach a shell prompt. |
| Background and lease tracking | `background_child`, `execution_lease_pass` | They read `/proc` and process groups. The one `sh -c` (`background_child`) interpolates only an integer process group id. |
| Board writes made by Switchyard's own code | none | No program builds a `ticket-board-write`, `directorctl` or `switchyard` command line from text. |
| Generated instructions: skills, guides, CLI hints | Switchyard | They showed `--reason "..."`, which invites prose inside double quotes. **Changed** (see below). |

### Every remaining `sh -c` in Switchyard's Python

`tests/shell_guard_test.py` pins these per file (`SHELL_C_SITES`), so a new site fails the suite until it is
accounted for. None takes board, ticket, comment, reason or notice text. No program uses `shell=True`,
`os.system`, `os.popen` or `subprocess.getoutput`, which the suite also checks.

| File | Sites | What the shell is given |
|---|---|---|
| `agent_cli_promotion.py`, `provider_auth_status.py` | 1 each | `command -v <binary>`, with the binary name `shlex.quote`d |
| `browser_inheritance.py` | 5 | fixed scripts; the handler, browser and paths are passed as positional arguments (`sh -c SCRIPT sh ARGS...`) |
| `github_identity.py`, `staged_role_tooling.py`, `repository_boundary_repair.py`, `upgrade_phases.py` | 1-2 each | provisioning renderers' command lists. Slugs, account names and paths go through the renderer's `shell_quote`. |
| `owner_commands.py`, `tenant_release_report.py` (4), `tenant_release_deploy.py` | | systemctl, git archive and deploy lines from the release SHA, unit names and paths, all `shlex.quote`d |
| `tenant_control_helper.py` | 1 | the staging commands above, as one `shlex.quote`d script |
| `presentation_layout_files.py` | 1 | a failure message and window title, `shlex.quote`d, printed in an inert pane |
| `role_command.py` | 1 | Copilot's root prestart: a fixed script with positional arguments |
| `ticket_board/publication_boundary.py` | 6 | root's publication setup from paths, the commit and documents, all `_quote`d |

## The guard

`scripts/ticket-board-shell-guard` is installed as the pre-tool hook of every supported runtime. It reads the
command the model asked for and refuses it before any process starts when a board command's prose would reach a
shell evaluation. Prose means:
- a free-text option of `ticket-board-write` (every option `write_cli.add_free_text_argument` registers, plus
  `--set`, `--value`, `--patch-json` and `--payload-json`) or of `switchyard` (`--reason`);
- the message of `directorctl send` / `callback`.

It is refused when it holds any of these:
- backticks, `$(...)`, `$((...))` or `<(...)`;
- a `$name` mixed into the text. A whole prepared value, `"$TEXT"`, passes: the shell does not run or re-read it.

It is also refused when that prose is read from standard input through an unquoted heredoc, a here-string, or a
pipe that does any of the above. And it is refused when a board command sits inside a string handed to `sh -c`,
`bash -c` or `eval` that the outer shell expands first; a literal one is read in turn.

### What the line runs, not what it says

Audit (on 4914b680) showed that reading only a literal `ticket-board-write` word left the class open:
`BOARD=ticket-board-write; "$BOARD" add-comment ... --text "...`x`..."` passed, and bash still ran the backticks. The
guard now decides what each simple command runs, and fails closed when it cannot tell:
- **The command word.** It skips assignments, wrappers and their options (`env`, `sudo -u`, `timeout`, `nohup`,
  `xargs`, `switchyard-bounded` and others), and the keywords that start a command list (`do`, `then`, `!`, `{`).
- **A value it can read is resolved.** A `"$NAME"` whose literal value this same line assigns (`NAME=value`,
  `export NAME=value`) is taken as that value.
- **A command it cannot name might be a board command.** A command word from an unresolved variable, a command
  substitution or the like is refused when its free-text options, or any of its arguments, hold something the shell
  would run. That includes `"$B" ... --commit $(git rev-parse HEAD)`; resolved, or written plainly, that form passes.
- **Functions and aliases.** One defined in the line whose body names a board command runs one under its own name. A
  `name() {...}` definition in a line naming a board command is refused, because the guard does not read it.
- **Prepared strings.** A `bash -c` or `eval` string resolved through the line's assignments is read in turn. One
  that can't be read is refused when the line names a board command.
- **Scripts written and run.** Literal heredoc, `printf` and `echo` staging are tracked before a later shell
  invocation in the same call. An unknown producer marks its output unknown, so running it as a shell script
  refuses. A heredoc written to notes and never executed stays data.
- **Existing and cross-call scripts.** Before `bash file`, `sh file`, `source file`, `. file`, a shell's redirected
  or piped script input, or direct execution of a shell-shebang/text script, the guard reads the script and applies
  the same board-prose rules. It resolves literal assignments, the tool's working directory, literal `cd`, and PATH
  lookup. A script written in a previous call is checked from its current file contents. Reads are nonblocking,
  limited to 1 MiB, and require a regular UTF-8 file; an explicit shell input that is missing, unreadable, a link,
  too large or unresolved refuses for the model's direct shell-file invocation. Inside an inspected script,
  syntax, dynamic sources, anonymous substitutions and inline nesting are refusals only when board reach exists.
  A literal existing but unreadable shell-file input has unknown board reach and still refuses. An absent optional
  configuration source has no current bytes to execute; it does not block an otherwise unrelated script.
  Static source/subscript reach is followed independently of the four-level inline-string limit, with a bounded
  64-state graph and cycle detection; file-state changes make a new graph state.
- **Visible file replacements in the same call.** Earlier literal `cp`, `mv`, `install` and `ln` commands replace
  the destination's modeled contents before a later shell reads it. Literal source bytes, target-directory
  options and relative symbolic-link targets are resolved without running the command. Known links follow later
  visible writes. An unresolved operand or directory/recursive effect invalidates the affected destination state;
  the shell input then refuses rather than trusting the file's old bytes. An unresolved unprivileged write cannot
  replace an immutable root-owned package script under `/usr` or `/opt`; a privileged wrapper gets no such exception.
  Ordinary copy/link commands themselves
  remain permitted, and a literal safe replacement can still be consumed. Function definitions are inspected in
  separate state, so defining a helper does not execute its filesystem effects at top level.
- **Visible interpreter shell strings.** Literal Python `-c` calls of `os.system`, `os.popen`, subprocess shell
  strings or an explicit shell argv are inspected through Python's AST without executing the code. Visible
  Node/Perl/Ruby shell-launch calls containing board text refuse with a direct-argv/file-input instruction.

The command-name test requires a complete executable token, optionally after a path. A tenant home such as
`/home/switchyard-agent` or text such as `switchyard-example` is not the `switchyard` command. The scanner follows
case patterns, nested case branches, function signatures and array assignments/appends; pattern `)` characters
do not end a surrounding command substitution. Dynamic nested-shell strings are assessed in their physical
command-line context rather than using unrelated board names elsewhere in the script. The static sibling-source
idiom `source "$(dirname "${BASH_SOURCE[0]}")/name.sh"` resolves against the inspected script's path without
executing `dirname`. Unrelated dynamic inputs inside a script do not by themselves imply a board command. An
unresolved command with board-shaped arguments (a ticket ID, board verb or reason option) remains fail-closed;
GUI `--text`/`--title` options alone are not board evidence. A shell-to-Tcl/Perl self-exec trampoline is inspected
through its unconditional interpreter exec, without treating its other-language body as shell. If unsupported
syntax prevents whole-script parsing, the guard still follows readable literal source/shell/path-script lines,
assignments and visible file replacement lines; it does not reinterpret arbitrary quoted program fragments as
independent shell commands. Explicit board-command text in unsupported syntax always refuses.

**What passes is what keeps the text literal:**
- a quoted heredoc: `--reason - <<'EOF'`, or `"$(cat <<'EOF' ... EOF)"`;
- `--reason-file PATH`;
- single quotes, or `$'...'`;
- double quotes with nothing to expand.

**What it ignores.** Other options pass as before (`--commit $(git rev-parse HEAD)`, a ticket id in
`"$TICKET_BOARD_TICKET_PREFIX-12"`), as does every command that is not a board command.

**What the refusal says.** It names what the shell would have run or expanded, gives the heredoc form, and says
that nothing was run. It reads shell syntax with a small scanner of its own: quotes, escapes, substitutions,
heredocs and their bodies, pipelines, subshells, redirections and comments. A command that names a board command
but can't be read (an unterminated quote, say) is refused, since bash would fail on it too. A payload with no
command, or a command naming no board command, is allowed, and so is a guard fault in that case. It is a
guardrail against the shell evaluating text by accident, not a sandbox: a role can still run what it runs.

### Each runtime's contract

Each was read from what the installed CLI ships. No model call was made.

| Runtime | Event | Payload | Refusal |
|---|---|---|---|
| Claude Code 2.1.318 | PreToolUse, no matcher | `tool_input.command` | `hookSpecificOutput.permissionDecision: deny` |
| Codex 0.166 | PreToolUse, no matcher (own group) | `tool_input.command` / `cmd`, a string or `[bash, -lc, ...]` | the same; Codex's schema (`PreToolUsePermissionDecisionWire`, "Command blocked by PreToolUse hook") |
| agy 1.5.1 | PreToolUse, matcher group `""` | `toolCall.args.CommandLine` | `{"decision": "deny"}`; every answer must be a JSON object, so it allows with `{}` (its embedded hook contract) |
| Copilot 1.0.10x | preToolUse | `toolName: bash`, `toolArgs` (a JSON string or object) with `command` | `{"permissionDecision": "deny"}` (its SDK docs) |
| Hermes 0.15 | pre_tool_call | `tool_input.command` (tool `terminal`) | `{"decision": "block"}` (`agent/shell_hooks.py`'s wire protocol) |

### Installation

**Where the guard runs from.** Like Claude's permission hook (SYRD-234), the guard runs from the copy staged
root-owned beside the installer (`--shell-guard`, defaulting to that sibling), never from a copy a role could
rewrite. `provision_role_tooling` stages it, so every path that runs the staged installer installs it: provisioning,
the upgrade's pane-hook refresh, and a Copilot pane's own COPILOT_HOME prestart.

**What re-installing does.** A re-install replaces the managed entry, as for every other hook.

**Codex runs it without a new approval.** A role's Codex pane starts with `--dangerously-bypass-hook-trust`
(`YOLO_ARGS_BY_CLI`), so it runs the guard from its next start. Only a Codex role configured without yolo checks
hook trust. For that role the guard is new hook content: the owner approves it in Codex's `/hooks`, and the
launcher's existing Codex hook-trust report names it until then. Either way, the guard goes after the PreToolUse
activity hook, as a group of its own. Codex trusts a hook by its position and content, so an approval already given
to the activity hook still holds.

**Hermes accepts it by itself.** Hermes roles start with `--accept-hooks`, so the new hook needs no consent prompt.

**Panes load it on restart.** Each pane picks up its new hooks when it restarts.

## The instructions

**The rule.** The board skill and the board guide now say to give every free-text option through a quoted heredoc
on standard input, or a file, and never inside double quotes. They also say what the guard does when it refuses.

**The examples.** In the skills, the onboarding guides and the CLI hints, every example has changed:
- execution-lease, provider-hold and documentation-gate notices;
- router setup;
- the blocker-review and reopen modules.

Free-form placeholders use `--reason-file PATH` and the corresponding file flags. Structured JSON examples use
`"$(cat <<'EOF' ... EOF)"`, so an apostrophe in the User's message cannot close a shell quote. The suite pins these
forms across generated hints, skills and guides.

**The schema's own hints (Audit, on 4914b680).** Four hints emitted by schema functions showed `--reason "..."`:
- the external-blocker refusal (`apply_blockers`);
- the clear-early hints of `note_provider_quota` and `provider_hold_status`;
- the clear hint of `begin_provider_account_hold`.

`pgu1035_syrd753_reason_file_hints.sql` redefines the four, each its owning migration's copy (pgu990,
pgu1011, pgu1011, pgu1030), with only the placeholder changed to `--reason-file PATH`. `schema.sql` carries the
same copies. There is no new function, and no grant changes.

## What is unchanged

- **The board client.** The `--*-file` and `-` forms, and how text resolves (SYRD-196).
- **Authority and attribution.** Caller authority is still bound to the process, and attribution is unchanged. A
  refused command never runs, so nothing is written or attributed.
- **Notice delivery.**
- **Execution leases and tool-activity evidence.** The activity hook still runs first, unchanged.

## Tests

`tests/shell_guard_test.py`. Main runs first, from a git archive.

**Installed hooks, both trees.** The real installer runs into a disposable home, on both trees at the same home
path.
- On main, no runtime has a pre-tool hook that reads a shell command.
- On this tree, each of the five runtimes has exactly one: the staged guard with its `--runtime`.
- Codex's activity hook has the same trust key and hash on both trees.

**The guard as each runtime runs it, this tree.** Each installed command runs as `sh -c <command>`, with that
runtime's payload on standard input.
- **Refused, in the runtime's own answer shape (26 forms; the 11 indirection forms are listed below):**
  - inline double quotes (MEFP's form), backticks alone, and a variable inside prose;
  - `--flag=value`;
  - an unquoted heredoc, `cat` of an unquoted heredoc, a here-string, a pipe, and a two-stage pipe;
  - `bash -c`, single- and double-quoted, and `eval`;
  - a `directorctl` message;
  - a wrapped command in a pipeline;
  - `--blocked-reason`.
- **Passed (7 safe forms and 9 ordinary commands):** agy gets exactly `{}`, the others silence.

**The bytes, this tree.** The text holds the Director's whole set:
- backticks around a plausible pkexec command;
- `$()` and `$(( ))`;
- `$HOME`, `${PATH}`, `$1` and `$$`;
- nested quotes, semicolons, pipes and redirects;
- backslashes, Unicode, and lines starting with `--` and `-`.

It goes through every safe form, each run by `bash -c` as Codex runs a tool call. Stand-ins for pkexec, `id`, `cp`,
`hostname` and a marker program come first on PATH, and `ticket-board-write` is a stand-in that runs the real parser
and resolver.
- No stand-in ever runs.
- The resolved text is exact (a `cat` substitution drops trailing newlines, as the shell does).
- A `--reason` through standard input is exact too.
- A client pinned to a disposable board stores each one byte for byte. The board strips one trailing newline from a
  comment, as SYRD-196 recorded.

The unsafe forms are only ever given to the guard, never to a shell.

**Indirection, this tree.** These are refused by every runtime's installed hook, all carrying the Director's text:
- a variable-named board command, and one from a variable the line never sets;
- `$(command -v ticket-board-write)`;
- prepared strings run by `bash -c` and by `eval`;
- an unreadable `bash -c` string in a line naming a board command;
- a function and an alias for a board command;
- a script the line writes and then runs;
- a variable-named command behind `sudo -u`, and one in a `for ... do` loop.

These pass:
- a variable-named board command fed by a quoted heredoc. It also runs under `bash -c`, and its text reaches the board
  exactly;
- `"$B" submit-to-audit ... --commit $(...)` with `B` resolved;
- `"$EDITOR" notes.md`;
- a heredoc written to notes;
- `bash -c "$CMD"` in a line naming no board command;
- a prepared plain `bash -c` string.

**The schema, both trees.** Main's `schema.sql` has the four double-quoted hints. On this tree no function of the
disposable board's live schema has any.

**The inventory, this tree.**
- The guard's prose options cover every free-text option of the CLI.
- No `shell=True`, `os.system`, `os.popen` or `subprocess.getoutput`.
- The `sh -c` sites are those above.
- Notices are handed to directorctl as argv; directorctl types literal keys and has no `eval`.
- No skill, guide or Python hint embeds free-form prose in a quoted fill-in placeholder.

**Neighbour suites adapted to the new hook and staged file:**
- `copilot_runtime_test`: the hook file has `preToolUse`, which runs the staged guard.
- `ticket_board_pane_hooks_install_test`: Codex has seven trust entries, not six.
- `ticket_board_hermes_hook_events_test`: `pre_tool_call`. The real Hermes 0.15 lists it among its valid events,
  checked offline.
- `provision_role_tooling_boundary_test`: the staged executables include the guard. Its golden was regenerated with
  its own `run()` and differs by exactly one install block and the matching call counts.
- `director_approval_revocation_test`: the skill's withdrawal command reads its reason from a file.

## Mutation

27 mutants, all killed by a check of this test:

| Where | Mutants |
|---|---|
| **The guard's rules** | backticks not executing; command substitution never a hazard; every substitution read as literal; mixed variables passed; stdin never checked; stdin flags ignored; nested shells ignored; an expanded `-c` string ignored; only the first pipe stage checked; directorctl ignored; `--reason` not prose; every heredoc read as expanding |
| **Answers** | Copilot answered in the Hermes shape; agy's `{}` dropped |
| **Indirection (Audit's return)** | wrappers not skipped; assigned values not resolved; the unknown-command rule removed; aliases and functions not followed; written scripts not recorded; an unreadable nested string allowed; prepared strings not resolved |
| **The schema** | one hint put back in `schema.sql` (the live-schema pin) |
| **The installer** | no Hermes, Copilot or agy entry; the guard placed before Codex's activity hook (its trust hash changes); every runtime given `--runtime claude` |

## Limits

**PATH-script decision comparison (2026-11-10).** With the same command-name inventory on this host, the
697fbaf2 baseline refused 55 of 529 shell-shebang executables, 93d5c75f refused 63, and the reachability repair
refuses four. `xdg-open`, `checkupdates`, all eight additional 93d5c75f refusals, and the nine pinned repository
scripts pass without execution. The remaining conservative refusals are `libtool` (a literal shell input after
an unresolved working-directory change) and `mkinitcpio`, `update-initramfs` and `plymouth-set-default-theme`
(literal/transitive shell inputs after unresolved file-state effects). The latter set includes three refusals
not present in 93d5c75f: following supported static inputs now reveals their unknown reach. This does not claim
all installed shell programs can be analyzed. The focused regression suite passes 461 checks; 14 reachability,
15 file-replacement, eight script-input and seven syntax/token mutations are each rejected by assertions.

**No live model turn was driven.** The runtimes' refusal is taken from each CLI's own shipped contract, as the
table above records.

**A non-yolo Codex role needs an approval.** Its owner approves the guard in `/hooks`, as the launcher's trust report
will say. Every role pane picks the guard up when it restarts.

**A guardrail for visible shell reparsing.** Existing shell-file inputs and directly visible interpreter shell
strings are covered. The guard does not statically analyze arbitrary Python/Node/Perl programs, binaries, encrypted
or generated programs, simulate filesystem effects of arbitrary helper calls, or predict future file mutations by
a concurrent process between inspection and execution. File-replacement tracking covers the directly visible
simple commands described above; unsupported directory effects or unresolved operands fail closed for a later
literal script input whose board reach cannot be determined. The role's concurrent filesystem remains outside
this decision-only model. It does
not enforce a sandbox or prove arbitrary shell programs safe. Its prevention claim concerns the documented visible
board-prose paths, with safe file/stdin forms taught for every board write. A new concrete supported-runtime path
needs its exact transcript and a bounded regression, rather than a claim of arbitrary-program completeness.

**A future report should carry evidence.** If a model-written command ever gets past the guard, the report needs the
exact command text from the runtime's transcript.
