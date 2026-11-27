# SYRD-889: a new Researcher defaults to agy with Gemini 4 Hydrogen (High)

The User, 2026-11-27: "lets change that to gemini 4 hydrogen high please". "That" is the default model a new
Researcher is given. Until now that default was Claude Opus 5.5: `switchyard new` offered Claude first, and
`switchyard research-stage <project> enable` needed `--runtime` and copied the template role's model.

## Where the default comes from now

The defaults live in `research_stage.RESEARCHER_DEFAULT_RUNTIME = "agy"` and
`RESEARCHER_DEFAULT_MODEL = "gemini-4-hydrogen-high"`. agy takes no effort level, which is already the catalog's
rule, so "high" is part of the model id and no effort is asked. `researcher_default_model(runtime)` returns the
model on agy and "" on every other runtime.

- **`switchyard new`, the Research stage question.**
  - Choosing `researcher` asks for the runtime first, with agy as Enter's answer.
  - The model question is then built for the runtime chosen, as Router's is (`prompt_researcher_runtime`). On agy,
    Enter takes `gemini-4-hydrogen-high`. That holds whether the list comes from the recorded catalog or from a live
    `agy models` in the owner's account that lists it anywhere.
  - Claude still offers Opus 5.5 at high, and Codex its own first model.
  - The menu line says where a Researcher starts.
  - The record, preview, apply and retry are unchanged. They carry whatever was chosen.
- **`switchyard research-stage <project> enable`.**
  - With neither `--role` nor `--runtime`, it declares a new Researcher on agy with `gemini-4-hydrogen-high`
    ("flags only to deviate").
  - `--role` still gives the research to an existing role.
  - A new `--model` names the Researcher's own model. `switchyard_dispatch` forwards it.
  - `--runtime agy` without `--model` takes the default.
  - `--role` together with `--runtime` or `--model` is refused.
  - After the projection, `research_onboarding.researcher_launcher_settings` writes the model onto the Researcher's launcher entry and
    removes the effort on a runtime that takes none. On any other runtime with no `--model`, the template's model
    and effort stay, as before.
  - A Researcher turned back on starts from its own retired launcher entry, as every re-enabled role does
    (`workflow_launcher.project_roles`).
- **`team-launcher <project> design --researcher-runtime agy`** with no `--researcher-model` records
  `gemini-4-hydrogen-high`.
- **The agy fallback catalog.** `runtime_catalog.RECORDED_MODELS["agy"]` records `gemini-4-hydrogen-high` as
  "a new Researcher's default", with a source note.
  - It goes last. agy's first recorded model is what Router, the Librarian or any other role on agy is offered
    first, and none of those changes.
  - The catalog version moves to 5, dated 2026-11-27.

## Not changed

- Existing Researchers, and any model chosen explicitly. There is no upgrade move.
- Every other Claude role's default stays `claude-opus-5-5` (SYRD-886).
- An implementer chosen to do the research keeps its own runtime and model.

## Sign-in for an agy-only Researcher: already covered

The ticket asked us to confirm that `switchyard new`'s sign-in step covers a Researcher that is the tenant's only agy
role. It does:

- `_prepare_new_project_board` runs `new_project_command` first. That projects the Researcher into the launcher
  config and records its model (`record_researcher_runtime`). Only then does `_run_new_project_sign_in` load that
  config.
- `build_first_run_setup_manifest` groups the config's roles by CLI (`_roles_by_first_cli`), so the Researcher
  brings agy with it:
  - an `agy models` status check in the owner's account;
  - when it is not signed in, the `agy` login step, with SYRD-782's printed link and no browser of its own.
- Once signed in, `run_first_run_auth_phase` compares the Researcher's model with the owner's own `agy models` list,
  and `confirm_unknown_models_with_owner` offers that real list when the model is missing from it.
- Before anything is created, the CLI precheck (`require_agent_clis_for_new_tenant`) is handed the Researcher's
  runtime through `with_researcher_cli`. So is the owner CLI check after the account exists
  (`verify_agent_clis_for_owner`).
- When the host records an agy credential source, it is seeded whatever the roles' runtimes are, so a Researcher
  needs no change there.

## Pinned by

**`tests/researcher_default_model_test.py` (29 checks).**
- The catalog entry and its position, the version, and the unchanged Claude and agy defaults.
- The launcher settings.
- The Research stage question:
  - agy and Hydrogen as Enter's answers, from the recorded list and from a live one that lists Hydrogen third;
  - no effort question on agy;
  - Claude, Codex and another agy model.
- The design command's record.
- A projected Researcher's runtime.
- agy's sign-in step for a tenant whose only agy role is the Researcher.

**`tests/research_stage_test.py`.** The real `switchyard research-stage` command now checks:
- plain `enable`;
- `--runtime agy`, with and without `--model`;
- `--runtime claude --model`, which keeps the template's effort;
- the refusal of `--role` with `--runtime` or `--model`.
