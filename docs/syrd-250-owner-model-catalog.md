# SYRD-250 — a model list belongs to an account, not to a host

## What the User saw

The live six-pane `test2` screenshot (2026-09-24): five panes up, and the
Antigravity audit pane sitting at its prompt having printed

```
model gemini-3.7-flash-high is not recognized as a known model or custom model
in settings. Ignoring the flag.
```

The pane started. The presentation worked. But `Ignoring the flag` means the
audit role was running on *something*, and not on the model stored in the
tenant plan — and nothing outside that pane said so.

## Why the slug is not simply wrong

`agy` 1.2.10 on the Switchyard development account lists
`gemini-3.7-flash-high` under `agy models`. So the value is not invalid in
general, and "fix the slug" is not the fix. The screenshot is from
`test2-agent`, a different account, whose `agy` settings and catalog differ.

That is the whole defect. **`agy models` is an account-scoped question**, and
SYRD-115 asked it of whichever account happened to be typing — on a `switchyard
new`, the operator, not the tenant owner. The operator was offered their own
models, chose one, and it was written into a plan that would be executed by an
account that had never been asked.

## The change

Two halves, because the mismatch can be created at two different moments.

### 1. Ask the owner when offering (provisioning and runtime edits)

`_prompt_switchyard_role_plan` now takes `owner_user` / `owner_home` and builds
an `owner_args` prefix with `_owner_command_env_args`, threaded down through
`_prompt_role_runtime_plan` into `_model_field`. The list an operator picks from
is produced by the account that will run the role.

`switchyard_new_command` supplies both — with the large caveat in "DAT round
two" below, since at that point the account does not exist yet.

The first version of this note claimed the runtime-edit path
(`set-role-runtime`, `add-role`) "already ran in the owner's context". That was
wrong: `set_project_role_runtime_command` passed no owner prefix at all, so a
director repairing a tenant was offered their *own* models — this ticket's root
cause, in the command meant to repair it. It is owner-scoped now.

### 2. Check what is already configured, before the pane opens

`run_first_run_auth_phase` reads the owner's own catalog once per CLI and
compares it with each role's configured model, filling
`FirstRunAuthReport.unknown_model_roles` as `(role, cli, model, available)`.
`stop_before_launch_for_unknown_models` then refuses the launch, naming the
role, the configured model, whose account was asked, and what that account does
offer.

This is the half that would have caught `test2`, whose plan was already written.

### What the check costs

One `agy models` per CLI per launch. No prompt, no token, no capability probe —
which is what distinguishes it from the probe SYRD-246 removed for delaying and
blocking launches. It is asked once however many roles share the runtime, and
not at all for a role with no model configured or for a CLI already reported
unauthenticated or missing.

It is the phase's *second* `agy models`, deliberately: the first is `agy`'s auth
probe, which runs before the phase has logged anybody in, so its answer can
describe an account that was not yet signed in.

## What may not contradict a configured model

Only a list the owner's CLI actually produced. `runtime_catalog` now separates
the two questions:

- `owner_model_catalog(runtime, ...)` → the live list, or `None`. `None` covers
  a runtime that cannot enumerate, no runner, a failed call, and unparseable
  output — all of which mean *this account has told us nothing*.
- `model_absent_from(catalog, model)` → the catalog, when it exists and does not
  contain the model; otherwise `None`.

So a recorded table never refuses anybody. Claude, Codex and Hermes expose no
machine-readable catalog, and an unauthenticated or broken `agy` has no opinion
either. `Catalog.enumerable` marks the distinction, and a recorded list now says
so in the words an operator reads:

```
UNVERIFIED: recorded 2026-09-23 (catalog v1) and not checked against this account
```

against a live one's `listed by agy models in this account`.

**Nothing is ever substituted.** The configured value is left exactly as it is
and the operator is told what their account offers. Picking a replacement here
would be the silent rewrite the ticket forbids — and the value may well be right
with the account simply not set up yet.

Effort is unchanged from SYRD-115: `agy`'s slug already encodes it, so no effort
question is asked for `agy`; the runtimes with a separate flag still get one.
The custom-value path (`allow_custom`) is retained on both fields.

