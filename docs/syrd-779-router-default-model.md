# SYRD-779: a new Router defaults to Haiku 5.5 at high effort

The User decided on 2026-11-15: "default router model should be haiku 5.5 high, not opus 5". syrd's own Router
already ran `claude-haiku-5-5` at `high`, but only because it was set by hand. A Router the product created did not
get that:

- `switchyard new` offered Claude's default, Opus 5 (Opus 5.5 since SYRD-778), at low effort;
- `switchyard router-role enable` copied its template role's model and effort.

## The default, in one place

`router_setup.ROUTER_DEFAULT_MODEL = "claude-haiku-5-5"` and `ROUTER_DEFAULT_EFFORT = "high"`, on
`ROUTER_DEFAULT_RUNTIME = "claude"`.

- `router_defaults(runtime)` gives them on claude and nothing on any other runtime. It checks them against the runtime
  catalog first, so a model the catalog does not know raises instead of being recorded.
- The catalog now offers `claude-haiku-5-5` (Haiku 5.5), third, after Opus 5.5 and Opus 5. That is catalog v3.
- `router_launcher_settings(runtime, model=, effort=)` returns the model and effort that are named, else those
  defaults. An empty value means nothing is recorded, so the runtime's own default applies.

## `switchyard new`

Turning Router on asks its runtime, then that runtime's model and effort (`router_onboarding.prompt_router_runtime`).
These are the same fields every role is asked with; only the defaults are Router's, chosen once the runtime is
known:

- on claude: `router model [Haiku 5.5]` and `router effort [high]`, which Enter takes;
- on another runtime: that runtime's own first model, at low effort, as before. A runtime that takes no effort is not
  asked one.

The menu says so: "on claude it starts on Haiku 5.5 at high effort, and on another runtime on that runtime's own
default model". The answer is recorded in the design artifact (`project.router`) and on Router's projected launcher
entry, as before. `team-launcher <project> design --router-runtime claude` with no `--router-model` or
`--router-effort` records the same defaults.

## `switchyard router-role <project> enable`

- `--runtime claude`: Haiku 5.5 at high effort, unless `--model` or `--effort` names others.
- Any other runtime: nothing is recorded unless named, so that runtime's own default applies.
- `--template-role` still chooses the launcher entry Router starts from, and the stages and actors it copies, but
  never its model or effort.
- `--effort` for a runtime that takes none is refused, and `disable` takes neither flag.

## Existing projects

Nothing changes them. A projection keeps a role's recorded launcher entry, so an upgrade, launch or resume leaves
Router's model and effort as recorded; syrd's is already Haiku 5.5 at high. `set-role-runtime <project> router
--model …` still changes it.

## Tests

`tests/router_default_model_test.py`:

- **`switchyard new`'s real Router prompt:** on claude it shows `[Haiku 5.5]` and `[high]`, and Enter takes them. The
  real artifact writer records them, they load back for `--from`, and `record_router_runtime` puts them on Router's
  launcher entry instead of its template's. On codex, Router gets codex's first model at low effort; on agy, agy's
  first model and no effort question.
- **`switchyard router-role enable`,** through the real `switchyard_main` against a real board, with a template role
  on another model:
  - claude with no flags gives Haiku 5.5 at high;
  - `--model` and `--effort` give those values;
  - codex records nothing, so the template's model is not used;
  - an effort on agy is refused, and `disable --model` is refused;
  - applied, the launcher's Router runs Haiku 5.5 at high, the template role keeps its own, and a later projection
    keeps Router's.
- **The constant:** it is in the catalog, and an unknown one is refused.

`router_role_command_test` now expects Router on codex to have no model rather than its template's.
`role_plan_prompt_boundary_test`'s golden was re-recorded for the catalog's new entry and Router's reworded
description.
