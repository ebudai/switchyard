# A fresh tenant that stopped in one Claude window

A fresh Zorin provisioning of `test2` opened a single standalone Claude window
and went no further. The `test` tenant before it had launched its five-pane
viewer without trouble.

Two separate defects, both in the first-run phase SYRD-211 built, and both
measured against the real CLI before anything was changed.

## The setup window closed while the question was still on it

Claude Code v2.1.270 opens its first run like this:

```
Welcome to Claude Code v2.1.270
Let's get started.
Choose the text style that looks best with your terminal
  1. Auto (match terminal)
❯ 2. Dark mode ✔
  3. Light mode
  ... seven options, with a live syntax preview below
```

`provider_is_waiting_for_an_answer` returned **false** on that screen. It was a
list of phrases, and it contained `select a theme` and `choose a theme` while
this release says *"Choose the text style that looks best with your terminal"*.
Nothing matched, so after six seconds of quiet the watcher concluded the
opposite of the truth -- "claude has finished its first run and is at its
ordinary prompt" -- closed the session, and read the account back to find
`hasCompletedOnboarding: None` and `theme: None`.

Six seconds is less time than it takes to read a seven-item list, so the window
went away under whoever was reading it.

`test` did not hit this because its owner account had already completed Claude's
first run, so the step was skipped entirely. `test2` is a fresh account, and the
first to meet a Claude that words that screen this way.

### What replaced it

The question is now read from the screen's **structure** as well as its wording:

- a selection cursor sitting on an option. The cursor alone is not enough --
  Claude draws the same glyph for its empty input box -- so it counts only when
  something follows it, which is the highlighted choice;
- a numbered menu of two or more options. One numbered line is not a choice
  between things, and line numbers are not options, which is why the dot
  matters: Claude's own theme screen draws a diff underneath the menu numbered
  `1`, `2`, `3` with no dots.

Both readings are evidence **for** a question and never against one. That
asymmetry is the point: a false positive makes Switchyard wait a little longer
for somebody who has already finished, and a false negative closes the window
while they are still reading it. Wording changes; a menu is a menu.

The phrases stay, because a question does not always have structure -- an OAuth
box is one line of prose, "Paste code here if prompted >", with nothing to read
but the words.

## The step after it never returned

That was only half of it. The login step ran `claude auth login` through
`_run_owner_cli_interactive`: a bare `subprocess.run` with no watcher, no
timeout and no completion predicate. Measured on a fresh account:

```
exited on its own: False (rc=None)  after 40.3s
   | Opening browser to sign in…
   | If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?...
   | Paste code here if prompted >
```

It does not exit. With no browser on that session to finish the flow, it sits
there indefinitely and holds the launch with it. That is the standalone window
the User was left in, and why no presentation ever opened -- the phase never
returned to open one.

The login step now uses `_run_owner_cli_until`, the same bounded runner the
setup and folder-trust steps already used, completing on the state it exists to
produce: `_cli_auth_status(...) == "authenticated"`.

There is an irony worth recording. That OAuth screen *does* match a phrase
marker, so the watcher would have handled it correctly all along. The step
simply did not use the watcher.

`_run_owner_cli_interactive` had no other caller and is gone. Its test moved to
the bounded runner, so the environment-scrubbing property it covered is still
covered, and now on code that ships.

## Telling the temporary window apart

Measured before implementing, because it decides whether this is possible:
Claude sets **no** terminal title while its first-run screens are up. It claims
one only when it reaches its ordinary prompt. So a title set by Switchyard
survives exactly the window that needs distinguishing.

The setup step names the window `Switchyard setup (temporary): claude first
run`, and gives the name back when it ends so a finished window stops claiming
to be one. Nothing is written when stdout is not a terminal: escape bytes in a
captured stream corrupt whatever is parsing it.

## Saying how to resume

An outstanding step used to name itself and stop there, which is a description
of a problem rather than a way out of one. It now carries the command that
finishes it and what to do afterwards:

> To resume: run `claude` as test2-agent, answer its own prompts to the end,
> then run the same switchyard launch again -- it picks up from whatever is
> already recorded and does not repeat the steps that are done.

That turned up a smaller defect on the way: `report.owner_user` was filled in
only for missing CLIs, stale hook trust or a broken owner shell. An incomplete
provider setup is none of those, so the resume line would have said "run this as
the owner account" without ever saying which one. It is now named whenever the
report tells somebody to become it.

## Verification

`tests/first_run_setup_completion_test.py`, 64 checks. The screens it reads are
recordings of the real CLI, kept in `tests/fixtures/claude-first-run/`, rather
than strings written from memory: the entire defect was a guess about wording,
so a test resting on another guess would prove nothing. One case asserts that
**no** phrase in the marker list matches the theme menu, so it is demonstrably
testing the structural path and not a phrase quietly added to make it pass.

The five cases the ticket asks for:

- **a fresh mixed Claude/Codex tenant** -- Claude's first run is offered once
  for three roles rather than once per role, and no provider is asked to sign in
  twice;