## DAT round two: two normal paths that were still broken

The first candidate (99f5417) got the gate right and both normal paths wrong.

### 1. The account does not exist when the models are chosen

`switchyard new` prompts for every role's runtime and model at
`_prompt_switchyard_role_plan`, and only creates the owner account ~80 lines
later at `_ensure_owner_user_and_project_dir`. Nothing may be mutated before the
plan review, so that ordering is correct and has to stay.

The consequence is that at selection time there is no account to enumerate.
`sudo -u <new-owner> agy models` fails, `model_catalog` falls back to the
recorded table — and **the recorded `agy` table contains
`gemini-3.7-flash-high`**, the exact slug test2 was misconfigured with. So a
fresh `switchyard new` could still select it, and the gate would then refuse the
launch *after* provisioning, for a choice made from the only list the operator
was given. Worse, the note blamed the host (`run this on a host that can reach
the runtime`) when the host was fine.

My own fixture hid this: `TwoAccounts` answered as the owner whenever asked,
which is precisely what cannot happen yet.

Three changes:

- **Ask nobody when the owner cannot be asked.** `catalog_runner` is `None`
  unless the account exists. Passing the runner with an empty owner prefix would
  have enumerated *whoever is typing* and labelled it "listed in this account" —
  the original defect, reintroduced inside its own fix. My first attempt did
  exactly that, and the new fixture caught it.
- **Say which account and why.** `model_catalog` takes `unverified_because`, so
  the note reads `the <owner> account does not exist yet, so its own list could
  not be read; this choice is confirmed against it after the account is created`.
- **Confirm it once the account is real.** `confirm_unknown_models_with_owner`
  runs after `_ensure_owner_user_and_project_dir` *and* after the first-run auth
  phase — the first moment the catalog is a real answer — and offers the real
  list for any role the owner does not recognise, writing the answer with
  `record_role_model`. Refusing there would be correct and useless: the operator
  chose from the only list available to them.

Nothing is substituted. The refused value is **not** seeded as the default —
that would mean pressing Enter keeps the broken model — it is named in the line
above and reachable only through the custom path. A choice that is still
unlisted stays flagged for the gate; a cancelled selector leaves the value alone
rather than crashing a half-provisioned project; with nobody to ask, nothing is
chosen at all.

### 2. The remedy could not repair anything

The stop message named `switchyard set-role-runtime <role>`, which omits the
required `project`. Spelled correctly it still did nothing: `preflight.is_noop`
compared runtimes alone, and `set_project_role_runtime_command` computed a model
only when `runtime != current_runtime`. For test2's audit role — already on
`agy` — the command reported "no change" and kept the bad model. **There was no
supported way to repair it.**

- `RuntimePreflight` now carries `current_model`/`requested_model`, and
  `is_noop` is true only when neither half changes. A model-only change also
  earns the readiness and busy checks, because it restarts the role exactly as a
  runtime change does — gating those on "is the runtime changing" would have let
  a repair kill a role mid-turn without `--force`.
- `set-role-runtime` takes `--model`, checked against the owner's live list and
  refused rather than replaced when absent. With no `--model` and a configured
  model the owner does not offer, it offers the real list at a terminal and
  refuses with the exact runnable command when there is no terminal.
- Its model selector was **not** owner-scoped before, contrary to what I wrote
  in the first implementation note. It is now. That note was wrong and this
  corrects it.
- The stop message prints two commands, both complete:
  `switchyard set-role-runtime <project> <role> --cli <cli> --model <listed>`
  and the same with `--model ''` for the runtime's own default. A test parses
  the printed command with the real argparse parser and then runs it, so
  "it runs and repairs this" is checked rather than asserted.

### Still a whole-launch refusal

The ticket says stop "that role"; the gate stops the launch, matching
`stop_before_launch_for_unauthenticated_providers` and the missing-CLI gate, and
avoiding a team silently missing its audit role. Audit accepted this reasoning
in the first round. With the deferred confirmation in place it is also much
harder to reach: a fresh `new` now repairs the mismatch before the gate sees it.

