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
