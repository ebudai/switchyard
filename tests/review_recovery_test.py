#!/usr/bin/env python3
"""SYRD-873: the review balancer keeps the Director's placement and never hands a review to a reviewer whose hand-off
would park; a parked review is the Director's, with a fresh review as one supported recovery.

MEFP-1234 (2026-11-24): Audit is owned by audit and audit-2, an ephemeral Codex reviewer, under a pull policy. audit-2
kicked a candidate back, and its pane was then cleared for another ticket. The resubmission waited behind audit's
current review, and the balancer moved it to idle audit-2. Its hand-off needed audit-2's saved conversation restored,
Codex has no verified in-session resume, so the delivery parked it. The Director reassigned it to audit; 23 seconds
later the balancer moved it back, because audit was busy and audit-2 live and idle.

A production-built board runs here (schema.sql, rbac.sql) with the shipped example workflow under a pull policy, and
audit-2 added to Audit as MEFP has it: ephemeral, on Codex. The real HTTP server and notify listener run, with the
real gate reading hook state the real pane hook's writer records. The panes are recording providers: a `/clear`
starts a new session announced with SessionStart(clear); a reviewer handed a ticket stays busy with it until the test
ends its turn. All tickets are synthetic. The board and panes are tests/review_pane_rig.py's, shared with
review_return_test.

SYRD-875 returns a rework to the reviewer that kicked it back. The two resubmissions here whose landing this test reads
(PGU-1's and PGU-6's) are made while that reviewer's provider is held, so the return is given up and they land with
audit, as on main.

Main runs first, from a git archive: the resubmission goes to audit-2 and parks, and the Director's reassignment of
another review to busy audit is undone. Then this tree:
- **Not eligible:** the resubmission stays with audit; audit-2 is sent nothing about it.
- **Placement kept:** a review the Director moves to busy audit stays there while audit-2 is idle and empty, and is
  delivered to audit, once, when audit frees up.
- **Parked is the Director's:** an older listener's balancer call (which skips nothing) moving the resubmission to
  audit-2 parks it, with one notice naming the ticket, the reviewer, the reason, the exact candidate and both
  recoveries; it is counted as the Director's next action, and the balancer never moves it to audit once audit is free.
- **Fresh review:** `force-fresh-context` sets audit-2's records of it aside (kept), and the review is handed to audit-2
  afresh -- cleared only once idle, never while busy -- once, naming the exact candidate. Stage, owner and candidate
  are unchanged.
- **SYRD-874, what a placement is:** a review the balancer moved to audit-2, waiting there behind audit-2's current
  one, is balanced on to audit once audit is idle and empty: the balancer's own move is no placement, and neither is a
  non-Director change through its window (no row in review_placements). A reassignment through the board UI's
  delegated authority -- the User's consumed grant, caller the User, actor the Director -- is the Director's
  placement, and the balancer keeps it.
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
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "45f0d9f7fc2ee079671996894c3a7ffd4cc80fd3"
from review_pane_rig import PANE_ENV_PREFIXES  # noqa: E402  (beside this file)
CANDIDATE = "c873" + "0" * 36
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
    with review_board(root, "s873") as rig:
        sql, act, where, settle, handed, to_audit, seeded = (rig.sql, rig.act, rig.where, rig.settle, rig.handed,
                                                             rig.to_audit, rig.seeded)
        idle, busy, sent, app, cluster, fixture = rig.idle, rig.busy, rig.sent, rig.app, rig.cluster, rig.fixture
        session_context = rig.session_context

        # History: audit-2 reviews PGU-1 (its first hand-off binds its conversation) and kicks it back.
        seeded("PGU-1", "app")
        to_audit("PGU-1", "app")
        act("PGU-1", "reassign", "director", assignee="audit-2", reason="audit-2 reviews it")
        settle()
        seen["history_bound"] = sql("SELECT coalesce((SELECT state FROM ticket_board.ticket_role_contexts "
                                    "WHERE ticket_id='PGU-1' AND role='audit-2'), '');")
        act("PGU-1", "audit_kick_back", "audit-2", reason="Two findings in the citations.")
        idle("audit-2")
        # audit-2 then reviews PGU-2, which clears its pane for it: PGU-1's conversation is no longer the pane's.
        seeded("PGU-2", "ops")
        to_audit("PGU-2", "ops")
        act("PGU-2", "reassign", "director", assignee="audit-2", reason="audit-2 reviews it")
        settle()
        act("PGU-2", "audit_sign_off", "audit-2", text="Audited.")
        idle("audit-2")
        # audit takes PGU-3 and is busy reviewing it.
        seeded("PGU-3", "main")
        to_audit("PGU-3", "main")
        settle(3)
        seen["audit_current"] = where("PGU-3")
        # A placement ends when its ticket leaves the stage: PGU-6, placed by the Director with audit, kicked back
        # and resubmitted, is the board's to balance again -- to idle audit-2, never handed it, so a first hand-off.
        # SYRD-875: a rework returns to the reviewer that kicked it back and stays there; audit's provider is held at
        # the resubmission, so the return is given up and PGU-6 lands as a first submission does.
        busy("audit-2")
        seeded("PGU-6", "ops")
        to_audit("PGU-6", "ops")
        act("PGU-6", "reassign", "director", assignee="audit-2", reason="audit-2 reviews it")
        act("PGU-6", "reassign", "director", assignee="audit", reason="audit takes it after its current review")
        act("PGU-6", "audit_kick_back", "audit", reason="One finding.")
        rig.hold("audit")
        to_audit("PGU-6", "ops")
        rig.release("audit")
        mark = len(sent)
        idle("audit-2")
        settle(4)
        seen["stale"] = {"where": where("PGU-6"), "to_audit_2": handed("audit-2", "PGU-6", mark),
                         "returns": rig.comments("PGU-6", "Review return in audit")}
        if where("PGU-6") == "audit/audit-2":
            act("PGU-6", "audit_sign_off", "audit-2", text="Audited.")  # which frees ops for PGU-4
        elif where("PGU-6") == "audit/audit":
            act("PGU-6", "audit_sign_off", "audit", text="Audited.")
        idle("audit-2")

        # 1. The rework of PGU-1, its exact candidate recorded, comes back to Audit while audit is busy.
        act("PGU-1", "add_comment", "app", text="Reworked both findings.")
        rig.hold("audit-2")  # SYRD-875: so the rework lands with audit, as on main, and not back with audit-2
        to_audit("PGU-1", "app")
        rig.release("audit-2")
        sql(f"SET session_replication_role = replica; UPDATE ticket_board.tickets SET commit_hash = '{CANDIDATE}' "
            "WHERE id = 'PGU-1';")
        mark = len(sent)
        idle("audit-2")
        settle(6)
        time.sleep(session_context.CONFIRM_SECONDS + 0.3)
        settle(3)
        director = [text for role, text in sent if role == "director" and "PGU-1" in text and "restored" in text]
        seen["rework"] = {"where": where("PGU-1"), "to_audit_2": [t for r, t in sent[mark:] if r == "audit-2"],
                          "returns": rig.comments("PGU-1", "Review return in audit"),
                          "parked": sql("SELECT ticket_board.ticket_context_parked('PGU-1', 'audit-2');") == "t",
                          "director": director}

        # 2. The Director places a review with busy audit while audit-2 is idle and empty. On main that is MEFP's own
        # sequence: PGU-1, parked with audit-2, reassigned to audit by the Director.
        if where("PGU-1") == "audit/audit-2":
            act("PGU-1", "reassign", "director", assignee="audit",
                reason="audit-2's hand-off cannot be delivered; audit takes it after its current review")
            idle("audit-2")
            settle(4)
            seen["placed"] = {"ticket": "PGU-1", "after": where("PGU-1")}
            return seen
        # PGU-4 is placed by the Director with busy audit. It was never handed to audit-2 (busy while the Director
        # moved it), so audit-2, idle and empty, could take it as a first hand-off.
        busy("audit-2")
        seeded("PGU-4", "ops")
        to_audit("PGU-4", "ops")
        act("PGU-4", "reassign", "director", assignee="audit-2", reason="audit-2 reviews it")
        act("PGU-4", "reassign", "director", assignee="audit",
            reason="audit-2's hand-off cannot be delivered; audit takes it after its current review")
        mark = len(sent)
        idle("audit-2")
        settle(4)
        seen["placed"] = {"ticket": "PGU-4", "stale": seen.pop("stale", None), "after": where("PGU-4"),
                          "to_audit_2": handed("audit-2", "PGU-4", mark), "to_audit": handed("audit", "PGU-4", mark)}

        if sql("SELECT to_regprocedure('ticket_board.balance_review_stage_skipping(text, text, timestamptz, text[])') "
               "IS NOT NULL;") == "t":
            # 3. An older listener's balancer call -- balance_review_stage, which skips nothing, as during an upgrade
            # or a rollback -- moves PGU-1 to idle audit-2. Its hand-off parks, and the Director is told, once.
            mark = len(sent)
            older = json.loads(sql("SET ROLE ticket_board_listener; SELECT ticket_board.balance_review_stage("
                                   "'audit', 'audit-2', clock_timestamp())::text;").splitlines()[-1])
            settle(4)
            time.sleep(session_context.CONFIRM_SECONDS + 0.3)
            settle(3)
            notices = [text for role, text in sent[mark:] if role == "director" and "PGU-1's review" in text]
            stall = json.loads(sql("SET ROLE ticket_board_listener; SELECT coalesce(json_agg(json_build_object("
                                   "'role', role, 'why', why_not)), '[]')::text FROM ticket_board.board_stall_runnable("
                                   "clock_timestamp()) WHERE ticket_id = 'PGU-1';").splitlines()[-1])
            # audit signs off PGU-3 and is idle and empty: PGU-4, placed with it, is its next review, delivered
            # once; PGU-1, parked with audit-2 (and not placed), is not balanced to it either.
            act("PGU-3", "audit_sign_off", "audit", text="Audited.")
            idle("audit")
            settle(4)
            pgu4_to_audit = handed("audit", "PGU-4")
            if where("PGU-4") == "audit/audit":  # audit reviews PGU-4 and is idle and empty once more
                act("PGU-4", "audit_sign_off", "audit", text="Audited.")
            idle("audit")
            settle(4)
            seen["parked"] = {"older": older, "where": where("PGU-1"), "notices": notices, "stall": stall,
                              "to_audit_2": [t for r, t in sent[mark:] if r == "audit-2"],
                              "pgu4_to_audit": pgu4_to_audit, "audit_after": handed("audit", "PGU-1", mark),
                              "parked": sql("SELECT ticket_board.ticket_context_parked('PGU-1', 'audit-2');") == "t"}
            # 4. The fresh review: never while audit-2 is busy; once it is idle, cleared, then handed it, once.
            busy("audit-2")
            mark = len(sent)
            try:
                result = act("PGU-1", "force_fresh_context", "director", reason="Codex cannot resume; review afresh")
            except AssertionError as exc:  # a refusal is an answer
                result = {"refused": str(exc)[:300]}
            settle(3)
            while_busy = [t for r, t in sent[mark:] if r == "audit-2"]
            idle("audit-2")
            settle(6)
            seen["fresh"] = {
                "result": result, "while_busy": while_busy, "after": [t for r, t in sent[mark:] if r == "audit-2"],
                "where": where("PGU-1"),
                "commit": sql("SELECT commit_hash FROM ticket_board.tickets WHERE id='PGU-1';"),
                "parked": sql("SELECT ticket_board.ticket_context_parked('PGU-1', 'audit-2');") == "t",
                "archive": json.loads(sql("SELECT coalesce(json_agg(json_build_object('role', role, 'candidate', "
                                          "candidate, 'sessions', set_aside->'contexts', 'restores', "
                                          "jsonb_array_length(set_aside->'restores'))), '[]')::text "
                                          "FROM ticket_board.fresh_review_archive;")),
                "bound": sql("SELECT coalesce((SELECT session_id || '/' || state FROM ticket_board.ticket_role_contexts "
                             "WHERE ticket_id='PGU-1' AND role='audit-2'), '');"),
                "comment": [c["text"] for c in app.get_ticket("PGU-1")["comments"] if "fresh review" in c["text"]]}
            settle(3)
            seen["fresh"]["later"] = [t for r, t in sent[mark:] if r == "audit-2"]
            seen.update(placements_pinned(sql, act, where, settle, busy, idle, handed, sent, cluster, fixture))
    return seen


def placements_pinned(sql, act, where, settle, busy, idle, handed, sent, cluster, fixture) -> dict:
    """SYRD-874: a balancer move is never a placement, and a board-UI-delegated Director reassignment is one.

    Moves "by the balancer" here are the board's own balance_review_stage_skipping, called as the listener calls it,
    with audit-2 busy: the listener would otherwise hand the ticket over at once, and a review handed to its owner is
    never balanced again (SYRD-745's re-review rule), which would hide whether the move left a placement.
    """
    import secrets

    import psycopg

    service = fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE)
    seen: dict = {}

    def arrive(ticket: str) -> None:
        """A review arriving in Audit with no author slot involved: from Inspection, to audit, the stage's first owner."""
        fixture.seed_postgres_ticket(admin_of(sql), ticket, title=ticket, state="inspection", assignee="inspector",
                                     commit_exempt=True)
        act(ticket, "inspector_sign_off", "inspector")

    def balance() -> dict:
        return json.loads(sql("SET ROLE ticket_board_listener; SELECT ticket_board.balance_review_stage_skipping("
                              "'audit', 'audit-2', clock_timestamp(), '{}'::text[])::text;").splitlines()[-1])

    def placements(ticket: str) -> list:
        return json.loads(sql("SELECT coalesce(json_agg(json_build_object('role', role, 'by', placed_by)), '[]')::text "
                              f"FROM ticket_board.review_placements WHERE ticket_id = '{ticket}';"))

    def delegated_reassign(ticket: str, assignee: str, reason: str) -> str:
        """The board UI's path (SYRD-598): the User's confirmed grant, minted, then consumed in the transaction that
        reassigns, so the operation runs as the User with the Director's lent authority."""
        payload = json.dumps({"assignee": assignee, "reason": reason})
        token, session = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        try:
            with psycopg.connect(service) as conn:
                version = conn.execute("SELECT ticket_board.ui_ticket_version(%s)", (ticket,)).fetchone()[0]
                version = version.decode() if isinstance(version, bytes) else version
            with psycopg.connect(service) as conn, conn.transaction():
                conn.execute("SELECT set_config('ticket_board.caller_role', 'user', true)")
                conn.execute("SELECT ticket_board.mint_ui_delegation(%s, %s, %s, 'reassign', %s::jsonb, %s, 120)",
                             (token, session, ticket, payload, version))
            with psycopg.connect(service) as conn, conn.transaction():
                conn.execute("SELECT set_config('ticket_board.caller_role', 'user', true)")
                conn.execute("SELECT ticket_board.consume_ui_delegation(%s, %s, %s, 'reassign', %s::jsonb)",
                             (token, session, ticket, payload))
                conn.execute("SELECT ticket_board.reassign(%s, %s, %s)", (ticket, assignee, reason))
            return "ok"
        except psycopg.Error as exc:
            return str(exc).splitlines()[0][:200]

    # audit-2 finishes PGU-1's fresh review, and is kept busy so nothing moved to it is handed over.
    act("PGU-1", "audit_sign_off", "audit-2", text="Audited.")
    busy("audit-2")
    arrive("PGU-7")  # audit's current review
    settle(3)
    seen["current"] = {"where": where("PGU-7"), "handed": handed("audit", "PGU-7")}

    # UI-delegated placement: PGU-10, moved to audit-2 by the balancer (no placement), is put back with busy audit
    # through the board UI's delegated authority. audit-2, idle and empty, could then take it -- but it stays.
    arrive("PGU-10")
    moved = balance()
    by_balancer = placements("PGU-10")
    delegated = delegated_reassign("PGU-10", "audit", "the User keeps it with audit")
    recorded = placements("PGU-10")
    idle("audit-2")
    settle(3)
    seen["delegated"] = {"moved": moved, "by_balancer": by_balancer, "reassign": delegated, "recorded": recorded,
                         "after": where("PGU-10"), "to_audit_2": handed("audit-2", "PGU-10")}
    act("PGU-10", "defer", "director", reason="out of the way")  # audit's next review must not be it

    # A balancer move is never a placement. PGU-8 arrives first and waits (awaited, so the balancer leaves it); PGU-9
    # arrives next and the balancer moves it to busy audit-2. A non-Director change through the balancer's window then
    # puts PGU-8 with audit-2 too: by arrival, audit-2's current ticket, with PGU-9 waiting behind it. When audit is
    # idle and empty, PGU-9 -- moved there only by the balancer -- is the board's to balance to audit.
    busy("audit-2")
    arrive("PGU-8")
    sql("UPDATE ticket_board.ticket_notification_state SET awaiting_role = 'ops', awaiting_since_at = clock_timestamp() "
        "WHERE ticket_id = 'PGU-8';")
    arrive("PGU-9")
    moved = balance()
    sql("SET ticket_board.caller_role = 'audit'; SET ticket_board.review_balance_ticket = 'PGU-8'; "
        "UPDATE ticket_board.tickets SET assignee = 'audit-2' WHERE id = 'PGU-8';")
    seen["not_placements"] = {"moved": moved, "balancer": placements("PGU-9"), "non_director": placements("PGU-8"),
                              "pgu8": where("PGU-8")}
    mark = len(sent)
    act("PGU-7", "audit_sign_off", "audit", text="Audited.")
    idle("audit")
    settle(4)
    seen["not_placements"].update(after=where("PGU-9"), to_audit=handed("audit", "PGU-9", mark),
                                  pgu8_after=where("PGU-8"))
    return seen


def admin_of(sql) -> str:
    return sql.admin


def would_park_unit() -> list:
    """`review_balance.would_park` against a stand-in board: which waiting reviews the listener skips for audit-2."""
    for extra in (str(ROOT), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from types import SimpleNamespace
    from scripts.ticket_board import review_balance

    bound = {"state": "bound", "runtime": "claude", "session_id": "s"}
    statuses = {
        "PGU-1": {"parked": True, "context": bound},                       # a restore failed, on a runtime that resumes
        "PGU-2": {"parked": False, "context": {**bound, "runtime": "codex"}},  # a conversation it cannot restore
        "PGU-3": {"parked": False, "context": bound},                       # one it can
        "PGU-4": {"parked": False, "context": None},                        # never handed to it: a first hand-off
    }

    class Rows:
        def __init__(self, rows):
            self.rows = rows

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0]

    class Conn:
        def execute(self, statement, params=()):
            if "FROM ticket_board.tickets" in statement:
                return Rows([{"id": ticket} for ticket in statuses])
            return Rows([{"s": json.dumps(statuses[params[0]])}])

    runtimes = {"audit-2": "claude"}
    skipped = review_balance.would_park(SimpleNamespace(role_runtimes=runtimes), Conn(), "audit", "audit-2")
    runtimes["audit-2"] = "codex"
    return [skipped, review_balance.would_park(SimpleNamespace(role_runtimes=runtimes), Conn(), "audit", "audit-2")]


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


def before(got: dict) -> None:
    rework = got["rework"]
    check(got["history_bound"] == "bound", f"main: audit-2's first review of PGU-1 bound its conversation: {got}")
    check(rework["where"] == "audit/audit-2" and rework["parked"] and len(rework["director"]) == 1,
          f"main: the resubmission is balanced to idle audit-2 and its hand-off parks (MEFP-1234): {rework}")
    placed = got["placed"]
    check(placed == {"ticket": "PGU-1", "after": "audit/audit-2"},
          f"main: the Director's reassignment of parked PGU-1 to busy audit is undone by the balancer: {placed}")


def candidate(got: dict) -> None:
    check(got["history_bound"] == "bound" and got["audit_current"] == "audit/audit",
          f"audit-2's first review of PGU-1 bound its conversation; audit is reviewing PGU-3: {got}")
    rework = got["rework"]
    check(rework["where"] == "audit/audit" and rework["to_audit_2"] == [] and not rework["parked"]
          and rework["director"] == [],
          f"the resubmission stays with audit: idle audit-2 cannot restore its saved conversation of it, so it is not "
          f"a target, is sent nothing, and nothing parks: {rework}")
    check(rework["returns"] == ["Review return in audit: audit-2 kicked this back, but cannot take the rework (its "
                                "provider is out of quota), so it lands with audit, the first owner, and may be balanced."],
          f"SYRD-875: audit-2, held at the resubmission, cannot take the rework back, and the ticket says so: {rework}")
    placed = got["placed"]
    check(placed["stale"]["returns"] == ["Review return in audit: audit kicked this back, but cannot take the rework "
                                         "(its provider is out of quota), so it lands with audit, the first owner, and "
                                         "may be balanced."],
          f"SYRD-875: audit, held at PGU-6's resubmission, gives its return up: {placed['stale']}")
    check(placed["stale"]["where"] == "audit/audit-2" and len(placed["stale"]["to_audit_2"]) == 1,
          f"a placement ends when its ticket leaves the stage: PGU-6, placed with audit, kicked back and resubmitted, "
          f"is balanced to idle audit-2 and handed to it: {placed['stale']}")
    check(placed["ticket"] == "PGU-4" and placed["after"] == "audit/audit" and placed["to_audit_2"] == [],
          f"PGU-4, placed by the Director with busy audit, stays with audit while audit-2 is idle and empty and could "
          f"take it: {placed}")
    check(placed["to_audit"] == [], f"busy audit is not interrupted with it: {placed}")
    parked = got["parked"]
    check(parked["older"].get("moved") is True and parked["older"].get("ticket") == "PGU-1",
          f"an older listener's call, skipping nothing, still moves the waiting PGU-1 to audit-2: {parked['older']}")
    check(parked["where"] == "audit/audit-2" and parked["parked"] and parked["to_audit_2"] == [],
          f"so its hand-off parks: audit-2 is sent nothing, no /resume, no clear: {parked}")
    check(len(parked["notices"]) == 1, f"the Director is told once: {parked['notices']}")
    notice = parked["notices"][0] if parked["notices"] else ""
    for part in ("PGU-1's review by audit-2 is parked", "codex has no verified in-session resume", CANDIDATE,
                 "reassign PGU-1 to another owner of audit", "force-fresh-context PGU-1"):
        check(part in notice, f"the notice names {part!r}: {notice}")
    check(parked["stall"] == [{"role": "director", "why": "its next action is the Director's"}],
          f"the stall clock counts it as the Director's next action, not audit-2 stalling: {parked['stall']}")
    check(len(parked["pgu4_to_audit"]) == 1, f"audit, free again, is handed PGU-4 -- placed with it -- once: {parked}")
    check(parked["where"] == "audit/audit-2" and parked["audit_after"] == [],
          f"and, once audit is idle and empty, parked PGU-1 is not balanced to it: its recovery is the Director's: "
          f"{parked}")
    fresh = got["fresh"]
    check(fresh["result"].get("fresh_review") is True and fresh["result"].get("candidate") == CANDIDATE,
          f"force-fresh-context starts a fresh review of the exact candidate: {fresh['result']}")
    check(fresh["while_busy"] == [], f"nothing reaches busy audit-2: no clear, no ticket: {fresh['while_busy']}")
    after = fresh["after"]
    check(after[:1] == ["/clear"] and len([t for t in after if t.startswith("PGU-1 -- ")]) == 1
          and not any(t.startswith("/resume") for t in after),
          f"once idle, audit-2 is cleared and then handed PGU-1, once, never resumed: {after}")
    handoff = next((t for t in after if t.startswith("PGU-1 -- ")), "")
    check("fresh review of the exact candidate " + CANDIDATE in handoff,
          f"the hand-off names the exact candidate: {handoff}")
    check(fresh["where"] == "audit/audit-2" and fresh["commit"] == CANDIDATE and not fresh["parked"],
          f"stage, owner and candidate are unchanged, and it is no longer parked: {fresh}")
    check(len(fresh["archive"]) == 1 and fresh["archive"][0]["role"] == "audit-2"
          and fresh["archive"][0]["candidate"] == CANDIDATE and fresh["archive"][0]["restores"] == 1
          and (fresh["archive"][0]["sessions"] or [{}])[0].get("session_id", "").startswith("audit-2-s"),
          f"audit-2's records of PGU-1's old conversation are set aside, kept whole: {fresh['archive']}")
    check(fresh["bound"].endswith("/bound") and not fresh["bound"].startswith(
          (fresh["archive"][0]["sessions"] or [{}])[0].get("session_id", "?") + "/"),
          f"the fresh review is bound to its own new conversation: {fresh['bound']}")
    check(len(fresh["comment"]) == 1 and "Codex cannot resume" in fresh["comment"][0],
          f"the fresh review is recorded on the ticket with the Director's reason: {fresh['comment']}")
    check(fresh["later"] == fresh["after"], f"and nothing more is sent: delivered once: {fresh['later']}")
    check(got["current"]["where"] == "audit/audit" and len(got["current"]["handed"]) == 1,
          f"audit is handed PGU-7 and busy reviewing it: {got['current']}")
    delegated = got["delegated"]
    check(delegated["moved"].get("ticket") == "PGU-10" and delegated["by_balancer"] == [],
          f"the balancer moves PGU-10 to audit-2 and records no placement: {delegated}")
    check(delegated["reassign"] == "ok" and delegated["recorded"] == [{"role": "audit", "by": "director"}],
          f"a reassignment through the board UI's delegated authority -- caller the User, actor the Director -- is "
          f"the Director's placement: {delegated}")
    check(delegated["after"] == "audit/audit" and delegated["to_audit_2"] == [],
          f"and the balancer keeps it with busy audit while audit-2 is idle and empty: {delegated}")
    pinned = got["not_placements"]
    check(pinned["moved"].get("ticket") == "PGU-9" and pinned["balancer"] == [],
          f"the balancer's move of PGU-9 to audit-2 leaves no placement: {pinned}")
    check(pinned["pgu8"] == "audit/audit-2" and pinned["non_director"] == [],
          f"nor does a non-Director owner change through the balancer's window (PGU-8, to audit-2): {pinned}")
    check(pinned["after"] == "audit/audit" and len(pinned["to_audit"]) == 1 and pinned["pgu8_after"] == "audit/audit-2",
          f"so PGU-9, waiting behind audit-2's current PGU-8, is balanced to audit once audit is idle and empty, and "
          f"handed to it once: {pinned}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]))))
        return 0
    if os.environ.get("SYRD873_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        with tempfile.TemporaryDirectory(prefix="syrd873-before.", dir="/tmp") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    claude, codex = would_park_unit()
    check(claude == ["PGU-1", "PGU-2"],
          f"for a Claude audit-2: a failed restore and a conversation from another runtime are skipped; one it can "
          f"resume and a first hand-off are not: {claude}")
    check(codex == ["PGU-1", "PGU-2", "PGU-3"],
          f"for a Codex audit-2, which resumes no saved conversation: every review it holds one of is skipped, and only "
          f"a first hand-off is not: {codex}")
    candidate(run(ROOT))
    print(f"review_recovery_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
