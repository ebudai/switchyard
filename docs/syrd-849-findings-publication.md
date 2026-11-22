# SYRD-849: research findings reach main through the Publisher

> **Historical design and rollout evidence.** It records the change, not the
> current operating contract. Use the [board guide](onboarding/switchyard-board-guide.md),
> the [Director guide](onboarding/switchyard-director-guide.md) and the
> [Researcher guide](onboarding/switchyard-researcher-guide.md) before acting.

## The gap

SYRD-842's `close_on_finding` moved a finding from triage to done. A findings
commit therefore lived only on its researcher's `roles/` ref, and the research
directory on main stayed empty. The Researcher guide calls a findings file
worth keeping, and the User asked for findings to be durable project
documents. The Director decided, at SYRD-842's Final Sign-Off, that findings
land on main.

## The shape chosen

On a board that runs the Publisher, a finding with a commit goes through
publication before Done, as any approved candidate does. The Publisher's own
stages are reused, with moves for findings:

- **The Director's `publish_finding`:** triage into `publication`, with the
  reason. It is the acceptance, in place of `close_on_finding`, which the board
  refuses for a finding with a commit there.
- **The Publisher's `finding_published`:** `publication` on to the terminal
  stage (`done`). There is no Final Sign-Off, because the Director's acceptance
  was the decision.
- **The Publisher's `finding_publication_return`:** back to Research and the
  researcher, for example when main moved since the finding was built. The
  researcher rebases, resubmits, and the finding is reviewed again.
- **The Director's `finding_publication_kick_back`:** `publication_failed` back
  to Research. The ordinary `publication_kick_back`, which goes to
  Implementation, is refused for a finding. `publication_retry` works as for
  any ticket.

The two returns are `move`s, not `return`s: a return goes back into a review
only from the stage that submitted into it.

**The database** (pgu1054):
- `publish_finding` marks the round's finding as accepted for publication, and
  `publication_is_finding` reads that mark. A new round in Research starts
  with no finding, so with no mark.
- For a finding with the mark, `publisher_finish` takes the finding's moves.
- `publication_missing_signoffs` asks no Audit code sign-off of a finding: its
  review was `research_review`, which holds no sign-off flag (SYRD-842's
  choice).
- `publish_finding` is refused for a no-commit finding, for a ticket that is
  not a research ticket, and for a commit that is not the finding submitted.
- The finding's kick-back is refused for a ticket never accepted as one.
- The Publisher's window lists its two finding moves.

Everything else the Publisher does is unchanged. It publishes exactly the
findings commit as one fast-forward on the public tip, from a verified bundle,
under a compare-and-swap lease. research_candidate already held the commit to
`<research directory>/<TICKET>.md` at submission.

**Composition:** the finding moves exist only with both composers on. Either
may be turned on first: with_publisher and without_publisher call
`research_stage.recompose_research` before they validate, and
validate_publisher admits the Publisher's two finding moves.
validate_research_stage holds them exactly.

**Kept as it was:**
- A no-commit finding closes with `close_on_finding`.
- `return_finding` is unchanged.
- On a board without the Publisher, `close_on_finding` closes every finding as
  before, and integrating a findings commit stays the Director's.
- Findings take no documentation review. The documentation gate does not apply
  to research tickets, so SYRD-848's documentation pair never meets a finding.

## Test gaps closed

- **The round-scoped timebox notice** (`research_timebox_notice_is_current`).
  research_stage_test now queues a round's notice and kicks the finding back,
  so the ticket is in Research again for a new round, then runs the listener's
  passes. The old notice is dropped as stale. With the round comparison
  removed, it is delivered.
- **The database's timebox range.** `research_finding.perform` passes an int
  timebox straight through, so research_stage_test now briefs 899 and 2592001
  seconds as ints through the real operation. The database refuses both, and
  the ticket is unchanged. With the range widened to 60, 899 is accepted.

## Tests

`tests/research_publication_test.py` runs on publisher_board_test's world: a
bare forge, the trusted cache, and the real Publisher. The board is built from
main's tree and upgraded with this tree's migrations, with the research stage,
the documentation gate and the Publisher. It covers:
- composition in both orders;
- a finding published to main and Done;
- a stale finding returned to the researcher, rebased and published;
- a failed publication, where the ordinary kick-back is refused and the
  finding's is taken;
- every refusal;
- an ordinary candidate still going on to Final Sign-Off.
