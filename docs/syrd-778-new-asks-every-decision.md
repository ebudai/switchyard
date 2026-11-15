# SYRD-778: `switchyard new` asks every decision it needs, instead of refusing for a flag

On 2026-11-15 `switchyard new` asked test11's name, slug, agent user and path, then stopped. Two boards (sapi and syrd)
were declared to take tenant reports, so it said: "Name one with --upstream-report-board <sapi|syrd> ... then run
this again". The User asked for every such decision to be "fold[ed] ... into the questions that are posed with the
others".

## The rule

- A decision `new` needs is asked in the same sequence as its other questions, before anything is created. Its
  default, when it has one, is shown in brackets and taken by Enter. A question with no safe default has none, so
  Enter alone answers nothing.
- A flag on the command line answers its question, so the question is not asked. `--yes` takes each question's
  default where it has one.
- A run nobody can answer keeps the refusal, with its flag, because there is nobody to ask. That covers a `--yes` run
  with no default to take, and a run with no terminal and no `--yes`. `new_project_questions.can_ask` decides this:
  an injected reader can answer, and the real `input` can only on a terminal.
- The summary printed before the confirmation includes each answer.

## The reports board

When no single declared board decides it, `new` asks which board takes the new tenant's reports. It asks after the
root check and before the role questions (`upstream_report_sinks.choose_report_board`, with `ask` and `yes`).

- **Choices:** the declared boards that can take reports, by slug and name, plus `none`, meaning the tenant cannot
  file reports. A board root has not declared is never offered (SYRD-686). With none declared, only `none` is
  offered, and the declaration the host lacks is named.
- **Default:** the Switchyard project's own board, the one root's registry records as `Switchyard` (syrd on this
  host), when it is one of the choices. This is the User's decision of 2026-11-15. `--yes` takes it.
- **No Switchyard board among the choices:** there is no default, and `--yes` keeps the refusal.
- **Exactly one declared board that can take reports:** it is the default and nothing is asked, as before.

## Which desktop

On a host with several signed-in desktops and none named, `new` asks which one the project may use, with no default:
guessing one person's session for another is what the refusal existed to prevent. Then that desktop's access is asked
as for any desktop: screenshots and clipboard (the default), or headless. Three cases are still refused, as before:

- `--desktop-gui-user` names an account that is not signed in (a wrong answer, not a missing one);
- `--yes`, because there is no default to take;
- nobody can answer.

On a host with no desktop, `--yes` now takes the headless question's default, headless, which grants nothing.

## A Claude role's model

`runtime_catalog.RECORDED_MODELS["claude"]` now offers `claude-opus-5-5` (Opus 5.5) first, so it is the default a
Claude role's model question shows and an Enter-only run records. `claude-opus-5` (Opus 5) is still offered. This is
the User's decision of 2026-11-15. The catalog version is now 2.

- Nothing else chooses a Claude model default. `DEFAULT_MODEL_ARG_BY_CLI` holds the model flag's name, not a model.
- Existing projects keep their recorded models.
- Router's default is SYRD-779's.
- On this host, the installed claude (2.1.318) already runs a live pane with `--model claude-opus-5-5`.

## Survey: every refusal on `new`'s interactive path that names a flag

| refusal | where | what became of it |
|---|---|---|
| several declared report boards: "Name one with --upstream-report-board … then run this again" | `upstream_report_sinks.choose_report_board` | **asked**; default syrd; `--yes` takes it |
| no board declared: "Declare the board … then run this again; or re-run with --no-upstream-report" | same | **asked**, offering `none` and naming the declaration the host lacks; no default |
| the one declared board cannot take reports (no token or URL) | same | **asked**, offering `none`; no default |
| several signed-in desktops: "Name the one … with --desktop-gui-user USER" | `desktop_policy._resolve_desktop_policy` | **asked**, no default |
| no desktop under `--yes`: "… or install headless with --headless" | same | **`--yes` takes the default**, headless (interactively it was already asked) |
| `--yes` with no recorded desktop approval: "--yes does not grant desktop access …" | same | **stays**: consent to desktop access is a person's act, and the question's default (desktop) grants it |
| an existing owner account: "pass --allow-existing-owner-user to reuse it" | `owner_preparation._confirm_existing_owner_user` | already asked (default No); refused only when nobody can answer |
| a selected CLI is not host-wide, under `--yes`: "re-run with --agent-cli-policy promote-local …" | `agent_cli_promotion.require_agent_clis_for_new_tenant` | already asked interactively (`[p/l/s/a]`); **stays** under `--yes`, because promoting a binary changes the host for every project. Its hint named `--role-cli`, which `new` does not have; it now says "in the role questions" |
| promote-local with no source, under `--yes` | same | stays under `--yes`; interactively the `[p]` choice offers the copy it found |
| a model still unknown with no terminal (`set-role-runtime …`) | `model_validation` (P4) | already asked on a terminal |

These name a flag but stay as they are, because each is a safety or validity stop rather than a missing choice:

- **Conflicting or invalid flag values:** `--headless` with `--desktop-policy`, `--no-upstream-report` with a board, a
  board that is not registered or is the tenant's own, `--agy-credential-source` that is not an account name, and
  `--agent-cli-source` that is not `cli=path`.
- **Not root:** "new requires sudo".
- **Host prerequisites found after the confirmation:** Postgres not answering, a port or database already in use, an
  unreadable agy credential, `--no-git-init` without a repository, and `--output-dir` not root-controlled.
- **Stops after the project is registered (P4):** a missing CLI, or a provider not signed in. Each is something to
  install or sign in to, not a choice.

These mention a flag but do not stop the run, so they are not refusals:

- The first board on a host is announced as the receiver, with "Re-run with --no-upstream-report to declare
  nothing".
- With no approved desktop owner and no `--board-ui-user`, the board UI lends no Director operation. The run continues
  with the safe default, nobody, and an operator names one later.

## Tests

`tests/new_project_questions_test.py` runs `switchyard new`'s real choice phase on the phase guard's fakes, against
SYRD-674's sandboxed host with real boards and registry records:

- With sapi and syrd declared, the question is asked with `[syrd]`. Enter gives syrd, and `sapi`, `1` and `none` are
  honoured. The summary says the answer, and the question names no flag.
- `--yes` gives syrd. One declared board is the default, unasked. A flag answers the question.
- A run nobody can answer keeps today's refusal.
- With no Switchyard board declared, there is no default: Enter re-asks, and `--yes` is refused.
- With none declared, only `none` is offered.
- A child on a real pty: the real `input` asks, and Enter takes syrd. A Director's model question shows
  `[Opus 5.5]`, and Enter records `claude-opus-5-5`. With no terminal, the child is refused, naming the flag.
- Several desktops are asked about with no default. `--yes`, a wrong name, and nobody to answer are each refused.
  `--yes` on a host with no desktop takes headless.
- A Claude role defaults to `claude-opus-5-5`, and Opus 5 is still offered and chosen by number.

Suites that pinned the old behaviour now say which case they cover:

- `host_report_receiver_test` and `upstream_report_receiver_test` cover nobody to answer.
- `desktop_policy_boundary_test` covers the ambiguous host.
- `role_plan_prompt_boundary_test`'s golden was re-recorded for the catalog's new first entry.
- `team_launcher_project_role_prompts_test` and `provisioning_selector_test` now expect Opus 5.5.
