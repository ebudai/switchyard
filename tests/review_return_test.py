#!/usr/bin/env python3
"""SYRD-875: a resubmitted review returns to the reviewer who kicked it back, not the first free owner.

SAPI-148 (release 623c34e5): audit2 reviewed round 1 (the balancer had moved it there) and kicked it back; round 2
landed with audit, the stage's first owner, and audit signed it off ("audit2 reviewed round 1. I take round 2.").

The board is tests/review_pane_rig.py's: production-built, Audit owned by audit (Claude) and audit-2 (ephemeral Codex)
under a pull policy, the real server and listener, recording panes. All tickets are synthetic.

Main runs first, from a git archive: round 2 of a review audit-2 kicked back lands with audit. Then this tree:
- **Return:** round 2 lands with audit-2 at once. Its saved conversation of round 1 cannot be restored (Codex), so it
  is not parked: audit-2 is cleared, at an idle boundary, and handed a fresh review of the exact candidate, once, and
  the ticket records why. Handed over, it is never given up, even when audit-2's process then goes.
- **Busy keeps it:** a rework back with busy audit-2 queues behind its current review; idle, empty audit cannot take it,
  even though audit-2 was never handed the ticket (it kicked back a reassignment before its notice arrived, so SYRD-745's
  re-review rule, which reads the hand-offs, does not hold it). audit-2 gets it when it frees up, once.
- **The Director wins:** moved by the Director to busy audit, the rework stays there while audit-2 is idle and empty;
  placed back with audit-2, it stays there even when audit-2's process goes.
- **Fallback:** a rework returned to an audit-2 whose provider process is gone goes to audit, the first owner, which is
  handed it; the ticket says why in one line.
- **The record:** a kick-back out of Audit records its reviewer, a sign-off ends the record, and single-owner stages
  (Inspection) record nothing; an owner named on entry is kept; the board's own reasons a reviewer cannot take a rework
  back.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "e7215ca81d11c0e650f903cac2f4ecf9c38ca9de"
from review_pane_rig import PANE_ENV_PREFIXES  # noqa: E402  (beside this file)

CANDIDATE = "c875" + "0" * 36
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
    with review_board(root, "s875") as rig:
        sql, act, where, settle, handed, to_audit, seeded = (rig.sql, rig.act, rig.where, rig.settle, rig.handed,
                                                             rig.to_audit, rig.seeded)
        idle, busy, sent, session_context = rig.idle, rig.busy, rig.sent, rig.session_context

        def arrive(ticket: str) -> None:
            """A review arriving in Audit with no author slot involved: from Inspection, to audit, the first owner."""
            seeded(ticket, "inspector", state="inspection")
            act(ticket, "inspector_sign_off", "inspector")

        def returns(ticket: str) -> list:
            if sql("SELECT to_regclass('ticket_board.review_returns') IS NOT NULL;") != "t":
                return ["(no review_returns on this board)"]
            return json.loads(sql("SELECT coalesce(json_agg(json_build_object('stage', stage, 'role', role, 'action', "
                                  f"action)), '[]')::text FROM ticket_board.review_returns WHERE ticket_id = '{ticket}';"))

        def sign_off(ticket: str) -> None:
            """Audit's sign-off by whoever holds the ticket, so a ticket somewhere unexpected is observed, not a crash."""
            holder = where(ticket)
            if holder.startswith("audit/"):
                act(ticket, "audit_sign_off", holder.split("/", 1)[1], text="Audited.")

        def to_audit_2(ticket: str) -> list:
            return [t for r, t in sent if r == "audit-2" and (t.startswith(ticket + " -- ") or t.startswith("/"))]

        def kicked_back_unhanded(ticket: str, author: str) -> None:
            """audit-2 kicks a review back that it was never handed: the Director's reassignment reaches busy audit-2
            as a queued notice, and audit-2 acts on the ticket from the board before that notice is delivered."""
            busy("audit-2")
            to_audit(ticket, author)
            act(ticket, "reassign", "director", assignee="audit-2", reason="audit-2 reviews it")
            act(ticket, "audit_kick_back", "audit-2", reason="One finding.")

        # History, SAPI-148's shape: audit is busy with PGU-11; PGU-1's round 1 lands with audit and the balancer moves
        # it to idle audit-2, which reviews it and kicks it back.
        arrive("PGU-11")
        settle(3)
        seeded("PGU-1", "app")
        to_audit("PGU-1", "app")
        settle(4)
        seen["round_1"] = {"where": where("PGU-1"), "balanced": rig.comments("PGU-1", "Board balance in audit"),
                           "bound": sql("SELECT coalesce((SELECT state FROM ticket_board.ticket_role_contexts "
                                        "WHERE ticket_id='PGU-1' AND role='audit-2'), '');"),
                           "audit_current": where("PGU-11")}
        act("PGU-1", "audit_kick_back", "audit-2", reason="Six findings.")
        seen["round_1"].update(kicked_back=where("PGU-1"), returns=returns("PGU-1"))
        idle("audit-2")
        # audit-2 then reviews PGU-12, which clears its pane for it: PGU-1's conversation is no longer the pane's.
        arrive("PGU-12")
        settle(4)
        seen["round_1"]["moved_on"] = where("PGU-12")
        sign_off("PGU-12")
        idle("audit-2")

        # 1. Round 2 of PGU-1, its exact candidate recorded, while audit is busy and audit-2 idle and empty.
        act("PGU-1", "add_comment", "app", text="Reworked all six findings.")
        to_audit("PGU-1", "app")
        sql(f"SET session_replication_role = replica; UPDATE ticket_board.tickets SET commit_hash = '{CANDIDATE}' "
            "WHERE id = 'PGU-1';")
        landed = where("PGU-1")
        mark = len(sent)
        settle(6)
        time.sleep(session_context.CONFIRM_SECONDS + 0.3)
        settle(3)
        seen["round_2"] = {
            "landed": landed, "where": where("PGU-1"), "to_audit_2": [t for r, t in sent[mark:] if r == "audit-2"],
            "to_audit": handed("audit", "PGU-1", mark),
            "director": [t for r, t in sent[mark:] if r == "director" and re.search(r"\bPGU-1\b", t)],
            "parked": sql("SELECT ticket_board.ticket_context_parked('PGU-1', 'audit-2');") == "t"}
        if landed != "audit/audit-2":  # main: round 2 went to the first owner (SAPI-148); nothing below applies there
            return seen
        settle(3)
        seen["round_2"].update(
            later=[t for r, t in sent[mark:] if r == "audit-2"],
            fresh=rig.comments("PGU-1", "Fresh review in audit"),
            archive=json.loads(sql("SELECT coalesce(json_agg(json_build_object('role', role, 'by', requested_by, "
                                   "'candidate', candidate, 'restores', jsonb_array_length(set_aside->'restores'))), "
                                   "'[]')::text FROM ticket_board.fresh_review_archive WHERE ticket_id = 'PGU-1';")),
            bound=sql("SELECT coalesce((SELECT state FROM ticket_board.ticket_role_contexts "
                      "WHERE ticket_id='PGU-1' AND role='audit-2'), '');"))
        # Handed over in this round, it is never given up: audit-2's process goes while it reviews PGU-1.
        rig.stop("audit-2")
        settle(3)
        seen["round_2"].update(handed_then_gone=where("PGU-1"), gone_comments=rig.comments("PGU-1", "Review return"))
        rig.start("audit-2")
        # A fresh review is once a round. audit-2's pane moves to another session, and the Director moves PGU-1 away and
        # back: that hand-off cannot be restored either, and it parks with the Director rather than starting fresh again.
        rig.session_start("audit-2", "audit-2-elsewhere", "startup")
        if where("PGU-1") == "audit/audit-2":  # elsewhere, the check below says where it is instead
            act("PGU-1", "reassign", "director", assignee="audit", reason="audit takes it")
            act("PGU-1", "reassign", "director", assignee="audit-2", reason="audit-2 keeps it after all")
        mark = len(sent)
        idle("audit-2")
        settle(4)
        time.sleep(session_context.CONFIRM_SECONDS + 0.3)
        settle(3)
        seen["round_2"]["again"] = {
            "where": where("PGU-1"), "to_audit_2": [t for r, t in sent[mark:] if r == "audit-2"],
            "parked": sql("SELECT ticket_board.ticket_context_parked('PGU-1', 'audit-2');") == "t",
            "director": [t for r, t in sent[mark:] if r == "director" and "PGU-1's review by audit-2 is parked" in t],
            "archived": sql("SELECT count(*) FROM ticket_board.fresh_review_archive WHERE ticket_id = 'PGU-1';")}
        sign_off("PGU-1")
        seen["round_2"]["signed_off"] = returns("PGU-1")
        idle("audit-2")

        # 2. A busy kick-back reviewer keeps its rework. audit-2 kicks PGU-4 back without ever being handed it, then
        # takes PGU-13 as its current review; audit signs PGU-11 off and is idle and empty when round 2 lands.
        seeded("PGU-4", "ops")
        kicked_back_unhanded("PGU-4", "ops")
        seen["busy"] = {"returns": returns("PGU-4"), "sends": sql(
            "SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id = 'PGU-4' AND target_role = 'audit-2' "
            "AND event = 'send';")}
        arrive("PGU-13")
        act("PGU-13", "reassign", "director", assignee="audit-2", reason="audit-2 reviews it")
        idle("audit-2")
        settle(4)
        seen["busy"]["current"] = handed("audit-2", "PGU-13")
        sign_off("PGU-11")
        idle("audit")
        mark = len(sent)
        to_audit("PGU-4", "ops")
        settle(6)
        seen["busy"].update(where=where("PGU-4"), to_audit=handed("audit", "PGU-4", mark),
                            while_busy=[t for r, t in sent[mark:] if r == "audit-2"],
                            comments=rig.comments("PGU-4", "Review return"))
        sign_off("PGU-13")
        idle("audit-2")
        settle(4)
        seen["busy"]["freed"] = [t for r, t in sent[mark:] if r == "audit-2"]
        sign_off("PGU-4")
        idle("audit-2")

        # 3. The Director wins. PGU-5's rework lands with busy audit-2; the Director moves it to audit, busy with PGU-14.
        seeded("PGU-5", "main")
        kicked_back_unhanded("PGU-5", "main")
        arrive("PGU-14")
        settle(3)
        to_audit("PGU-5", "main")
        landed = where("PGU-5")
        act("PGU-5", "reassign", "director", assignee="audit", reason="audit takes it after its current review")
        mark = len(sent)
        idle("audit-2")
        settle(4)
        seen["placed"] = {"landed": landed, "where": where("PGU-5"), "to_audit_2": handed("audit-2", "PGU-5", mark),
                          "audit_current": where("PGU-14")}
        # Placed back with audit-2 by the Director, it stays there when audit-2's process goes.
        busy("audit-2")
        act("PGU-5", "reassign", "director", assignee="audit-2", reason="audit-2 kicked it back; it reviews round 2")
        rig.stop("audit-2")
        settle(3)
        seen["placed"].update(back=where("PGU-5"), comments=rig.comments("PGU-5", "Review return"))
        rig.start("audit-2")
        idle("audit-2")
        settle(4)
        sign_off("PGU-5")
        sign_off("PGU-14")
        idle("audit-2")
        idle("audit")

        # 4. Fallback: PGU-6's rework, returned to audit-2, whose provider process is gone; audit is idle and empty.
        # Round 1 reaches audit-2 by the Director's reassignment, delivered, so nothing is left queued for audit.
        seeded("PGU-6", "app")
        busy("audit")
        to_audit("PGU-6", "app")
        act("PGU-6", "reassign", "director", assignee="audit-2", reason="audit-2 reviews it")
        idle("audit-2")
        settle(4)
        act("PGU-6", "audit_kick_back", "audit-2", reason="One finding.")
        idle("audit-2")
        idle("audit")
        rig.stop("audit-2")
        mark = len(sent)
        to_audit("PGU-6", "app")
        landed = where("PGU-6")
        settle(4)
        seen["fallback"] = {"landed": landed, "where": where("PGU-6"), "to_audit": handed("audit", "PGU-6", mark),
                            "to_audit_2": [t for r, t in sent[mark:] if r == "audit-2"],
                            "comments": rig.comments("PGU-6", "Review return"), "returns": returns("PGU-6")}
        rig.start("audit-2")
        sign_off("PGU-6")
        idle("audit")

        # 5. The record. Inspection, owned by one role, records no kick-back; an owner named on entry is kept.
        seeded("PGU-15", "inspector", state="inspection")
        act("PGU-15", "inspector_kick_back", "inspector", reason="Inspection finding.")
        seen["record"] = {"inspection": returns("PGU-15"), "kicked": where("PGU-15")}
        seeded("PGU-8", "ops")
        kicked_back_unhanded("PGU-8", "ops")
        arrive("PGU-16")  # audit's current review, so PGU-8 waits behind it
        settle(3)
        named = "{\"assignee\": \"audit\"}"
        sql("SET ticket_board.caller_role = 'director'; UPDATE ticket_board.tickets SET commit_exempt = true "
            "WHERE id = 'PGU-8';")
        sql(f"SET ROLE {rig.fixture.SERVICE_ROLE}; SET ticket_board.caller_role = 'ops'; "
            f"SELECT ticket_board.perform_workflow_action('PGU-8', 'submit_to_inspection', '{named}');")
        if where("PGU-8").startswith("inspection/"):
            sql(f"SET ROLE {rig.fixture.SERVICE_ROLE}; SET ticket_board.caller_role = 'inspector'; "
                f"SELECT ticket_board.perform_workflow_action('PGU-8', 'inspector_sign_off', '{named}');")
        seen["record"].update(named=where("PGU-8"), named_comments=rig.comments("PGU-8", "Review return"))
        idle("audit-2")
        settle(4)
        seen["record"]["named_then"] = where("PGU-8")

        def unavailable(role: str, setup: str = "") -> str:
            out = sql(f"BEGIN; {setup} SELECT 'why=' || coalesce(ticket_board.review_return_unavailable('audit', "
                      f"'{role}', clock_timestamp()), '(none)'); ROLLBACK;")
            return next(line[4:] for line in out.splitlines() if line.startswith("why="))

        seen["unavailable"] = {
            "available": unavailable("audit-2"), "not_owner": unavailable("main"),
            "not_active": unavailable("audit-2", "UPDATE ticket_board.workflow_configuration SET document = jsonb_set("
                                                 "document, '{roles}', (SELECT jsonb_agg(CASE WHEN r->>'name' = 'audit-2' "
                                                 "THEN r || '{\"active\": false}' ELSE r END) FROM jsonb_array_elements("
                                                 "document->'roles') r));"),
            "not_registered": unavailable("audit-2", "DELETE FROM ticket_board.role_runtime_assignments "
                                                     "WHERE role = 'audit-2';"),
            "paused": unavailable("audit-2", "INSERT INTO ticket_board.work_pauses (mode, began_by, reason) "
                                             "VALUES ('drain', 'director', 'test');"),
            "account_held": unavailable("audit-2", "INSERT INTO ticket_board.provider_account_holds (runtime, kind, "
                                                   "began_by) VALUES ('codex', 'suspected', 'director');")}
        rig.hold("audit-2")
        seen["unavailable"]["held"] = unavailable("audit-2")
        rig.release("audit-2")
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


