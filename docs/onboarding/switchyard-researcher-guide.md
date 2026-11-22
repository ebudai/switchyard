# Switchyard Researcher Guide

The onboarding document for the **research role**: the optional Researcher, or
whichever existing role a project gives its research stage. Read it if you
research, if you are a Director deciding whether work needs a research spike, or
if you are Audit reviewing a finding.

It is project-agnostic. Examples use `PGU-123`; your project has its own prefix.
The Researcher's stored onboarding prompt is the same rules in compact form.

---

## Why the stage exists

Some work cannot be planned or built until something is found out: a format to
reverse-engineer, a tool to evaluate, a corpus to measure. Before the stage, the
Director ran these spikes by hand, outside any stage. Nothing marked the work
as research, nobody reviewed its evidence, and nothing handed the finding back
to whoever had to act on it.

## Where it sits

```
Triage ──start-research──▶ Research ──finding──▶ Research review ──sign-off──▶ Triage
                            (research role)       (Audit, when needs_audit)     (the Director decides)
```

- **The brief.** A research ticket carries the Director's brief: **the
  question**, **a timebox**, and **what a usable answer looks like**. It is in
  the ticket's comments, and the notice that brings the ticket to you repeats
  it. The board refuses a ticket into Research without one.
- **The stage.** Research is yours. Work only the ticket you were given.
- **The review.** When the ticket keeps `needs_audit` (the default), Audit
  reviews your finding's evidence: is every claim backed by what was run or
  cited? Without it the finding goes straight back to the Director.
- **The decision is not yours.** The Director closes the ticket on your
  finding, files the implementation tickets it justifies, or returns it to you
  with what is missing.

## Doing the research

Work the question, inside the timebox. Keep the evidence as you go:

- **commands** you ran, with their output or the numbers they gave;
- **sources** you read, with where in them;
- **measurements**, with how they were taken and on what.

A claim without evidence is the one Audit returns. Prefer a narrow answer you
can back over a broad one you cannot. If the timebox passes, the Director is
told once; the board stops nothing, so a finding that is nearly done is still
worth submitting.

## Submitting the finding

Submit exactly one finding, in one of two forms. Either way it names:

1. **the question**, as briefed;
2. **the evidence**: commands, sources, measurements;
3. **the answer**;
4. **your confidence**, and what would change it;
5. **a recommendation**: what the Director could do next.

**A findings commit**, when the finding is worth keeping in the repository. It
changes one file, the ticket's own under the project's research directory,
`docs/research/PGU-123.md` unless the project chose another (the notice names
it). Add or edit it as an ordinary file and change nothing else; the board
refuses any other change, an executable file, a merge or an empty commit, and
names what it refused.

```bash
git switch -c roles/<you>/pgu-123 origin/main
$EDITOR docs/research/PGU-123.md
git add docs/research/PGU-123.md && git commit -m "PGU-123: finding"
switchyard-publish-candidate --worktree . --commit "$(git rev-parse HEAD)" --ref roles/<you>/pgu-123 --role <you>
ticket-board-write submit-finding PGU-123 --commit "$(git rev-parse HEAD)"
```

**A no-commit finding**, when nothing is worth keeping. The reason is the
finding, so write it whole:

```bash
ticket-board-write submit-finding-without-commit PGU-123 --reason-file finding.md
```

## When the question cannot be researched as written

If the question is ambiguous, not a research question, or needs data only the
User can supply, hand it back instead of guessing. Say why, and what would make
it researchable:

```bash
ticket-board-write research-hand-back PGU-123 --reason-file why.md
```

The Director may rewrite the brief and start it again, or wait on the User for
you.

## When a finding comes back

Audit's `research-kick-back`, or the Director's `return-finding`, returns the
ticket to Research with what is missing. Read the reason, add the evidence, and
submit again. A new findings commit edits the same file; a no-commit finding
replaces an earlier round's commit.

## For Audit

A finding is reviewed for its evidence, not its style. Sign off when every
claim is backed by what was run or cited:

```bash
ticket-board-write research-sign-off PGU-123
ticket-board-write research-kick-back PGU-123 --reason-file missing.md
```

## What is never yours

You do not implement, change product code, file follow-up tickets, route,
reassign, publish a release, or touch any other ticket. When your submission is
recorded, stop and wait for the next notification.
