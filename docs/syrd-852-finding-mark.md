# SYRD-852: a finding's acceptance mark goes when it stops being a finding in publication

> **Historical design and rollout evidence.** It records the change, not the
> current operating contract. Use the [Director guide](onboarding/switchyard-director-guide.md)
> before acting.

## The gap (Audit, on SYRD-849)

SYRD-849's `publish_finding` marks the round's finding as accepted for
publication. For a marked research ticket the Publisher takes the finding's
moves: on to done, with no Audit code sign-off asked. The mark was cleared
only when the ticket entered Research again.

Audit's probe:
1. A finding is accepted into publication.
2. It is deferred.
3. needs_research is turned off.
4. It is routed to an implementer, and a code commit goes through Audit into
   publication.

The code ticket still carried the mark. Only `publication_is_finding`'s
needs_research condition kept the Publisher from sending it to done without
Final Sign-Off and without its gates. A mutant that dropped that condition
survived every suite.

## The change (pgu1055)

`note_research_round` clears the mark in three cases:
- **needs_research goes off**, on any update, in any stage.
- **The ticket leaves the publication stages** (`publication`,
  `publication_failed`) by anything but `finding_published`: a defer, a cancel,
  a route, a force-move. The finding's own returns enter Research, which
  clears the round's finding as before.
- **The ticket enters `publication`** by anything but `publish_finding` or
  `publication_retry`. For example, Audit's approval of a code candidate, or a
  force-move.

An escalation (`publication` → `publication_failed`) and the Director's retry
keep the mark, so a retried finding is still published as one. The
needs_research condition in `publication_is_finding` stays as a second guard.

**Also:** `switchyard research-stage --help` now names publish-finding.
`switchyard publisher --help` now says a finding the Director accepts goes on
to Done, and a finding goes back to its researcher.

## Tests

`tests/research_publication_test.py`, `mark_lifecycle`, on the real forge,
cache and Publisher, with a board built from main's tree and migrated:
- **Audit's probe.** The deferred finding loses its mark. Made an ordinary
  ticket and built as code, it enters publication by Audit's approval with no
  mark, including one written there beforehand under the triggers. With a mark
  written again in publication, it is still published on to Final Sign-Off,
  because it is not a research ticket.
- **needs_research turned off** while the finding waits in publication takes
  the mark.
- **An escalation and the Director's retry** keep it, and the finding is
  published and done.

**Results:**
- The suite fails on main's tree at the deferred finding's mark.
- Each of these mutants is killed by a check:
  - Audit's dropped needs_research condition (killed at "published on to Final
    Sign-Off, not to Done");
  - each of the three clears removed;
  - a retry that clears;
  - an escalation that clears.