- **the standalone setup process exiting successfully** -- a provider that
  closes its own window is believed only when the account says so, checked both
  ways;
- **unexpected entry into an interactive Claude shell** -- the theme menu is a
  question by its structure, and the ordinary prompt still is not;
- **resume after interruption** -- once the first run is recorded the step is
  not offered again, while folder trust, which genuinely was not done, still is;
- **the final multi-pane launch** -- the phase returns a report even when steps
  are outstanding, which is what lets the caller go on and open panes. The
  reported failure was not a wrong message but no message at all.

SYRD-211's property is kept as an explicit regression: the ordinary prompt must
stay "not a question", or Switchyard waits ten minutes at a prompt nobody is
being asked anything at.

Two paths are driven on the shipped default rather than through the injected
runner every phase test uses, because that seam returns before any predicate is
consulted and so cannot see the defect at all: a provider that never finishes
must be given up on and reported, and a step already recorded must not start the
provider again. The predicate the phase wires onto the login step is captured
and exercised directly for the same reason.

Mutation coverage: 12 mutants across the structural predicate, the phrase path,
the login step's completion wiring, the resume instruction and the window title.
No survivors. Four survived a first pass and each named a real gap rather than a
missing assertion -- among them an assertion about naming the owner account that
was being satisfied by the warning's prefix rather than by the resume sentence
it was supposed to be about.

## Reopened: the wait was still silent

User acceptance on a freshly provisioned `test3` reported the same thing again:
stopped in a standalone Claude window. It had, and the reason is that the fix
above changed *which* window it stopped in rather than removing the stop.

What was never measured the first time is what Claude does **after** the theme
question is answered. Driving a fresh account on a pty and actually answering
it:

```
[ 0.5s] Welcome to Claude Code v2.1.270 / Choose the text style...  onboarding=None theme=None
[12.0s] >>> pressed Enter to accept the highlighted theme
[12.5s] "...Select login method: ❯ Claude account with subscription"  onboarding=None theme=None
final state: onboarding=None theme=None
```

Two things follow from that transcript.

**The `theme` half of the completion check was dead.** `_claude_account_setup_complete`
accepted either `hasCompletedOnboarding` or a non-empty `theme`. Answering the
theme prompt writes **neither** -- not during the session, not after it exits.
And a genuinely onboarded account on this host has no top-level `theme` key at
all; it carries `hasCompletedOnboarding: true` and `lastOnboardingVersion`. So
`theme` never fires for a real first run, and had some path written one
mid-flow it would have reported success while the account was still half set up
-- which is the original defect wearing a different hat. It is gone, and a test
pins that a theme alone is not a completed first run.

**Which means the step necessarily waits for a full interactive sign-in.**
`hasCompletedOnboarding` is written at the end of the whole flow, sign-in
included. Before this change the step printed its instruction, started Claude,
and then said nothing at all for up to `FOREGROUND_COMPLETION_TIMEOUT_SECONDS`
-- ten minutes -- before giving up.

Ten silent minutes is precisely what this ticket forbids: setup must have "an
explicit purpose and completion lifecycle", and where it cannot continue
Switchyard must "keep the controlling command alive long enough to report the
exact incomplete step and a resumable next action rather than silently leaving
one unrelated window". Correctly recognising the question and then waiting in
silence is indistinguishable, from the far side of the screen, from having hung
-- and it lasts a hundred times longer than the six-second close it replaced.

### Making the wait speak

Claude draws inline, and it draws continuously, so anything Switchyard writes to
that terminal is scribbled over by the next redraw. The terminal **title** is
the one channel the CLI does not contend for -- measured in the section above,
Claude claims a title only once it reaches its ordinary prompt, which is exactly
the moment this wait ends.

So the wait now republishes the title every five seconds with the time left:

```
Switchyard setup (temporary): claude first run
Switchyard setup: answer claude's prompts to the end -- 9m32s left
Switchyard setup: answer claude's prompts to the end -- 9m27s left
...
```

It says what is wanted ("answer claude's prompts to the end"), and the countdown
says the thing a static title cannot: that this is still running, and how long
it will keep running. A window that is counting down is not a window that has
hung. The countdown is written the way a person reads a clock -- `9m32s`, then
`45s` near the end -- rather than as a raw number of seconds.

Giving up used to say only that the clock had run out. It now names what was
outstanding:

> claude did not record its first run, so it was still asking for something when
> time ran out -- most often the sign-in that follows the theme question. The
> CLI was ended and the run continues; the step and how to resume it are
> reported below.

That is the "exact incomplete step" the ticket asks for, and it hands off to the
resume instruction that was already there. The run then continues to the
presentation rather than stopping, so the User ends up in front of Switchyard
with a report, not alone in somebody else's CLI.

### Verification of the reopened fix

`tests/first_run_setup_completion_test.py`, now 79 checks, adds three cases: a
theme alone is not a completed first run; the window says what it is waiting for
while it waits, republishing as the countdown falls and ending with a give-up
message that names the sign-in and a way to resume; and the countdown reads as
time a person recognises.

