# SYRD-844: a reviewer's mutation run is scoped to what the ticket changed

## What went wrong

On sapi, Audit ran hundreds of mutants against the whole test corpus on ordinary tickets, 30 minutes or more each
time. Limiting it noticeably sped the project up. Nothing in Switchyard's shared reviewer guidance said how big a
reviewer's mutation run should be:
- the board skill and board guide said "a mutant of your own";
- the Director guide said "kill a mutant of your own";
- the methodology suggested mutation testing at milestone boundaries.

So a reviewer that values thoroughness took "all of it" as the default.

## The rule

Every body a reviewer reads now states the same rule:
- **Mutate what the ticket changed.** Mutants go on the lines and behaviours the candidate adds or changes, chosen
  from the risk the reviewer sees. They run against the tests covering that area: the ticket's own suites and the
  suites that exercise the changed modules. They never run against the whole corpus.
- **A few semantic mutants beat hundreds of syntactic ones.** On an ordinary ticket, a reviewer's own mutation work
  is a handful of mutants, each answering a review question. The implementer's recorded mutants are reused, not
  regenerated.
- **Scope, not time.** The User ruled out time limits: "sometimes a really long run is reasonable." The rule limits
  what the mutants target, not how long the run takes, so there is no duration cap and no reason owed before a long
  run. The existing hang protection is untouched (`switchyard-bounded`, and `tests/bounded_run.py`'s per-case
  deadlines). It stops a mutant that never ends, not a long but legitimate run, and the board skill points at
  `--expect` for a run that legitimately runs long.
- **A whole-corpus run is the exception:** the ticket's test tier is `full`, the project asks for one, or it is a
  milestone-boundary test-integrity pass.
- **A survivor is a finding**, whatever the run's size.

**The test tier.** The board skill and board guide state the rule where they explain the Director's test tier.
`partial`, or no tier, keeps the reviewer's mutation run scoped to the change; `full` lets the reviewer run the wider
plan.

## Where it is stated

| Body | Reader | Where |
|---|---|---|
| `skills/switchyard-board/SKILL.md` | every role, including Audit | the tier paragraph, and "Mutation testing: mutate what the ticket changed" after "Test evidence" |
| `docs/onboarding/switchyard-board-guide.md` | Audit's onboarding | the tier paragraph, and a block after the worked-examples table in "Test evidence" |
| `docs/onboarding/switchyard-director-guide.md` | the Director | after "Kill a mutant of your own" and its rules |
| `skills/switchyard-director/SKILL.md` | the Director | after the rerun reasons in Final Sign-Off |
| `docs/onboarding/adversarial-collaborative-methodology.md` | everyone | "Coverage chasing", next to the milestone guidance |
| `docs/llm-operations.md` | index | next to the test-evidence rule |

**Audit has no generated role prompt.** `switchyard new` and `add-role` ship an onboarding prompt only for the
Director (`project_onboarding.director_onboarding_seed_text`), the Router (`router_setup.ROUTER_PROMPT`) and the
Librarian (`documentation_gate.LIBRARIAN_PROMPT`). An Audit role reads the board skill and the onboarding packet, and
those are where the rule now lives. Tenants' own onboarding packets and declared Audit prompts belong to their
Directors and are not edited. Tenants pick up the shared skill and guides on upgrade.

**Where it sits.** In the Director skill the rule comes after the rerun reasons, not inside the reuse paragraph.
SYRD-47's suite keeps the reuse permission and its rerun triggers within 1,200 characters, so that a reader who stops
early still meets the limits on reuse.

**One correction in passing:** the Director guide's "Three rules make mutation testing honest" lists four, and now
says so.

## Evidence

`tests/review_mutation_scope_test.py` pins each clause, as a pattern, in each body:
- **Main first, from git** (`cdc3a741`): no body states where mutants go, that the whole corpus is out of scope, or
  when a whole-corpus run is allowed, and neither the board skill nor the guide ties the tier to it.
- **The whole rule in each body:** the board skill, board guide, Director guide and skill, methodology, and the LLM
  operations index (which is compact, so the survivor and "hundreds of syntactic" clauses are not required there).
- **No time limit:** the block that states the rule carries no duration and no "reason before a long run".
- **The tier tie** sits in the board skill's and guide's tier paragraph.
- **The board guide's "Test evidence" section** carries the rule.
- **No ticket IDs** in either skill.
- **Installation:** every installed board-skill adapter carries the rule.
