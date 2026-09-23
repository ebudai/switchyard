# A launch that asks no model to prove anything

Live `test17`, on the published SYRD-245 build. Claude's first run and every
folder trust were complete. Switchyard then checked designer, director and audit
(claude-opus-5), then main and ops (gpt-5.6-sol, Codex 0.152.1) — and returned to
the shell with no panes.

Main's Codex probe exited 0 without printing `model-ok`. Ops exited 0 and twice
answered without reading `switchyard-model-probe.txt`. Every model on that team
can call tools. What failed was the probe.

## What the probe cost

One live provider request per configured role, on the critical path of a first
launch, retried once when the answer came back without the token, and — since
SYRD-245 — bounded at 180 seconds an attempt. For a five-role tenant that is
five sequential live requests before a single pane opens, with a non-deterministic
outcome at the end of each.

And the outcome was a gate. `switchyard new` did:

```python
if first_run_auth_report.model_validation_failures:
    report_first_run_auth_warnings(...)
    return 1
```

so a capable model that happened to answer in prose refused the launch outright.

SYRD-244 gave that probe better evidence and SYRD-245 bounded its wait. Neither
is a reason to keep it in front of a launch, and the User's judgement here is the
decisive one: these are the models they chose, they can all read files, and the
probe is what is blocking the end-to-end test.

## What changed

**The normal path no longer probes.** `switchyard new` calls the first-run phase
without `validate_models`, and the launch-blocking gate is gone. `switchyard
<slug>` already defaulted to not probing, so it needed nothing.

The only caller that still turns probing on is `switchyard validate-models`, the
explicit diagnostic, and a case asserts that it is the only one — a boundary
somebody could quietly put back is worth holding.

**What stays is what is cheap and certain.** A CLI the owner cannot run, and an
account that is not signed in, still stop a launch before it opens panes onto
something unusable. Those cost one `command -v` and one status call, they are
never ambiguous, and both keep their cases here.

**The run says nothing was checked.** The ticket is explicit — do not claim model
capability was verified when no probe ran — and a launch that silently stopped
probing would look exactly like one that had probed and been satisfied. So it
says the opposite, once, and only when there is something it could have asked
about:

    switchyard: the configured models were NOT checked; nothing here asked them
    to prove anything. If one is wrong the provider says so in that role's own
    pane, in its own words. To ask on purpose:
    `switchyard validate-models <slug>`.

A tenant whose roles configure no model is told nothing, because a notice about a
diagnostic nobody needs is just noise.

## Verification

`tests/launch_without_model_probes_test.py`, 22 checks: an ordinary first-run
phase makes no live model request; a tool-blind model — test17's ops, exactly —
no longer stops anything; the notice says what it must and names the command; a
tenant with no configured model hears nothing; the explicit diagnostic still asks
once per role; a missing CLI and an unauthenticated provider each still stop the
launch; and no normal launch path asks for model validation.

Probe calls are matched on `MODEL_VALIDATION_PROMPT` rather than on a flag,
because the spelling differs per runtime (`-p`, `exec`, `-z`) and what this
ticket removes is the request, not one spelling of it.

Mutation, 3 mutants, all killed: putting `validate_models=True` back on the new
command; making the notice unconditional; and dropping the notice from the launch
altogether.

The third **survived** its first run. The notice had a case, but nothing proved
the shipped launch called it — the trap of testing a helper and not its caller.
Covered now by asserting the call's position in `switchyard_new_command`, between
the gates that can stop a launch and the line that opens the panes.

Suites re-run per case against a clean worktree at the base `041e328`:
`team_launcher_first_run_models_test`, `team_launcher_model_tool_call_probe_test`,
`model_probe_tool_call_evidence_test`, `first_run_trust_step_silence_test` and
`first_run_setup_completion_test` all pass in both trees. No broad sweep, per the
ticket.

## A limitation, stated

The call-site case reads the source rather than driving `switchyard new`. That is
deliberate: the command resolves the owner's home from the passwd database, so a
test that ran it far enough to reach this line would be writing into a real
`/home/<owner>`. It therefore checks position rather than behaviour — the
behaviour of the line itself is checked directly, and the two together are what
is available without a sandboxed tenant.