Driven against the **real** `claude` on a pty with a throwaway `HOME` and a
shortened deadline, the titles the window actually claimed, in order:

```
Switchyard setup (temporary): claude first run
Switchyard setup: answer claude's prompts to the end -- 16s left
Switchyard setup: answer claude's prompts to the end -- 11s left
Switchyard setup: answer claude's prompts to the end -- 6s left
Switchyard setup: answer claude's prompts to the end -- 1s left
Switchyard
```

Mutation coverage over the new behaviour: accepting a theme alone, never
publishing the status, publishing it once and then going quiet, a countdown
stuck at zero, and a give-up message that says only that the clock ran out. All
five killed, no survivors.

## Reopened again: one narrated window out of several

User acceptance rejected the repair-and-resume path outright: acceptance is a
brand-new tenant, provisioned from nothing by `switchyard new`, completing each
provider's first run once and opening the full presentation. Reusing `test2` or
`test3` does not count.

Asking what a brand-new tenant meets that the section above does not cover
finds it immediately. `set_terminal_title` was called in exactly one function,
`run_provider_first_run_session` — and `run_first_run_auth_phase` runs three
kinds of foreground provider step:

| step | runner | per | narrated before this |
|---|---|---|---|
| `provider_setup_steps` | `run_provider_first_run_session` | provider | yes |
| `login_steps` | `_run_owner_cli_until` | provider | **no** |
| `folder_trust_steps` | `_run_owner_cli_until` | **worktree** | **no** |

`_first_run_trust_command` is `list(role.cli)` — a bare `claude`. So the trust
step opens a plain standalone Claude window, once per role, each able to sit for
`FOREGROUND_COMPLETION_TIMEOUT_SECONDS` = 600s.

Measured by driving `_run_owner_cli_until` exactly as the trust step calls it,
against the real CLI on a pty with a throwaway `HOME` and a 20s deadline:

```
captured 2732 bytes in 20.1s
--- terminal titles claimed, in order ---
   OSC 0 | (empty)
--- did it say anything while waiting? ---
   'gave up waiting': 1
   'Switchyard setup': 0
   'left':            0
   'resume':          0
```

Three things in that.

**The window never says it is temporary setup.** The ticket asks for the setup
window to be distinguishable from the presentation "in both terminal output and
window title"; for two of the three step kinds it was not distinguished at all.

**The one title event is Claude *clearing* the title** — an empty `OSC 0`. The
window is left with no name whatsoever, which is worse than a wrong one: there
is nothing to tell it from any other terminal. It also settles a design
question. A name set once before the CLI starts does not survive it. Only a
republished name does, which means the countdown is not decoration on top of
the title — it is the mechanism that makes the title exist at all.

**Giving up carried no resumable action**, only the generic "the step is
reported as outstanding below".

So the earlier fix narrated one window out of several, and the silent ones
outnumber it because folder trust is per worktree. A fresh multi-role tenant met
one window that explained itself followed by a run of anonymous ones — which is
what "stopped in a standalone Claude window" describes.

### One narrator, three windows

The behaviour is now a single `_SetupWindowNarrator` rather than three copies:
`opened()` names the step, `tick()` republishes the name with the time left,
`stalled()` composes the give-up message, `closed()` gives the name back. Both
`run_provider_first_run_session` and `_run_owner_cli_until` drive it.

The title carries which step it is, because "Switchyard setup" repeated five
times does not tell somebody how far through they are:

```
Switchyard setup (temporary): claude first run
Switchyard setup (temporary): claude sign-in
Switchyard setup (temporary): claude folder trust
```

and giving up names the answer that is still missing, per step — not the clock:

```
first run    -> did not record its first run ... most often the sign-in that
                follows the theme question
sign-in      -> did not record a signed-in account ... most often an
                authorization code that was never pasted back
folder trust -> did not record trust for that directory
```

The same measurement against the real CLI, after the change:

```
--- terminal titles claimed, in order ---
   OSC 0 | (empty)                                    <- Claude clearing it
   OSC 0 | Switchyard setup (temporary): claude folder trust
   OSC 0 | Switchyard setup: answer claude's prompts to the end -- 14s left
   OSC 0 | Switchyard setup: answer claude's prompts to the end -- 9s left
   OSC 0 | Switchyard setup: answer claude's prompts to the end -- 4s left
   OSC 0 | Switchyard
--- did it say anything while waiting? ---
   'Switchyard setup': 4
   'left':             3
   'resume':           1
```

### Verification

`tests/first_run_setup_completion_test.py`, now 123 checks. The new cases drive
the real bounded runner rather than the narrator in isolation: every setup
window says which step it is and no two steps claim the same name; a bounded
step counts down and gives the name back; giving up names the step that did not
finish and points at a way to resume, differently for each step; and a step that
completes is not reported as stalled.

One case drives the **phase**, not the runner, with `_run_owner_cli_until`
recorded: a runner that narrates perfectly still produces anonymous windows if
the phase never tells it which step it is running, so both call sites are
checked for a purpose that matches what the step actually is.

