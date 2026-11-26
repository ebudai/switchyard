# SYRD-886: Opus 5.5 is the default Claude model

The User, 2026-11-26: "I would like the default version for opus to be 5.5
instead of 5".

SYRD-778 had already put `claude-opus-5-5` first in the runtime catalog, so
`switchyard new` offered it. Opus 5 was still supplied in two places:

- the bundled `config/team-launcher/pgu.json`, for director, research and
  audit;
- `runtime_catalog.RECORDED_MODELS["claude"]`, which described Opus 5 as
  "configured for director, research and audit".

`switchyard add-role` configured no model at all, so a Claude role it added ran
whatever the installed claude picked.

## What changed

- **One Claude default.** `runtime_catalog.default_model("claude")` is the
  catalog's first Claude model, `claude-opus-5-5`. Every other runtime returns
  `""`, its own default, as before. The catalog is now v4 (2026-11-26).
- **Opus 5 is an explicit choice only.** It is still offered, described as "the
  previous default, still chosen explicitly".
- **The bundled `pgu.json`** runs director, research and audit on
  `claude-opus-5-5`.
- **`switchyard add-role`** configures a Claude role with
  `default_model("claude")`. A Codex, agy, Hermes or Copilot role still gets no
  model.

## Upgrades move the stale retired default, once

The Director changed acceptance 3 on 2026-11-26, after the User said: "I have
been manually changing model versions myself, nothing used opus 5 for a while
now". A configuration that still names `claude-opus-5` because Switchyard once
gave it as the default is a stale default, not a pick. Opus 5 stays selectable
as an explicit choice (acceptance 2), so an upgrade tells the two apart.

- **Retired defaults.** `runtime_catalog.RETIRED_DEFAULT_MODELS` maps
  `claude-opus-5` to `claude-opus-5-5`. `RETIRED_DEFAULT_SINCE_CATALOG` says it
  stopped being the default at catalog v2 (SYRD-778).
- **What moves** (`role_model_selection.retired_default_moves`): a Claude role
  configured with `claude-opus-5`, but only when all three hold:
  - the tenant was made while Opus 5 was the default: its project artifact's
    `catalog_version` is below 2, or it has no artifact;
  - nothing recorded the model as the role's choice: no `model_selected_at`,
    which `reconcile-role-models` and `upgrade --role-models` write;
  - no upgrade has moved this tenant before: the configuration has no
    `retired_defaults_moved`.
- **The move.** The dry run lists each role by tenant and role ("would move
  otto's audit from claude-opus-5, a retired default, to claude-opus-5-5;
  ..."). Once nothing is refused, and only after every phase of the upgrade has
  succeeded, it:
  - writes the new model and nothing else, so effort, runtime, conversation and
    `model_selected_at` stay as they are;
  - records `retired_default_moved: {from, at}` on each moved role;
  - marks the tenant with `retired_defaults_moved: ["claude-opus-5"]`, even when
    nothing moved, so it never moves again;
  - lets the project artifact's `role_models` follow;
  - restarts nothing.

  An upgrade whose phases fail moves nothing and leaves the configuration as it
  was. It names each role it didn't move, and the next upgrade moves it.
- **What survives an upgrade.** An Opus 5 pick recorded as a choice, any Opus 5
  set after the tenant's first move, and any Opus 5 on a tenant made at catalog
  v2 or later are all kept.
- **The SYRD-579 drift guard** judges a moved role against `claude-opus-5-5`:
  - a session the User switched to 5.5 by hand doesn't drift, so no
    `--role-models` choice is demanded;
  - a moved role whose process from before still runs `claude-opus-5` isn't
    drift, at the upgrade or a later reload, until it has run 5.5 since the move
    (`ran_since`), so it takes 5.5 at its next ordinary restart;
  - after that, Opus 5 is a pick like any other, and drift. A later recorded
    choice clears the move record.
  - Every other drift is held exactly as before, for example a recorded 5.5 pick
    whose session then ran Opus 5, or a Sonnet 5.5 pick. The upgrade then
    refuses and moves nothing.

## What did not change

- **`switchyard new`'s role question** already defaulted to Opus 5.5, at a
  terminal and with nobody to ask. Paths that skip the question, such as a
  design artifact with no models or `team-launcher new-project`, still write no
  model: an absent model means the runtime's own default (SYRD-115).
- **`set-role-runtime` without `--model`** keeps the configured model on the
  same runtime. On a runtime change it offers the catalog at a terminal, which
  defaults to Opus 5.5, and writes `""` without one.
- **Template copies and pool workers** copy the template role's configured
  model. Router stays on Haiku 5.5 (SYRD-779).
- **A tenant's choices are kept.** Every explicit pick (above), every other
  model and every effort stays as configured.

No live tenant reads the bundled `pgu.json`. All six registered tenants (pgu,
otto, mefp, sapi, pie, syrd) have a root registry entry naming their own
`.switchyard/provision/<slug>.json`. The registry is authoritative (SYRD-709),
so the bundled file is read only for an unregistered `pgu`. A tenant whose own
configuration still names a stale `claude-opus-5` is moved at its next upgrade,
as above.

## Tests

`tests/role_model_selection_test.py`'s `retired` case covers the upgrade:
- a tenant on `claude-opus-5`: the dry run lists each Claude role and changes
  nothing;
- an upgrade whose phases fail moves nothing and changes no file;
- a successful one moves the model only, and marks the tenant and each moved
  role;
- each role then resumes on 5.5;
- ops, still on Opus 5, is no drift, and a reload goes on;
- once ops has run 5.5, Opus 5 again is drift;
- an Opus 5 set after the move survives the next upgrade;
- a recorded Opus 5 choice isn't moved;
- a recorded 5.5 pick followed by a session on Opus 5 is drift;
- a tenant made at catalog v2 moves nothing;
- a Sonnet 5.5 pick is still refused, and nothing moves.

`tests/opus_default_model_test.py` covers four paths:

- **The catalog:** one Claude default, and Opus 5 still offered.
- **`switchyard new`:** the real role question records Opus 5.5, both with Enter
  at a terminal and with nobody to ask, and the real config writer writes it.
- **`switchyard add-role`:** the real command, against a generated config,
  writes Opus 5.5 for a Claude role and no model for a Codex one.
- **The bundled `pgu.json`:** the real loader reads it with every Claude role on
  Opus 5.5.
