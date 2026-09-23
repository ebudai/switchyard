# A trust step that can only speak

Fresh Zorin acceptance on the disposable tenant `test15`. The window said:

    switchyard: claude is at its ordinary prompt with nothing left to ask about
    this directory, so this step is done; closing it and carrying on.
    switchyard: claude will now run in /home/test15-agent/test15-worktrees/audit
    as this project's owner so it can be trusted once for audit. Answer the
    trust prompt; the terminal comes back on its own once the answer is recorded.

and then nothing. Title still "Switchyard", a cursor, no Claude trust prompt.
`ps -u test15-agent` showed only the owner's services and one Python listener;
`pgrep -af '[t]est15'` found the board, the notify listener and a desktop-access
watcher. **No provider process at all** — nothing the announcement had promised.

## What is and is not claimed here

The report says the desktop-side launcher's own process identity and state were
not captured, so the exact wait point is unknown. Nothing below claims to have
reproduced it, and nothing infers a model-probe failure: the ticket forbids that
without process or output evidence, and there is none. The earlier missing Enter
cue after Codex's "Successfully logged in" is a separate thing and is not
touched here.

What *is* claimed: this step had two ways to produce precisely the screen that
was described, both reachable without a provider process ever existing, and
neither of them could say a word while doing it. Both are now impossible to have
silently.

## The terminal is taken before the provider is started

Deliberately, and SYRD-221 was right to: a CLI asks its questions in the first
milliseconds it is alive, and a reply arriving while the outer tty still echoes
is drawn on the screen. So `_RawTerminal` is entered, and only then is the
session constructed.

That ordering means a spawn that fails or never returns leaves exactly what
`test15` showed — a raw terminal, a cursor, no echo, no prompt. Driven against
the real step, a session factory that raises produced:

```
printed to the operator: []
written to the window  : []
RAISED: FileNotFoundError ... '/home/test15-agent/test15-worktrees/audit'
```

Nothing printed. Nothing on the window. A bare exception out of a raw terminal,
naming a path but not which CLI, which role, or which of a dozen worktrees. And
a factory that merely *hangs* wrote nothing at all, because the window was named
only after the session was constructed — so the step that most needs a name on
the screen was the one step guaranteed not to have one.

## Two changes

**The window is named before the provider is started.** `_SetupWindowNarrator`
now opens ahead of the spawn, so a step that never gets a provider still says
which step it is:

    Switchyard setup (temporary): claude folder trust

**A provider that cannot start says so, and the run continues.** The spawn is
wrapped; an `OSError` becomes a bounded diagnostic and the step returns "not
recorded" rather than raising:

    warning: switchyard: claude could not be started in
    /home/test15-agent/test15-worktrees/audit for its folder trust, so that step
    recorded nothing: [Errno 2] No such file or directory. Nothing was asked of
    you and nothing is waiting. The command was: sudo -u test15-agent env ...
    claude. The step and how to resume it are reported below; run that command
    yourself to see what it says.

Returning rather than raising matters: the phase already reports which roles are
still missing what, and that report is more use than a traceback — it names the
role and the resumable command, and the other worktrees still get their turn.

## The third arm: advancing when the answer is already recorded

The requirement is a disjunction — show the prompt, advance if trust is already
recorded, or stop with a diagnostic — and the middle arm had a gap the manifest
cannot close. The manifest is built at the *start* of the phase, before the
provider's own first run. That run records trust for whichever directory it was
answered in, so by the time the loop reaches that worktree the step can already
be done. The announcement went out anyway, and a step announced as a prompt that
then completes instantly reads, from the other side of the screen, as a stall.

The loop now re-reads trust before announcing anything:

    switchyard: claude already trusts /home/test15-agent/test15-worktrees/audit
    for this account, so that step is done; nothing to answer.

This is not speculative: the preceding line in the live transcript is Claude
finishing a directory, which is exactly when that state is written.

## Verification

`tests/first_run_trust_step_silence_test.py`, 20 checks, one per arm of the
disjunction and one per way it was breakable: a provider that cannot start says
so instead of raising; the window is named before the spawn; an already-recorded
answer reports done without a word; trust recorded by the first run mid-phase is
not asked for again; and the ordinary step still shows the provider's own prompt
and records the answer.

Mutation, 4 mutants, all killed:

| mutant | killed by |
| --- | --- |
| catch a different exception, so a failed spawn escapes | the bare `FileNotFoundError` reaching the caller |
| name the window after the spawn again | nothing on the window when the spawn fails |
| re-raise instead of reporting | the traceback instead of the diagnostic |
| drop the mid-phase trust re-read | the already-trusted worktree announced as a prompt |

The fourth **survived** its first run. The case written for it pre-recorded
trust, and a pre-trusted worktree is filtered out of the manifest entirely, so
the guard was never reached and the assertion could not fail. Rewritten around
the reachable path — trust recorded *during* the phase by the provider's own
first run — it kills the mutant. A fifth attempt missed its anchor and was
re-applied rather than counted.

Focused suites, per case against a clean worktree at the base `d0d4c32`:
`first_run_setup_completion_test`, `first_run_login_inheritance_test` and
`first_run_single_login_test` pass in both trees;
`team_launcher_project_role_prompts_test` fails at
`test_switchyard_new_prompts_roles_and_skips_designer_when_absent` identically
in both — pre-existing.

## Still open

**A spawn that blocks forever is named but not bounded.** The window now says
what it is, but the countdown only ticks once the watch loop is entered, so a
`Popen` that never returns leaves a titled window rather than a silent one.
Bounding the construction itself needs a watchdog around it and is not a narrow
change; it is called out here rather than done quietly.

**`test15` is untouched.** No sudo was run, no live configuration read or
written, and the tenant and its artifacts are preserved for diagnosis. The
authoritative fresh Zorin end-to-end acceptance remains SYRD-222.