`unknown_model_roles` remains out of `has_warnings` and
`report_first_run_auth_warnings`: it does not warn, it stops, and the gate says
the whole thing where it is actionable.

## Evidence

`tests/owner_model_catalog_test.py` — 77 checks. The account-scoped root cause,
and the ordering the first candidate assumed away: `AccountCreatedLater` fails
`agy models` exactly as `sudo` does for a user that does not exist, and flips to
answering once the account is created. Cases cover the honest note, the
provisioning prompt with a genuinely absent owner, the fact that the recorded
fallback contains the bad slug (which is what makes the deferred confirmation
load-bearing), the confirmation writing the owner's model, Enter *not* keeping
the refused value, no substitution with nobody to ask, a kept unlisted choice
still being refused, and a cancelled selector changing nothing.

`tests/role_model_repair_test.py` — 36 checks, new. test2's shape: a one-role
tenant whose audit role is on `agy` with the bad model. Covers the `is_noop`
defect at preflight level, the repair actually changing the model and restarting
the role, an unlisted `--model` refused with nothing written, the owner rather
than the caller being asked, a busy role still protected, and the printed remedy
parsed by the real parser and then executed to completion.

Its owner is deliberately an account that does **not** exist on this host:
`test2-agent` does, and the runtime journal derives from the owner's home, so
the first version of this suite tried to write into the live tenant's account.
It was refused by permissions; the fixture now nests the tenant under an
owner-named directory so every owner-derived path resolves inside the sandbox.

**Mutation: 16/16 killed** across both suites, foreground and batched. The
mutants cover both findings and their overlap: treat a future owner as askable;
enumerate the caller when the owner is absent; drop the honest note; report an
absent account as existing; ignore the reason in the catalog; never ask again
after creation; never write the answer; offer the refused model back as the
default; treat a still-unlisted choice as resolved; pick a model with nobody to
ask; call a model-only change "no change"; skip the readiness and busy checks
for one; never offer a same-runtime repair; take `--model` on faith; ask the
caller for the list; omit the project from the stop message; report a cancelled
selector as resolved. Every kill is a real `AssertionError` naming the
behaviour.

One mutant was retired rather than survived: an `owner_exists` test in the
`owner_args` expression had no observable effect, because withholding the runner
already prevents any enumeration. It was redundant and is gone — one guard, and
it is the one that does the work.

### Neighbouring suite

`tests/team_launcher_first_run_models_test.py` pins the phase's exact call
sequence and legitimately gained one entry: the owner's `agy models`, at index
4. That fixture's runner already lists `gemini-3.7-flash-high`, so nothing is
newly flagged — the only change is the extra call. Updated in place with the
reason.

### Sweep

25 suites touching `runtime_catalog`, `role_runtime`, `run_first_run_auth_phase`,
`switchyard_new_command`, `_prompt_switchyard_role_plan`, `FirstRunAuthReport`
or the launch gates, run serially under `env -i` against a clean `origin/main`
worktree at `32654e7`.

Pass/fail is **identical per suite**. The reds were red on `origin/main` before
this branch existed — they need `sudo`/`unshare`, which this pane cannot
provide.

Per-case comparison on both trees, since these suites halt at their first
failure:

| suite | cases | failing (same on both) |
| --- | --- | --- |
| `team_launcher_new_project_test` | 19 | 1 |
| `presentation_window_recovery_test` | 21 | 2 |
| `team_launcher_first_run_hermes_test` | 6 | 1 |
| `team_launcher_onboarding_git_test` | 10 | 3 |
| `team_launcher_switchyard_commands_test` | 12 | 3 |
| `desktop_access_test` | 4 | 1 |

`team_launcher_switchyard_resolution_test` remains the one gap I cannot close:
its per-case run dies at `sudo: a password is required` after six cases, on both
trees alike, and those six are identical. Of the five unreached,
`test_switchyard_validate_models_command_runs_model_validation_on_demand` is the
one in this ticket's area; run on its own it fails identically on both trees at
line 307, on the `output ==` warning-text assertion rather than the
call-sequence assertion above it. The remaining four could not be reached in
this pane on either tree.
