#!/usr/bin/env python3
"""SYRD-881: a hand-off left undelivered when its ticket leaves a stage, or changes owner, is never delivered for a
later entry: one hand-off per entry.

Found building SYRD-879. `audit` is busy, so a ticket's "entered Audit" hand-off is deferred. `audit` kicks the ticket
back without ever being handed it (acting on it from the board), and the notice stays queued. The rework comes back to
`audit` (SYRD-875's return) with its own hand-off, and the old notice -- same ticket, target and stage -- is current
again: `audit` is handed the one entry twice. The same happens within a stage: the ticket moved to `audit-2` and back.

The board is tests/review_pane_rig.py's (production-built, the real server and listener, recording panes). All
tickets are synthetic. Main runs first, from a git archive, and shows the two sends; then this tree shows one, the old
notice dropped as `left_its_entry` when the ticket left. On both, a hand-off deferred for the current entry is still
delivered once the reviewer frees up, and nothing reaches a busy pane.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "f7e7731a6619c6159a3094196639217374808cce"
#: Main as SYRD-883 is cut from it: SYRD-881's retirement, and the race between a claim and its hold.
BEFORE_883 = "a5fc71b49536f481c3652cb5043b68e9b7be3a00"
from review_pane_rig import PANE_ENV_PREFIXES  # noqa: E402  (beside this file)

CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def scenario(root: Path) -> dict:
    sys.path.insert(0, str(Path(__file__).resolve().parent))  # the rig, beside this scenario on either tree
    from review_pane_rig import review_board

    seen: dict = {}
    with review_board(root, "s881") as rig:
        sql, act, where, settle, sent = rig.sql, rig.act, rig.where, rig.settle, rig.sent

        def sends(ticket: str, since: str) -> int:
            return int(sql("SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id = "
                           f"'{ticket}' AND target_role = 'audit' AND ticket_state_at_event = 'audit' "
                           f"AND kind = 'transition' AND event = 'send' AND ts >= '{since}'::timestamptz;"))

        def drops(ticket: str) -> list:
            return sql("SELECT coalesce(string_agg(target_role || ':' || coalesce(busy_reason, ''), ',' ORDER BY id), '') "
                       f"FROM ticket_board.notification_trace WHERE ticket_id = '{ticket}' AND event = 'drop';").split(",")

        def to_audit(ticket: str) -> list:
            return [t for r, t in sent if r == "audit" and t.startswith(ticket + " -- ")]

        def freed_twice(ticket: str, since: str) -> dict:
            """audit frees up and is handed what is queued for it; then its turn ends while it still holds the
            ticket, when anything else still queued for it would arrive."""
            while_busy = list(to_audit(ticket))
            rig.idle("audit")
            settle(4)
            first = sends(ticket, since)
            rig.idle("audit")
            settle(4)
            return {"while_busy": while_busy, "first": first, "after_turn": sends(ticket, since),
                    "handed": to_audit(ticket), "where": where(ticket), "drops": drops(ticket)}

        # 1. Re-entry. audit is busy; PGU-1's hand-off is deferred; audit kicks PGU-1 back without being handed it, and
        # the rework comes back to audit (the kick-back reviewer) while audit is still busy.
        rig.busy("audit")
        since = sql("SELECT clock_timestamp()::text;")
        rig.seeded("PGU-1", "app")
        rig.to_audit("PGU-1", "app")
        settle(3)
        # Away and back between two passes, as when a deferred notice's requeue delay outlasts the ticket's absence:
        # a pass while it is away would find the old notice stale and drop it.
        act("PGU-1", "audit_kick_back", "audit", reason="One finding.")
        rig.to_audit("PGU-1", "app")
        settle(3)
        seen["reentry"] = {"landed": where("PGU-1"), **freed_twice("PGU-1", since)}
        act("PGU-1", "audit_sign_off", "audit", text="Audited.")
        rig.idle("audit")

        # 2. Owner change. audit is busy; PGU-2's hand-off is deferred; the Director moves PGU-2 to busy audit-2 and back.
        rig.busy("audit")
        rig.busy("audit-2")
        since = sql("SELECT clock_timestamp()::text;")
        rig.seeded("PGU-2", "ops")
        rig.to_audit("PGU-2", "ops")
        settle(3)
        act("PGU-2", "reassign", "director", assignee="audit-2", reason="audit-2 takes it")
        act("PGU-2", "reassign", "director", assignee="audit", reason="audit after all")
        settle(3)
        seen["owner_change"] = {"landed": where("PGU-2"), **freed_twice("PGU-2", since)}
        act("PGU-2", "audit_sign_off", "audit", text="Audited.")
        rig.idle("audit")
        rig.idle("audit-2")

        # 4. SYRD-883: claimed, then the ticket left and came back before the hold. audit is free, so the listener
        # claims PGU-4's hand-off and goes to deliver it; between that claim and its hold (a seam on the hold, never a
        # sleep) audit kicks PGU-4 back and the rework returns to it: the claimed row is retired, a new one queued.
        since = sql("SELECT clock_timestamp()::text;")
        rig.seeded("PGU-4", "main")
        rig.to_audit("PGU-4", "main")
        eligibility = rig.listener.eligibility
        real_hold, moved = eligibility.hold_for_delivery, []

        def moved_before_the_hold(conn, connect, ticket_id, target_role, payload=""):
            if ticket_id == "PGU-4" and not moved:
                moved.append(act("PGU-4", "audit_kick_back", "audit", reason="One finding."))
                rig.to_audit("PGU-4", "main")
                moved.append(where("PGU-4"))
            return real_hold(conn, connect, ticket_id, target_role, payload)

        eligibility.hold_for_delivery = moved_before_the_hold
        try:
            settle(3)
        finally:
            eligibility.hold_for_delivery = real_hold
        seen["claimed"] = {"moved": [str(m)[:60] for m in moved], **freed_twice("PGU-4", since),
                           "retired": sql("SELECT coalesce(string_agg(busy_reason, ','), '') FROM "
                                          "ticket_board.notification_trace WHERE ticket_id = 'PGU-4' AND event = 'drop';")}
        act("PGU-4", "audit_sign_off", "audit", text="Audited.")
        rig.idle("audit")

        # 3. A hand-off already typed into its pane, its delivery proof open (SYRD-565: unsubmitted), is that proof's
        # to settle -- it tells the Director to clear the text left in the composer -- so a move never retires it.
        # Last, so the stand-in proof row touches nothing after it.
        rig.busy("audit")
        rig.busy("audit-2")
        rig.seeded("PGU-3", "main")
        rig.to_audit("PGU-3", "main")
        typed = sql("SELECT id FROM ticket_board.ticket_notification_queue WHERE ticket_id = 'PGU-3' "
                    "AND target_role = 'audit' AND kind = 'transition';")
        sql("INSERT INTO ticket_board.notification_proofs (notification_id, ticket_id, target_role, kind, target, state, "
            f"sent_at) VALUES ({typed}, 'PGU-3', 'audit', 'transition', 'pgu-audit:0.0', 'unsubmitted', clock_timestamp());")
        act("PGU-3", "reassign", "director", assignee="audit-2", reason="audit-2 takes it")
        seen["typed"] = {"queued": sql(f"SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE id = {typed};"),
                         "retired": [d for d in drops("PGU-3") if d == "audit:left_its_entry"]}
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    for name in (Path(__file__).name, "review_pane_rig.py"):  # this scenario and its rig, on that tree
        shutil.copy2(Path(__file__).resolve().parent / name, into / "tests" / name)
    return into


def run(root: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    child = subprocess.run([sys.executable, str(root / "tests" / Path(__file__).name), "--scenario", str(root)],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def common(got: dict, tree: str) -> None:
    for case in ("reentry", "owner_change"):
        c = got[case]
        check(c["landed"] == "audit/audit" and c["where"] == "audit/audit", f"{tree}, {case}: the ticket is audit's: {c}")
        check(c["while_busy"] == [], f"{tree}, {case}: nothing reaches audit while it is busy: {c}")
        check(c["first"] >= 1, f"{tree}, {case}: once audit frees up, the hand-off deferred for this entry is "
                               f"delivered: {c}")
    check(got["typed"] == {"queued": "1", "retired": []},
          f"{tree}: a hand-off typed into its pane, its delivery proof open, survives its ticket's move, left to that "
          f"proof: {got['typed']}")


def claimed_common(got: dict, tree: str) -> dict:
    c = got["claimed"]
    check(len(c["moved"]) == 2 and c["moved"][1] == "audit/audit" and c["where"] == "audit/audit",
          f"{tree}: between the claim and the hold, PGU-4 left audit and came back to it: {c}")
    check(c["first"] >= 1, f"{tree}: audit is handed PGU-4's entry: {c}")
    return c


def before(got: dict) -> None:
    common(got, "main before SYRD-881")
    for case in ("reentry", "owner_change"):
        c = got[case]
        check(c["after_turn"] == 2, f"main before SYRD-881, {case}: audit is handed this one entry twice -- the hand-off "
                                    f"left from before the ticket left, and the new one (SYRD-881): {c}")


def before_883(got: dict) -> None:
    common(got, "main before SYRD-883")
    for case in ("reentry", "owner_change"):
        check(got[case]["after_turn"] == 1, f"main before SYRD-883, {case}: SYRD-881 already delivers it once: {got[case]}")
    c = claimed_common(got, "main before SYRD-883")
    check(c["after_turn"] == 2, f"main before SYRD-883: the copy claimed before PGU-4 left is sent beside the new "
                                f"entry's own hand-off: two hand-offs for one entry: {c}")


def candidate(got: dict) -> None:
    common(got, "this tree")
    c = claimed_common(got, "this tree")
    check(c["first"] == 1 and c["after_turn"] == 1 and len(c["handed"]) == 1,
          f"claimed before PGU-4 left and returned, the old copy is not sent: audit is handed the entry once, also "
          f"after its turn ends while it still holds the ticket: {c}")
    check("retired_after_claim" in c["retired"].split(","),
          f"the claimed copy is dropped under the hold, traced as retired_after_claim: {c['retired']}")
    for case, left in (("reentry", "in_progress"), ("owner_change", "audit-2")):
        c = got[case]
        check(c["first"] == 1 and c["after_turn"] == 1 and len(c["handed"]) == 1,
              f"{case}: audit is handed this entry exactly once, also after its turn ends while it still holds the "
              f"ticket: {c}")
        check("audit:left_its_entry" in c["drops"],
              f"{case}: the hand-off queued before the ticket left audit (to {left}) is dropped, traced as "
              f"left_its_entry, never delivered: {c['drops']}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]))))
        return 0
    if os.environ.get("SYRD881_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        for commit, verdict in ((BEFORE, before), (BEFORE_883, before_883)):
            with tempfile.TemporaryDirectory(prefix="syrd881-before.", dir="/tmp") as tmp:
                verdict(run(tree_at(commit, Path(tmp) / "before")))
    candidate(run(ROOT))
    print(f"handoff_entry_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