Mutation: 20 mutants across the narrator, both call sites, the phase wiring, the
countdown, the per-step explanations and the structural question detector. No
survivors. Among them: a sign-in window claiming to be folder trust, the phase
passing no provider name, and `opened()` returning without writing — each of
which leaves the User looking at exactly the window this ticket is about.

### What this does not do

It makes every setup window legible. It does not remove the need for one.
Claude writes `hasCompletedOnboarding` only after an interactive sign-in, so a
brand-new tenant still requires somebody to sit at that window and finish it,
and folder trust is still asked once per worktree. If the acceptance criterion
is a fresh tenant reaching its presentation with no human step at all, that is a
different change and is not in this layer.

## Reopened a third time: a step that could not see the screen

The brand-new `test4` tenant failed, and its report is precise enough to be a
diagnosis on its own: after the User completed the theme and sign-in flow
without `/exit`, Switchyard "advanced only to another standalone Claude Code
session and left it sitting indefinitely at the ordinary prompt. It did not
detect completion, close the temporary window, or continue to the full
multi-role presentation." The session showed Auto mode, so it was a setup
window and not a presentation pane.

Two of those facts together are the whole thing. The CLI was **at its ordinary
prompt** — the person had finished answering. Switchyard **did not notice**.

### Why it could not notice

`_run_owner_cli_until` started the CLI with a bare `subprocess.Popen`
inheriting stdio. It never read the screen. So it had exactly two ways to end a
step: the state file it watches, or the 600-second deadline.

The provider's own first run has a **third**, and has had since SYRD-211 — gone
quiet, asking nothing, therefore done. That third answer exists precisely
because live UAT had already shown what its absence looks like: "the questions
were answered, Claude sat at its prompt, and Switchyard waited ten minutes for
a key that is not written until exit."

The sign-in and folder-trust steps never got it. And `_first_run_trust_command`
is `list(role.cli)` — a bare `claude` — which is also why the window showed Auto
mode rather than the role's bypass-permissions runtime.

The code's own give-up message had anticipated this exact case:

> If the CLI showed no prompt at all, it already considers this done and
> Switchyard is reading the wrong state -- say so, because that is a defect
> here and not something to answer again.

The User's report is that sentence coming true. Rather than leave the User to
notice it and report it, the step now notices it itself.

### Reproduced, then re-measured

Driving `_run_owner_cli_until` as the trust step calls it, against the real
`claude`, in a directory it already trusts so it opens straight at the ordinary
prompt, with the predicate returning False throughout — the UAT's "did not
detect completion":

```
elapsed=25.5s of a 25s budget        <- burned the entire deadline
```

with `⏵⏵ auto mode on` on screen, the same thing the UAT saw. After the change,
same probe, same directory, predicate still always False:

```
elapsed=7.5s of a 90s budget
switchyard: claude is at its ordinary prompt with nothing left to ask about
this directory, so this step is done; closing it and carrying on. You do not
have to exit anything.
```

Ten minutes of a standalone window becomes seven seconds and a sentence, and
the run goes on to the presentation.

### What was ruled out on the way

Recorded so the next pass does not re-tread them:

- `claude auth status --json` and `claude auth login` both exist and work on
  v2.1.278; `auth status --json` reports `loggedIn: true`, so a signed-in
  account correctly skips the sign-in step. The login step was not the culprit.
- The real ordinary prompt is **not** misread as a question by the widened
  structural detector. Checked against the live screen: `False`. The widening
  from the first fix is not the cause.
- The ordinary prompt **does** go quiet — measured gaps of 9.9s, 8.0s and 27.1s
  against a 6s threshold. Nothing was wrong with the detection; there was no
  watcher on these steps to do it.
- Answering the trust dialog "Yes" records `hasTrustDialogAccepted` where
  Switchyard looks, and the step then ends in about 1.4s. The happy path was
  never broken. The stuck path is when the CLI does not write what the step
  watches for.

### One runner

`_run_owner_cli_until` now runs its step through the same watched pty session as
the first run, so all three foreground steps share one runner and one narrator.
A step ends when its state is recorded, **or** when the provider has gone quiet
at a screen asking nothing, or at the deadline.

The "nothing left to ask" message is per step, for the same reason the stalled
message is: a folder-trust step reporting "finished its first run" would be a
guess dressed as a fact.

Removing the duplicate loop removed the `popen` seam with it. That seam existed
only for tests — production always built a pty session — so every test double
now implements the session the shipped path actually drives, rather than a
narrower thing nothing in production uses.

### Verification

`tests/first_run_setup_completion_test.py`, 139 checks. The central case is the
UAT as a test: the predicate never fires, and the window must still close
without burning the deadline. Its screen is a fresh recording of the real CLI at
the Auto-mode prompt — `tests/fixtures/claude-first-run/ordinary-prompt-auto-mode.txt`,
the reported screen itself.

Guarded in both directions, because the screen is evidence either way: an
unanswered trust dialog must still hold the window open, or this change would
reintroduce the original defect of a window closing under somebody still reading
it. And a step whose screen cannot be read must still be bounded rather than
hang.

