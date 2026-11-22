# SYRD-848: the Publisher publishes the board-recorded implementer + documentation pair

> **Historical design and rollout evidence.** It records the change, not the
> current operating contract. Use the [board guide](onboarding/switchyard-board-guide.md)
> and [board service: publishing](ticket-board-service.md) before acting.

## The contradiction

SYRD-624 and SYRD-625 contradicted each other:
- **SYRD-624:** a Librarian's documentation sign-off makes the candidate a
  documentation commit whose sole parent is the implementer's candidate. That
  is two commits on the public tip.
- **SYRD-625:** the Publisher publishes exactly one commit, so it returned every
  such candidate for lineage, clearing every sign-off.

It happened on SYRD-836 twice and on SYRD-842. Each time it cost a squash, a
resubmission and a whole review round, for documentation Audit had already
reviewed.

## The shape chosen

**The Publisher accepts exactly the pair the board recorded,** and pushes it
whole. I chose this over the alternative, a board-made squash at documentation
sign-off, because a squash makes one commit of two people's work: either the
implementer's change goes under the Librarian's name, or the Librarian's goes
under the implementer's. Pushing the recorded pair keeps each commit, with its
author, exactly as reviewed.

- **The layer** (pgu1053, `ticket_documentation_layers`). Audit's
  documentation-only return (SYRD-719) keeps the audited candidate, so a
  Librarian's correction after it is a second documentation commit on the
  first. `ticket_audit_returns` keeps only the latest decision, so the board now
  also keeps every documentation commit, in order, with the candidate it was
  layered on. A trigger runs after `record_audit_return` and follows its record:
  - a documentation commit on the record's base starts the layer again;
  - one on the layer's top extends it;
  - no change leaves it;
  - anything that ends the record's documentation layer (a producer recorded
    again, a revision made any other way) clears it.
- **The read** (`publication_documentation_pair`, for the listener alone). It
  returns the implementer commit and the layer only when all of these hold:
  - the latest decision is the holder's own: a documentation commit, or no
    change. A no change after a documentation return keeps the earlier
    documentation commit as the candidate, on the implementer's, and that is
    still the recorded pair;
  - its commit is the ticket's candidate;
  - its base is the producer's commit, and differs from it;
  - the recorded layer runs unbroken from that commit to the candidate.

  Otherwise it returns NULL.
- **Lineage** (`publisher.Attempt.require_one_commit_on`).
  - **Accepted:** the candidate is the layer's top, each commit's sole parent
    is the one under it in the record, and the implementer commit's sole
    parent is the public tip.
  - **Refused:**
    - a recorded pair whose implementer commit is not on the tip is returned,
      saying so;
    - any other shape of several commits, including a record that is not the
      candidate's history link by link, is returned exactly as before.
- **The push** is the same bundle (head the candidate, prerequisite the tip) and
  the same compare-and-swap push leased on the tip. The push repository proves
  every commit's parent from the bundle again before pushing, and escalates to the
  Director, pushing nothing, if they differ.
- **The evidence** names every commit, its author, and the role the board
  recorded. The attempt's journal keeps the chain.
- **Unchanged:** a candidate the Librarian approved with no change is one commit,
  and is published as before. So are review evidence and sign-offs: nothing is
  cleared, since Audit reviewed the documentation commit's tree.

Audit's first review found the two documentation-return cases, which the first
candidate returned. A board-made reset of the candidate to the implementer's
commit on a documentation return was the other way to keep a single pair. It
would have changed SYRD-719's "keeping the candidate it audited", and lost the
first documentation commit on a no change. So the layer is recorded instead.

Boards upgraded with a ticket already past a documentation sign-off have no
layer for it. Its candidate is returned once, as before, and records a layer
from its next documentation commit.

## Tests

`tests/publisher_documentation_pair_test.py` runs on publisher_board_test's
real world: a bare forge, the trusted cache, and a production-built board with
the documentation gate (a Librarian) and the Publisher. It covers:
- the recorded pair published, in order, each with its author, with the
  evidence naming both;
- after Audit's documentation-only return: a correction publishes all three
  commits, and a no change publishes the pair;
- the implementer's rework clears the layer, and the next documentation
  commit starts it again;
- a misrecorded layer parent, implementer commit, producer or top layer is
  returned, and the board reads no pair for it;
- an unrecorded two-commit candidate returned, including one shaped like a
  documentation pair;
- a stale pair returned;
- the push repository's re-check;
- no change published as one commit.

The same suite fails on main's tree at the recorded pair, which was returned
for lineage.
