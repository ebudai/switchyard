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

`switchyard_new_command` supplies both. The runtime-edit path (`set-role-runtime`,
`add-role`) already ran in the owner's context.

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

## One decision worth a reviewer's attention

The ticket says "stop **that role** with an actionable selection/reconfiguration
message". This implementation stops **the whole launch**, for two reasons:

1. it is what the neighbouring gates do (`stop_before_launch_for_unauthenticated_providers`,
   the missing-CLI gate), and
2. starting five of six panes leaves a team silently missing its audit role,
   which is a state nobody chose either.

Per-role skipping would mean threading a skip list into `launch_project` and
leaving a dead slot in the tenant's layout. If Audit prefers the literal
reading, that is a contained change and I will make it — flagging it rather than
letting it pass unnoticed.

`unknown_model_roles` is also deliberately **not** counted in
`has_warnings` and not printed by `report_first_run_auth_warnings`: it does not
warn, it stops, and the gate says the whole thing where it is actionable.
Counting it as a warning with no warning line to print would be a report
claiming more than it shows.

## Evidence

`tests/owner_model_catalog_test.py` — 53 checks, new. It reproduces the
ticket's own situation with a `TwoAccounts` runner whose `agy models` answers
differently depending on who asks: the operator's list contains
`gemini-3.7-flash-high`, the owner's does not.

Cases: the root cause as a difference between two answers; a mismatch reported
and not rewritten; a listed model passing silently; a non-enumerating runtime
contradicting nothing; a failed listing contradicting nothing; recorded
provenance labelled `UNVERIFIED`; `agy` asked for no separate effort; the gate
refusing and changing nothing; a clean report stopping nothing; provisioning
offering the owner's models and never the caller's; end-to-end through
`run_first_run_auth_phase` with two `agy` roles, one flagged and one not; the
list read once however many roles share a runtime; no read at all when no model
is configured; and no read for an account that just reported itself
unauthenticated.

The call-count cases distinguish the catalog read from `agy`'s auth probe by the
kwargs `model_catalog` passes (`capture_output`), since the two argv are
identical. If that ever stops being true these cases go red rather than quietly
counting nothing.

**Mutation: 12/12 killed.** Each mutant changes one shipped decision — re-read
per role; ask the caller instead of the owner; drop `owner_args` in
provisioning; let a recorded table contradict a model; treat an unset model as a
mismatch; drop the owner's name from the report; start the role anyway; drop
"nothing has been changed for you"; read a catalog for a role with no model;
ask an unauthenticated CLI; prefer the recorded table over the live list; drop
the `UNVERIFIED` label. Every kill is a real `AssertionError` naming the
behaviour, not a `SyntaxError` and not an unmatched anchor.

### Neighbouring suite

`tests/team_launcher_first_run_models_test.py` pins the phase's exact call
sequence and legitimately gained one entry: the owner's `agy models`, at index
4, between the auth probes and the model-validation probes. That fixture's
runner already lists `gemini-3.7-flash-high`, so nothing is newly flagged there
— the only change is the extra call. Updated in place with the reason.

### Sweep

19 suites touching `runtime_catalog`, `run_first_run_auth_phase`,
`_prompt_switchyard_role_plan`, `FirstRunAuthReport` or the launch gates, run
serially under `env -i` against a clean `origin/main` worktree at `32654e7`.

Pass/fail is **identical per suite**. Thirteen are green on both. Six are red on
both, and were red on `origin/main` before this branch existed — they need
`sudo`/`unshare`, which this pane cannot provide. For each, the first failing
case and its line number are identical on both trees.

Because those suites halt at their first failure, every case of five of them was
additionally run independently on both trees: `team_launcher_first_run_hermes_test`
(6 cases), `team_launcher_onboarding_git_test` (10),
`team_launcher_switchyard_commands_test` (12), `desktop_access_test` (4) — all
identical, failures pre-existing.

`team_launcher_switchyard_resolution_test` is the one gap I could not close:
its per-case run dies at `sudo: a password is required` after six cases, on
both trees alike. Those six are identical. Of the five unreached,
`test_switchyard_validate_models_command_runs_model_validation_on_demand` is the
one in this ticket's area, so it was run on its own: it fails identically on
both trees at line 307, on the `output ==` warning-text assertion — not the
call-sequence assertion above it, which passes, and whose config uses `codex`
and so triggers no catalog read at all. The remaining four could not be reached
in this pane on either tree.