Mutation: 16 mutants, no survivors — among them the bounded step going back to
not reading the screen, a quiet screen never ending a step, an unanswered
question no longer holding the window, and every step claiming it finished a
first run.

One mutant survived a first pass: the blind-session adapter inventing output.
It could not be killed because that adapter was reachable only from tests, and
the answer was to delete it rather than to assert around it.

## Reopened a fourth time: none of the above ever ran

`test5`, a brand-new tenant on the previous candidate, failed identically to
`test4`: the Claude process reached its ordinary Auto-mode prompt and stayed
there, and — the detail that gives it away — "the title remained `✳ Claude
Code` with no republished Switchyard setup countdown."

No countdown means no narrator. No narrator means the watched session was never
constructed. Everything the three previous sections describe was live-path code
that the live path never reached.

### The mechanism

`run_first_run_auth_phase` reads its `runner` argument as *"the caller is
driving these steps itself"*:

```python
injected_runner = runner
runner = runner or subprocess.run
...
if runner is not None:          # in the foreground step runners
    runner(args, **kwargs)      # fired and forgotten
    return is_complete()
```

Its own docstring said so: *"None means the live path: probes run through
`subprocess.run`, and the foreground steps are bounded rather than fired and
forgotten."*

But both live entry points passed a runner, because their own parameter
**defaulted to `subprocess.run`**:

- `switchyard_new_command(..., runner = subprocess.run)` → `run_first_run_auth_phase(..., runner=runner)`
- `run_switchyard_launch_first_run_auth(..., runner = subprocess.run)` → the same

So on a real launch every foreground provider step was a plain blocking
`subprocess.run` with inherited stdio. No pty. No watcher. No title. No
countdown. No quiet-screen classification. No deadline. It returned when the
CLI exited — and an interactive Claude at its ordinary prompt does not exit
until somebody types `/exit`, which is the one thing this ticket forbids asking
for.

Demonstrated by driving the real phase over a stub CLI that prints a prompt,
claims the title `✳ Claude Code`, and sits there:

```
--- runner=subprocess.run   (what `switchyard new` passed) ---
  Switchyard titles    : 0
--- runner=None            (the documented live path) ---
  Switchyard titles    : 1
      | Switchyard setup (temporary): claude first run