def history(got: dict, tree: str) -> None:
    first = got["round_1"]
    check(first["where"] == "audit/audit-2" and len(first["balanced"]) == 1 and first["bound"] == "bound"
          and first["audit_current"] == "audit/audit",
          f"{tree}: round 1 of PGU-1 lands with audit, busy with PGU-11, and the balancer moves it to idle audit-2, "
          f"which is handed it: {first}")
    check(first["kicked_back"] == "in_progress/app" and first["moved_on"] == "audit/audit-2",
          f"{tree}: audit-2's kick-back returns it to its implementer, and audit-2 then reviews PGU-12: {first}")


def before(got: dict) -> None:
    history(got, "main")
    second = got["round_2"]
    check(second["landed"] == "audit/audit" and second["where"] == "audit/audit" and second["to_audit_2"] == [],
          f"main: round 2, which audit-2 kicked back, lands with audit, the first owner, and audit-2 is sent nothing "
          f"(SAPI-148): {second}")


def candidate(got: dict) -> None:
    history(got, "this tree")
    check(got["round_1"]["returns"] == [{"stage": "audit", "role": "audit-2", "action": "audit_kick_back"}],
          f"the kick-back records audit-2 as the reviewer PGU-1's rework returns to: {got['round_1']}")
    second = got["round_2"]
    check(second["landed"] == "audit/audit-2", f"round 2 lands with audit-2, which kicked round 1 back: {second}")
    after = second["to_audit_2"]
    check(after[:1] == ["/clear"] and len([t for t in after if t.startswith("PGU-1 -- ")]) == 1
          and not any(t.startswith("/resume") for t in after),
          f"audit-2 cannot restore its saved conversation of PGU-1 (Codex), so it is cleared at an idle boundary and "
          f"handed the review afresh, once, never resumed: {after}")
    handoff = next((t for t in after if t.startswith("PGU-1 -- ")), "")
    check("fresh review of the exact candidate " + CANDIDATE in handoff, f"the hand-off names the exact candidate: {handoff}")
    check(not second["parked"] and second["director"] == [] and second["to_audit"] == [],
          f"nothing parks, the Director is told nothing, and audit is handed nothing: {second}")
    check(second["fresh"] == [
        "Fresh review in audit: audit-2 kicked this back and takes the rework, but its saved conversation of PGU-1 "
        "cannot be restored (codex has no verified in-session resume). It is set aside (kept in the board's record), "
        f"and audit-2 reviews PGU-1 afresh: the exact candidate {CANDIDATE}, with its earlier findings in these comments."],
        f"the ticket records that the review started fresh, and why: {second['fresh']}")
    check(second["archive"] == [{"role": "audit-2", "by": "board", "candidate": CANDIDATE, "restores": 1}]
          and second["bound"] == "bound",
          f"audit-2's old records of PGU-1 are kept, and the fresh review is bound to a new conversation: {second}")
    check(second["later"] == after, f"and nothing more is sent: delivered once: {second['later']}")
    again = second["again"]
    check(again["where"] == "audit/audit-2" and again["parked"] and again["to_audit_2"] == []
          and len(again["director"]) == 1 and again["archived"] == "1",
          f"a fresh review is once a round: moved away and back by the Director, with its conversation displaced, PGU-1 "
          f"parks with one Director notice, and nothing starts fresh again: {again}")
    check(second["handed_then_gone"] == "audit/audit-2" and second["gone_comments"] == [],
          f"handed over in this round, the rework is never given up, even when audit-2's process goes: {second}")
    check(second["signed_off"] == [], f"a sign-off out of Audit ends the record: {second['signed_off']}")
    busy = got["busy"]
    check(busy["returns"] == [{"stage": "audit", "role": "audit-2", "action": "audit_kick_back"}] and busy["sends"] == "0"
          and len(busy["current"]) == 1,
          f"audit-2 kicked PGU-4 back without being handed it, and is busy with PGU-13: {busy}")
    check(busy["where"] == "audit/audit-2" and busy["to_audit"] == [] and busy["comments"] == [],
          f"round 2 queues behind audit-2's current review: idle, empty audit is not given it: {busy}")
    check(busy["while_busy"] == [], f"busy audit-2 is not interrupted: {busy['while_busy']}")
    check(busy["freed"][:1] == ["/clear"] and len([t for t in busy["freed"] if t.startswith("PGU-4 -- ")]) == 1,
          f"freed, audit-2 is cleared and handed PGU-4, once (a first hand-off: it never held it): {busy['freed']}")
    placed = got["placed"]
    check(placed["landed"] == "audit/audit-2" and placed["audit_current"] == "audit/audit",
          f"PGU-5's rework lands with audit-2 while audit reviews PGU-14: {placed}")
    check(placed["where"] == "audit/audit" and placed["to_audit_2"] == [],
          f"moved by the Director to busy audit, it stays there while audit-2 is idle and empty: {placed}")
    check(placed["back"] == "audit/audit-2" and placed["comments"] == [],
          f"placed back with audit-2 by the Director, it stays there when audit-2's process goes: {placed}")
    fallback = got["fallback"]
    check(fallback["landed"] == "audit/audit-2", f"PGU-6's rework is returned to audit-2: {fallback}")
    check(fallback["where"] == "audit/audit" and fallback["to_audit_2"] == []
          and fallback["to_audit"] == ["PGU-6 -- PGU-6 is now assigned to you"],
          f"audit-2's process is gone, so it goes to audit, the first owner, which is handed it once: {fallback}")
    check(fallback["comments"] == [
        "Review return in audit: audit-2 kicked this back, but cannot take the rework (registered provider process is "
        "gone), so it lands with audit, the first owner, and may be balanced."] and fallback["returns"] == [],
        f"the ticket says why in one line, and the return is given up: {fallback}")
    record = got["record"]
    check(record["inspection"] == [] and record["kicked"].startswith("in_progress/"),
          f"Inspection, owned by one role, records no kick-back: {record}")
    check(record["named"] == "audit/audit" and record["named_comments"] == [],
          f"an owner named on entry is kept, though audit-2 kicked PGU-8 back: {record}")
    check(record["named_then"] == "audit/audit-2",
          f"and, not with the reviewer that kicked it back, it waits behind audit's PGU-16 like any review, and is "
          f"balanced to idle audit-2: {record}")
    check(got["unavailable"] == {"available": "(none)", "not_owner": "it no longer owns audit",
                                 "not_active": "it is not an active role", "not_registered": "it is not registered",
                                 "paused": "(none)", "account_held": "its provider account is held",
                                 "held": "its provider is out of quota"},
          f"the board's own reasons a reviewer cannot take a rework back; a work pause, which holds every reviewer "
          f"alike, is not one: {got['unavailable']}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]))))
        return 0
    if os.environ.get("SYRD875_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        with tempfile.TemporaryDirectory(prefix="syrd875-before.", dir="/tmp") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(run(ROOT))
    print(f"review_return_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
