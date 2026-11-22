# SYRD-851: plan text renders as text, pinned; the read-query boundary golden re-pinned

Two test items from Audit's sign-off of SYRD-850. No product code changes.

## 1. Markup in plan text is shown as text

Plan strings come from two places and appear in three:
- **Sources:** the plan index (milestone and deliverable titles) and the board (an unplanned reason).
- **Where they show:** the ticket view, the Deliverable filter and the Plan dialog.

SYRD-850 renders them through `textContent`, `new Option` and `optgroup.label`, and Audit's manual probe confirmed that
markup in them is shown literally. Nothing pinned it.

`tests/plan_board_view_browser_test.py` now re-plans its plan-gated board through a second plan ticket. The new plan's
M2 and D2.1 titles carry `<img src="x" class="plan-851" onerror="…">`, whose handler counts itself, and a new unplanned
ticket's reason carries the same markup. At desktop and phone size the test checks:
- the filter's D2.1 option and M2 group label;
- the ticket view of PGU-921 (the deliverable and milestone titles) and of the unplanned ticket (the reason);
- the plan view's M2 heading and D2.1 button.

Each must show the string exactly, with no element made from it at any step, and the handler must never run. The
earlier assertions keep the plain fixture's text, so they read as before. PGU-930 now goes to ops rather than main,
because a ticket held in main's serial slot would queue the re-plan behind it.

## 2. `ticket_board_read_query_boundary_test` re-pinned

**Cause:** the SQL hash has been stale since SYRD-595 (e986c496). That change, and those of SYRD-599, 626, 629, 662,
673, 697 and 850, each altered the read query on purpose without re-measuring the pin.

The test was last green at SYRD-113 slice B (52fb1c55), measured by running the suite at each commit that touched the
query or the test. Each commit's change to `ticket_read_query.py` is that ticket's own feature:
- SYRD-595: retired-runtime sends;
- SYRD-599: `routing`;
- SYRD-626: `context_continuation`;
- SYRD-629: `uat`;
- SYRD-662: `user_wait`;
- SYRD-673: `audit_return`;
- SYRD-697: the owner-write confirmation;
- SYRD-850: `plan_scope`.

The hashes were measured again from main (e62f8efd). The test keeps everything it protected: the exact bytes for a
legacy and a configured workflow, read-only statements, parameters, and the connection reuse. It gains a third golden
for a plan-gated workflow, and checks that only that query reads `ticket_deliverables`.

## Evidence

- **Mutants: 8 of 8 killed by an assertion.**
  - innerHTML in the ticket-view line, the filter option, the plan view's deliverable button, its milestone heading,
    and every plan element;
  - one byte of the query changed;
  - `plan_scope` read without the gate, and never read.
- **Sweep** of both suites, main against this tree: the read-query suite is red on main and green here, and the
  browser suite is green on both.