```

### Why three rounds of testing missed it

Every phase test injects a runner, because that is how a suite avoids launching
a real CLI. So the injected branch was covered twice over and the branch that
ships had no coverage at the phase boundary at all.

My own measurements had the same shape: I drove `_run_owner_cli_until` and the
session directly with `runner=None`, so I was exercising the live path's code
while production took the other branch. Every result was real and none of it
was reachable.

### The change

`runner` conflated two questions — which runner probes use, and who drives the
interactive steps. A live command needs the first and not the second, and had
no way to say so. They are now separate: `run_first_run_auth_phase` takes
`foreground_runner`, defaulting to a sentinel meaning "same as `runner`" so
every existing caller and suite is unaffected.

Both live commands go through one helper, `run_first_run_auth_for_launch`,
because the decision is invisible at a call site and getting it wrong is
silent. Spelling it out at each command is how both of them came to pass
`subprocess.run`.

The entry points default `runner` to a sentinel rather than `subprocess.run`,
so "nobody injected anything" stays distinguishable from "somebody injected the
default". And in `switchyard_new_command` the capture and the resolution are a
single statement, because a capture-then-resolve pair is one editing accident
away from capturing the resolved value and unwatching everything again.

A first version of this also refused the ambiguity outright: passing
`subprocess.run` to the shared helper raised. That was wrong, and Director
review caught it before release — see "The refusal that broke two callers"
below.

### Verification

157 checks. The ones that matter are at the boundary that had none:

- the phase does not hand the foreground to the probe runner — driven with a
  real (stubbed) provider on the owner's PATH so the watched branch genuinely
  runs, asserting that the bare CLI and the vendors' login commands never reach
  the injected runner;
- the shipped launch watches its setup windows, and an injected runner still
  drives every step, so no suite starts launching real CLIs;
- one place decides, tested both ways;
- neither entry point can confuse "no runner" with "the default one";
- the ambiguous runner is refused.

Mutation: 13 mutants, 12 killed. The survivor is recorded honestly below.

A first pass had three survivors, all in this area, and they were the point: my
first attempt tested only what the wrapper *passed*, not what the phase *did*
with it, and not the `switchyard new` path at all — the same blind spot one
layer up. The phase-level case also silently tested nothing at first, because
its stub runner reported the CLI as not installed and the phase skipped every
interactive step; and once it did run them it launched a real `codex login`
OAuth server, because only `claude` had been stubbed. Both are fixed: the
runner reports installed-but-unauthenticated, and every provider is stubbed.

### The one mutant still standing

Reordering the capture and resolution inside `switchyard_new_command` — so it
keeps the resolved runner and unwatches every window — is **not** killed by any
test. That function creates Unix accounts and repositories, so driving it far
enough to reach the phase call is not possible in a unit test here.

What bounds it: the decision itself now lives in one covered helper, the
reorder-prone pair is a single statement, and the wrong value is refused at the
boundary with an error rather than silently accepted. If that line is ever got
wrong again the launch fails loudly instead of stranding somebody in an
unwatched window.

## The refusal that broke two callers

The first version of the fix above made the ambiguity loud: passing
`subprocess.run` to `run_first_run_auth_for_launch` raised a `ValueError`,
because that value is indistinguishable from "nobody injected anything" and
resolving it the wrong way unwatches every window.

Director review found it release-blocking, and rightly. Two live callers pass
exactly that value, legitimately, for their **probes**:

- `switchyard_validate_models_command` defaults `runner=subprocess.run` and
  passes it through `run_switchyard_launch_first_run_auth`;
- `scripts/workflow_launcher.py::prepare_role` resolves its own default to
  `subprocess.run` and passes it through the same function.

Both would have raised before authentication or trust ran. I had audited the
`switchyard new` path that failed UAT and not the others — the same mistake in
a different place: fixing what was reported instead of what was affected.

### Said outright instead of inferred

The refusal is withdrawn. Ownership of the windows is now a parameter of its
own wherever a caller can express it:

```python
def run_switchyard_launch_first_run_auth(
    config, *, validate_models=False,
    runner=subprocess.run,          # probes: ordinary, and correct
    foreground_runner=None,         # the watched path, by default
    print_func=print,
)
```

A caller passing `subprocess.run` for its probes now keeps its watched windows,
which is what both of those callers wanted all along and could not ask for.

`switchyard_new_command` is the one place that still has to infer, because its
`runner` does a dozen other jobs and the two questions cannot be asked
separately at its boundary. That inference is now a named, tested function,
`foreground_runner_for`, rather than an expression buried in a call.

### Why `switchyard new` cannot simply watch always

Tried, and measured rather than assumed. Making it watch unconditionally breaks
four suites immediately — `team_launcher_new_project_test`,
`team_launcher_project_artifacts_test`,
`team_launcher_switchyard_new_prompts_test` and
`team_launcher_agy_credential_seeding_test` — each failing inside
`subprocess._execute_child`, because they inject a runner, reach real foreground
steps, and would launch actual providers. The sentinel is what keeps a suite
driving its own steps while production gets a pty.

That is also a useful fact about the coverage: those four suites *do* drive
`switchyard new` through the first-run phase, which is why a mutant that makes
the decision return `subprocess.run` now dies there.

### Verification

160 checks. The new ones cover each live caller rather than reasoning about it:

- `switchyard validate-models` keeps the probe runner it was given **and**
  still watches its windows;
- the workflow launcher's `prepare_role` does the same, driven all the way to
  the first-run hop and asserted to have reached it — the first draft of that
  case stopped short at a `KeyError` on the workflow projection and proved
  nothing, so it now stubs only the projection and the worktree/hook steps that
  belong to other tickets;
- the injected-runner decision is tested for all three inputs, including a
  caller that deliberately injects `subprocess.run` and must not be overruled;
- the launch wrapper's `foreground_runner` default is pinned to the watched
  path.

The real, pre-existing `test_switchyard_validate_models_command_runs_model_validation_on_demand`
passes unchanged, which is the direct check that probes still reach the runner
exactly as before.

Mutation: 14 mutants, 13 killed, with `team_launcher_new_project_test` added to
the harness so the `switchyard new` decision is under mutation at all.

### The one mutant still standing

Handing `foreground_runner_for` the resolved runner instead of the caller's
original, at the one call site inside `switchyard_new_command`, is killed by
nothing. Under any injected runner the two are the same value, so only the live
path differs — and that path creates Unix accounts and repositories, so it
cannot be driven from a unit test here.

It is smaller than it was: the decision itself is a named function tested for
every input, and the only uncovered step is which variable is handed to it.

## The proxy was not invisible

`test6`, a brand-new tenant, proved the watched path is finally live — the
window title read `Switchyard setup (temporary): claude first run` — and the
same screenshot showed the next defect: raw `^[[...` drawn into the theme menu.

A pty proxy that leaves the outer terminal in canonical mode with echo on is
not a proxy, it is a second voice on the same screen. There was no `termios`,
`tty` or window-size handling anywhere in the file.

### What the terminal was doing

Claude asks the terminal questions — primary device attributes, the kitty
keyboard protocol. A real terminal answers on Switchyard's stdin. The line
discipline then **echoes those answers**, caret-rendered, into the middle of
whatever the provider is drawing. Canonical mode also withholds each keystroke
until Enter, so the arrow keys the theme menu is driven with never arrive.

Reproduced by putting a terminal that answers queries in front of the real CLI:

```
bytes=2302  queries answered=2
caret-rendered control sequences visible on screen: 2
    '^[[?62;1;4c'
    '^[[?0u'
VERDICT: CORRUPTED -- echoed control bytes are on the screen
```

Those are exactly the `^[[...` in the UAT screenshot. After the change, the
same probe against the same CLI:

```
bytes=2263  queries answered=2
caret-rendered control sequences visible on screen: 0
VERDICT: clean -- no echoed control bytes
```

and the terminal is handed back byte-identically (`termios` compared before and
after: equal).

### Three things a proxy owes the terminal

**Raw mode, taken before the provider starts.** Nothing echoed, nothing
buffered, every byte passed through once — and restored whatever happens,
because leaving somebody's terminal raw is worse than anything this was fixing.
The *before* matters: a CLI asks its questions in the first milliseconds it is
alive, and the regression test caught a reply landing while the outer tty was
still echoing, in the one window where raw mode was not yet on. That ordering
was wrong in my first draft and the test found it, not reasoning.

**The window the person is actually looking at.** A pty opened cold is 80x24
whatever the real window is, so a full-screen CLI lays itself out for a
terminal nobody is looking at — which on the theme menu wraps the option list
into the preview below it. The inner pty now starts at the outer terminal's
size and follows it when the window is dragged mid-setup.

**Switchyard's own voice, in the terminal's own mode.** A line printed while
the outer tty is raw has no carriage return of its own and climbs the screen in
a staircase, over whatever the provider drew — so the message explaining what
went wrong arrives looking like more of the corruption it is explaining. Both
closing messages are now deferred and said once the terminal is back.

### Verification

176 checks. The new ones drive a real pty with a terminal in front of it that
answers queries the way a real one does: the replies must not reach the screen;
the terminal must be handed back in the mode it was lent in; the provider must
be given the window the person is looking at, and must follow it when it is
resized; and what Switchyard says must be said to a terminal in its own mode,
checked down both endings.

Mutation: 16 mutants, no survivors.

### Two things the mutants caught that I had got wrong

Both were silent no-ops in my own edits, and both would have shipped looking
fine.

**The deferred messages were never deferred.** The edit that was supposed to
turn `print_func(...)` into `closing_message = ...` did not match after an
earlier reindentation, so it did nothing: `closing_message` was dead, and both
messages were still printed into the raw terminal. The mutation run showed a
mutant that removed the deferred print surviving, which is only possible if
nothing was using it.

**The resize follow was never called.** `sync_window_size` was defined and
wired nowhere. The resize test failed on the real behaviour, not on a bad
assertion.

There was a third, subtler one. The staircase test passed even against a mutant
that printed into the raw terminal, because the harness inherited a suite's
**block-buffered** stdout: every message sat in a buffer until long after the
terminal had been handed back, so the mis-ordering was invisible. A real launch
writes to a terminal and is line buffered. The harness now reconfigures itself
to match, and the mutant dies. A test whose I/O discipline differs from
production's can agree with production and still be measuring something else.

## What the Zorin-local review found next

The control-sequence corruption is fixed and confirmed on the VM against the
real CLI. Two production pty defects remained, plus a latency measurement.

### A resize that signals nobody

The candidate copied the new size into the inner pty with `TIOCSWINSZ`, but the
pty had no controlling terminal and no foreground process group — `tcgetpgrp`
returning ENOTTY — so the kernel had nowhere to send SIGWINCH. Real Claude
started at 118 columns, the terminal was reduced to 62, and it kept drawing
116-column rules.

The provider is now made a session leader owning the pty it was handed, between
fork and exec. Measured here: `tcgetpgrp(master)` returns the provider's own
process group rather than 0.

That matters most for the shape that actually ships. A provider is launched
across a user boundary through `sudo -u`, which puts it in a session of its
own, so it cannot pick up the outer terminal's signals by inheritance the way a
same-session child does. Reproducing the defect locally needed that shape: with
the provider spawned into the outer session, a resize reaches it anyway — and
my first probe said "delivered" for exactly that reason, measuring the outer
terminal's own SIGWINCH rather than the proxy's.

The shipped test now drives the cross-user session shape, and proves delivery
by having the provider **trap** SIGWINCH rather than re-read `stty size`. The
old test observed only what the kernel had stored, which is true whether or not
anything was ever told.

### A killed Switchyard left the terminal raw

Measured after a SIGTERM: `icanon=False echo=False isig=False` — the person's
shell unusable, with nothing said about why. Python's cleanup does not run for
a fatal signal, so the normal, SIGINT, timeout and exception paths all restored
the terminal and the fatal ones did not.

SIGTERM, SIGHUP and SIGQUIT are now caught for exactly as long as the terminal
is raw: the handler hands the terminal back and re-raises, so a signal that
says stop still stops the run. SIGKILL is the one case nothing can help with.

### Interactive latency, assessed rather than deferred

The reviewer measured 0.60–1.00s round trip. Reproduced here at 0.80–1.00s, and
the cause is arithmetic: the loop slept a fixed `FOREGROUND_COMPLETION_POLL_SECONDS`
(0.5s) and relayed input once per iteration, so a keystroke waited up to one
interval to be passed on and its redraw up to another.

The loop now waits on the descriptors instead of sleeping blindly, with the
interval kept as an upper bound because the loop has work no descriptor will
wake it for — the countdown, the deadline, and the quiet window. Same probe
after the change: 0.00s, every sample.

### Verification

198 checks. Mutation: 15 mutants, 14 killed.

### Three tests that proved nothing until the mutants said so

**The fatal-signal case let the session finish first.** It used a stub that
goes quiet, so the ordinary-prompt path ended the step before the signal landed
— and it passed against a mutant that caught no signals at all. It now uses a
provider still holding a question up, and asserts the session did *not* finish
on its own.

**The kill only fired when there was output.** The harness checked its deadline
after `if not ready: continue`, and a provider holding a question up says
nothing for seconds at a time. Moved above the read.

**`tcgetattr` on a destroyed pty looks like a restored one.** With `pty.fork`
the parent holds no slave descriptor, so when the child dies the pty is torn
down and `tcgetattr` reports fresh defaults — indistinguishable from a terminal
properly handed back. The harness now builds the pty by hand and keeps its own
slave open.

That third one is the same lesson as the buffering one a section earlier: the
observation has to survive the thing it is observing.

### The one mutant still standing

Re-raising the signal without restoring first is not killed. In principle it
matters — restoring is what uninstalls the handler, so without it the re-raise
re-enters itself. In practice the process still ends up dying and the terminal
still ends up restored, through the ordinary unwind, on every path measured
here: terminal state, exit, and promptness within 0.5s. It is kept because it
is right, not because a test demands it.

### Not verified here

The cross-user launch runs the provider through `sudo -u <owner>`, and this
host does not authorise sudo from a candidate. The argv shape is asserted, and
the session work is applied to whatever is spawned — `sudo` becomes the session
leader owning the pty before it execs the provider, so the CLI inherits that
controlling terminal. That is reasoning, not measurement, and the VM is where
it can be measured.

## test7: three Codex roles started with no credentials

The Claude half now works on a fresh tenant. `test7` provisioned, launched, and
then said:

```
warning: switchyard: codex is still unauthenticated; affected roles: main, app, ops
```

A report, after the fact, of the thing it was supposed to prevent.

### One fix made the other defect

`codex login` draws this and then waits on a browser callback that may be
minutes away — recorded from the real CLI, in
`tests/fixtures/claude-first-run/codex-login-waiting.txt`:

```
Starting local login server on http://localhost:1455.
If your browser did not open, navigate to this URL to authenticate:
https://auth.openai.com/oauth/authorize?response_type=code&client_id=...
On a remote or headless machine? Use `codex login --device-auth` instead.
```

It asks nothing. It has no menu, no cursor, no prompt — and it says nothing at
all while it waits. So the quiet-screen path, the one added two sections ago to
stop Claude's folder-trust step hanging for ten minutes, read it as "at its
ordinary prompt", closed it after six seconds, and carried on. The sign-in never
completed.

That is worth stating plainly: the third answer — *gone quiet, asking nothing,
therefore done* — is right for a CLI sitting at its prompt and exactly wrong for
one waiting on a browser. Both look identical by silence alone, which is why the
difference has to be read from what the screen says.

A screen that has handed the sign-in somewhere else is now recognised as
waiting. Both markers are lines from the recording, and they are deliberately
two: either could be reworded, and the cost of the other still matching is that
Switchyard waits for somebody who has already finished — the side to be wrong
on.

A test now requires every marker to appear in some recorded screen. This ticket
has been reopened twice over phrases written from memory, and a marker nobody
has seen is a guess however plausible it reads. Writing the first version of
this list I added four such phrases; the grounding test is what makes that
impossible rather than merely discouraged.

### A role with no credentials must not start

The unfinished sign-in was reported as a warning *after* the launch. It is now a
gate before it, on both live paths — `switchyard new` and the daily `switchyard
<slug>` — naming the provider, the roles, whose account to finish it on, and
that re-running picks up from whatever is already recorded.

The first version of that gate read `report.unauthenticated`, a field that does
not exist; the real one is `unauthenticated_roles`. It would have raised
`AttributeError` at launch. The test caught it before anything else did, which
is the whole argument for writing the test alongside the gate rather than after.

### A character split across two reads

The captured output began `❯<?>switchyard:`. `read()` decoded each read on its
own, and a read lands wherever the kernel has bytes:

```
bytes of ❯: b'\xe2\x9d\xaf'
decoded as two reads: '��'
decoded whole       : '❯'
```

The relay now decodes across reads with an incremental decoder, so a character
drawn in three bytes survives arriving in two pieces.

### Verification

222 checks. Mutation: 9 mutants over this round, 8 killed.

The survivor removes one of the two sign-in markers. It survives because the
recorded screen matches both, which is the redundancy described above rather
than a gap — and a mutant that *adds* an ungrounded marker is killed by the
grounding test.

### Still not verified here

The `sudo -u <owner>` boundary. This host does not authorise sudo from a
candidate, so the cross-user behaviour of the sign-in steps — whether `codex
login` run as another user records its credentials where the probe reads them —
remains the VM's to confirm. What can be said from here is that the step is no
longer closed while it waits, and that no role starts without credentials.
