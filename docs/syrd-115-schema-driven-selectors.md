# Choosing a provisioning value instead of recalling one

Switchyard's interactive provisioning asked operators to type values that have a
known, finite set. A role's runtime was chosen by reading the alternatives out
of the prompt text and typing one back:

    director CLI (claude/codex/agy/hermes) [claude]:

The implementer roles were one comma-separated line, so a typo in the middle of
it was a role nobody asked for and a role nobody noticed was missing. A model
was an identifier to remember — and there was no list of models anywhere in the
launcher to remember it from.

## What was already there

A numbered selector existed. `_prompt_choice` was added by SYRD-143 with the
right instincts — number or name, a named default, a bounded retry — and its
docstring says exactly why:

> A provisioning question whose answer is a path somebody has to author is a
> question most people cannot answer; a numbered list of what this host can
> actually do is one they can.

It had **one** caller. Everything else asked for text.

So this is less "invent an interaction" than "have one, and have the choices be
data". Three modules, stdlib only:

* `prompt_schema` — what a constrained question *is*. No I/O.
* `terminal_select` — the one place one is asked.
* `runtime_catalog` — where the option lists come from, and how fresh each is.

## Two distinctions that run through all of it

**A value is not a label.** `Choice.value` is the stable identifier that reaches
the artifact and the launcher configuration; `Choice.label` is for the person
reading the screen. Nothing stores a label, so rewording one cannot change what
a project runs.

**Not offered is not invalid.** A project configured a year ago may name a model
its runtime has stopped advertising. `with_existing_value` puts that value
first, marks it `not currently offered`, and lets Enter keep it. Dropping it
would offer an operator every option except the one their project uses and then
silently re-point the project at whatever they picked instead.

There is a third, in the catalog: **`None` is not empty.** A runtime with no
recorded models says so; a tenant whose selection was never recorded is not a
tenant that selected nothing. The same distinction SYRD-220 needed a week ago.

## The guided path

One question at a time, each narrowing the next:

    Implementer roles:
      1) main -- core/domain implementation and integration  [default]
      2) ops -- operations and tooling  [default]
      3) app -- application/UI work
      ...
      c) A role of your own
      (number, several numbers, Enter for the default, 'cancel' to stop)

then, per role, runtime → model → effort. The models offered are the chosen
runtime's. The effort question is **not asked at all** for a runtime that
discards it: `EFFORT_STYLE_BY_CLI["agy"]` is `None`, so an effort level never
reaches agy's command line, and a question whose answer is thrown away should
not be asked.

A whole project is now configurable with nothing but Enter — the ticket's first
acceptance criterion — and the review summary says what that chose before
anything is created.

## What a long catalog needs

A model list is the case that makes "show the choices" hard, so the selector
pages at twelve and filters on any typed text. Three real defects came out of
writing the tests for it, all of which would have selected the wrong model:

* after filtering, Enter still took the *original* default rather than the first
  visible entry — filter to the one model you want, press Enter, get another;
* the printed numbers are absolute (`13)` on the second page) but were resolved
  against the page, so a pick after `more` landed twelve entries away;
* `042` was read as list position 42. A number is now a position only when it is
  written the way a position is written; `042` is somebody typing part of
  `vendor/model-042`, and that ambiguity is now decided deliberately and tested.

## The catalog is a record, not a claim

Vendor model names drift, and the launcher already says so of its install
commands. Everything in `RECORDED_MODELS` is sourced, one line per entry, from
something in this repository — mostly `config/team-launcher/pgu.json`, the live
launcher configuration. A test re-reads that file and fails if a model this
project actually runs is missing from the table, so the comment above it cannot
quietly stop being true.

Nothing was invented to make the list look fuller. Hermes has **no** recorded
models because this repository has never held one: it authenticates by handing
the terminal to `hermes model`, its own picker. Its catalog is empty, says why,
and offers the custom path. `agy` is asked for its own list first
(`agy models`, the same output the launcher already parses) and falls back to
the recorded table when it cannot be reached — and a non-zero exit is not a
catalog no matter what it printed, because that refusal is how an unauthenticated
agy answers.

## A runtime switch is a dependent choice too

`switch_role_runtime` re-pointed the resume flags at the new runtime, for a
reason it states plainly: "resume semantics belong to the runtime, so a stale
one left behind would try to resume the new CLI with the old CLI's flag."

It left the **model** exactly where it was. A role moved from Codex to Claude
kept `gpt-5.5`, and the new runtime was started with the old one's model name.
A model belongs to its runtime for precisely the same reason, so it is now
recalculated on a switch, and an effort level is dropped when the new runtime
renders none.

## What was deliberately left as text

`push_policy` and `owner_shell` are constrained in spirit and have **no**
vocabulary anywhere in this codebase — only a default string each. Offering a
selector there would mean inventing the alternatives, and a list somebody made
up to fill the screen is worse than a text field, because it looks
authoritative. They stay as they were. `worktree_policy`, which has a declared
set (`WORKTREE_POLICIES`), is now a selector.

The open values — project name, slug, owner account, paths — stay text inputs,
which is what the ticket asks for.

## Verification

`tests/provisioning_selector_test.py`, 84 checks, one section per acceptance
item: defaults, multi-select roles, dependent choices, large catalogs, offline
fallback, stale configured values, cancellation, non-TTY operation, the review
summary, secret handling, the runtime switch, and the artifact round-trip.

Mutation, 8 mutants, all killed:

| mutant | killed by |
| --- | --- |
| no default when a field declares none | Enter selecting nothing |
| the filter does not move the default | Enter taking the pre-filter entry |
| `042` read as list position 42 | the padded-number case |
| a custom role joins the defaults instead of replacing them | two roles nobody asked for |
| an unlisted configured value goes last, not first | the project's own model not being the default |
| every runtime takes an effort level | agy keeping one |
| `model=""` and `model=None` treated alike | the old runtime's model surviving a switch |
| a failed model listing parsed anyway | — |

The last one **survived** the first run: the offline-fallback case used a runner
that exited non-zero *and* printed nothing, so the exit check was redundant to
it. That is a missing test rather than redundant code — an unauthenticated `agy`
prints its refusal, and parsing it would have offered `You` as a model. With
that case added, the mutant dies.

Existing suites were compared **per case** against a clean worktree at
`329588b`, because several are red on main already. At parity:
`team_launcher_project_role_prompts_test` (2 pre-existing),
`team_launcher_switchyard_new_prompts_test` (3), `team_launcher_add_role_vcs_test`
(2), `team_launcher_project_artifacts_test` (6), `team_launcher_design_test` (1),
`team_launcher_new_project_test` (1), `team_launcher_switchyard_commands_test`
(3) — same cases, same failing statements, in both trees. Green in both:
`role_runtime_test`, `role_onboarding_prompt_test`, `worker_pool_lifecycle_test`,
`desktop_policy_generation_test`, `team_launcher_adopt_registry_config_test`.

Two cases in `team_launcher_project_role_prompts_test` encoded the interaction
this ticket replaces — the free-text CLI retry cap and the comma-only role
retry cap. They are rewritten rather than deleted: both kept what they actually
protect, which is that the loop is bounded and that at least one implementer
role is required.

## Not verified here

**The terminal interaction itself.** This pane has no tty, so every case above
drives the selector through its injected `input_func`/`print_func` pair rather
than through a real terminal. What a person sees — the redraw, the cursor, how
a long list behaves in a small window — is the Inspector's to validate, and the
ticket asks for exactly that.
