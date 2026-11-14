#!/usr/bin/env python3
"""SYRD-772: a ticket past Implementation behind a blocker gives up its implementer's slot, and the next is handed over.

Live on syrd (2026-11-14): SYRD-768, Main's, entered DAT -- the Director's acceptance, a review stage, which holds its
author's slot -- and the Director recorded `operator:eric` on it, waiting on a deploy for UAT. SYRD-769 and SYRD-770 sat
behind it in Main's queue, assigned and in progress, and Main was told nothing: the slot was freed only when SYRD-768
went on to User Review. SYRD-568/726's rule -- a blocked ticket holds nothing -- applied in Implementation only.

Now a ticket with an unresolved blocker holds nothing in any stage that would hold its implementer, and the ticket
already active keeps the slot ahead of work past implementation, so a ticket unblocked later never takes the slot back
from work under way. Checked on a production-built board (schema.sql, roles, rbac.sql, every migration) with the
shipped workflow, the real HTTP server and the real notify listener with an injected sender:

* main: A in DAT, B queued behind it. `operator:eric` on A: B becomes main's active ticket and is handed over once.
  The blocker released: B keeps the slot, and A, in DAT, holds nothing. The Director sends A back: it queues behind B.
* app: C in Audit (a review stage held by its author), D queued. C blocked by another ticket: D is handed over.
* ops: E in DAT, F queued, `project:` blocker: F is handed over.
* main's code (the before-run) leaves B queued and unannounced, as it did live.
* An upgrade: main's board in exactly that stuck state, then this tree's migrations: the waiting ticket is handed over.
* A fresh schema.sql board installs the same function as the migrations.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import user_review_serial_release_test as urs  # noqa: E402 - its production-built board, listener and helpers

BEFORE = "af71ca4da6da03deae11736ea12372501f38545c"  # main before SYRD-772
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def to_dat(board, ticket: str, author: str) -> list[dict]:
    return [board.act(ticket, "submit_to_audit_without_commit", author, reason="finished"),
            board.act(ticket, "audit_sign_off", "audit", text="Audited.")]


def activated(batch: list[tuple[str, str]], role: str, ticket: str) -> list[str]:
    return [m for m in urs.about(batch, role, ticket) if "now your active ticket" in m]


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = urs.build_board(fixture, cluster, root, "board")
        board = urs.Board(fixture, cluster, admin, "board", "a")
        board.app.apply_workflow(urs.workflow(root), expected_revision=0, dry_run=False, caller_role="director")
        A, B, C, D, E, F, X = "PGU-701", "PGU-702", "PGU-711", "PGU-712", "PGU-721", "PGU-722", "PGU-730"
        G, H = "PGU-741", "PGU-742"
        for ticket in (A, B, C, D, E, F, X, G, H):
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director",
                                         commit_exempt=True, needs_user_signoff=True)
        board.one_pass()

        # --- main: A in DAT holds main; B waits behind it, unannounced.
        board.act(A, "route", "director", state="in_progress", assignee="main")
        board.one_pass()
        seen["a_to_dat"] = to_dat(board, A, "main")
        seen["b_route"] = board.act(B, "route", "director", state="in_progress", assignee="main")
        seen["dat_holds"] = [board.where(A), board.reservation("main"), board.waiting(B),
                             urs.about(board.one_pass(), "main", B)]
        # The Director records operator:eric on A, waiting on a deploy for UAT.
        seen["a_blocked"] = board.act(A, "set_blockers", "director", blocked_by=["operator:eric"],
                                      blocked_reason="waiting on the deploy for UAT")
        first = board.one_pass()
        again = board.one_pass(board.new_listener()) + board.one_pass()
        seen["b_handed"] = [board.reservation("main"), board.waiting(B), activated(first, "main", B),
                            activated(again, "main", B), board.ticket_json(B).get("serial_queue")]
        seen["a_queue"] = board.ticket_json(A).get("serial_queue")
        # The operator acts and the Director releases the blocker: B keeps the slot; A, in DAT, holds nothing.
        seen["a_release"] = board.act(A, "release_external_blocker", "director", ref="operator:eric",
                                      reason="deployed; UAT can start")
        released = board.one_pass() + board.one_pass()
        seen["after_release"] = [board.reservation("main"), board.waiting(B), board.where(A),
                                 activated(released, "main", A), activated(released, "main", B)]
        # The Director sends A back to main: the rework queues behind B and does not interrupt it.
        seen["a_back"] = board.act(A, "director_dat_kick_back", "director", reason="the deploy showed a defect")
        seen["a_rework_waits"] = [board.where(A), board.waiting(A), board.reservation("main"),
                                  activated(board.one_pass(), "main", A)]
        # B goes on to the User, releasing main: the rework is handed over, and resubmitted it holds main in Audit
        # again -- re-entering implementation cleared its release.
        seen["b_on"] = to_dat(board, B, "main") + [urs.to_user(board, B)]
        seen["a_reactivated"] = [board.reservation("main"), board.waiting(A), activated(board.one_pass(), "main", A)]
        seen["a_resubmit"] = [board.act(A, "submit_to_audit_without_commit", "main", reason="fixed"), board.where(A),
                              board.reservation("main")]

        # --- app: C in Audit, blocked by another ticket; D is handed over.
        board.act(C, "route", "director", state="in_progress", assignee="app")
        board.one_pass()
        seen["c_submit"] = board.act(C, "submit_to_audit_without_commit", "app", reason="finished")
        seen["d_route"] = board.act(D, "route", "director", state="in_progress", assignee="app")
        seen["audit_holds"] = [board.where(C), board.reservation("app"), board.waiting(D)]
        seen["c_blocked"] = board.act(C, "set_blockers", "director", blocked_by=[X], blocked_reason="after X")
        seen["d_handed"] = [board.reservation("app"), board.waiting(D), activated(board.one_pass(), "app", D)]

        # --- ops: E in DAT behind another board's ticket; F is handed over.
        board.act(E, "route", "director", state="in_progress", assignee="ops")
        board.one_pass()
        seen["e_to_dat"] = to_dat(board, E, "ops")
        seen["f_route"] = board.act(F, "route", "director", state="in_progress", assignee="ops")
        seen["ops_dat_holds"] = [board.where(E), board.reservation("ops"), board.waiting(F)]
        seen["e_blocked"] = board.act(E, "set_blockers", "director", blocked_by=["otherboard:OTH-5"],
                                      blocked_reason="needs the other board's fix")
        seen["f_handed"] = [board.reservation("ops"), board.waiting(F), activated(board.one_pass(), "ops", F)]

        # --- ops: G blocked in implementation and moved past it by the Director stays released when unblocked:
        # F, ops's active ticket, keeps the slot (review work would otherwise come first).
        board.act(G, "route", "director", state="in_progress", assignee="ops")
        seen["g_blocked"] = board.act(G, "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="x")
        seen["g_moved"] = board.act(G, "force_move", "director", state="dat", assignee="director")
        seen["h_route"] = board.act(H, "route", "director", state="in_progress", assignee="ops")
        board.one_pass()
        seen["g_release"] = board.act(G, "release_external_blocker", "director", ref="operator:eric", reason="done")
        seen["g_stays_released"] = [board.where(G), board.reservation("ops"), board.waiting(H)]

        # --- lifecycle: L, sent back by the User, waits in a system stage holding main; blocked, it releases main.
        life_admin = urs.build_board(fixture, cluster, root, "lifeboard", roles=False)
        life = urs.Board(fixture, cluster, life_admin, "lifeboard", "l")
        life.app.apply_workflow(urs.workflow(root, "lifecycle"), expected_revision=0, dry_run=False, caller_role="director")
        L, M = "PGU-751", "PGU-752"
        for ticket in (L, M):
            fixture.seed_postgres_ticket(life_admin, ticket, title=ticket, state="analysis", assignee="director",
                                         commit_exempt=True, needs_user_signoff=True)
        life.act(L, "route", "director", state="in_progress", assignee="main")
        life.one_pass()
        seen["l_steps"] = urs.to_user_review(life, L, "main")
        seen["l_sent_back"] = (life.act(L, "user_send_to_director", "user", reason="UAT: wrong") if life.uat
                               else life.act(L, "user_kick_back", "user", reason="UAT: wrong"))
        seen["m_route"] = life.act(M, "route", "director", state="in_progress", assignee="main")
        seen["lifecycle_holds"] = [life.where(L), life.sql(f"SELECT ticket_board.declared_stage_kind(state) FROM "
                                                           f"ticket_board.tickets WHERE id = '{L}';"),
                                   life.reservation("main"), life.waiting(M)]
        life.one_pass()
        seen["l_blocked"] = life.act(L, "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="ask")
        seen["m_handed"] = [life.reservation("main"), life.waiting(M), activated(life.one_pass(), "main", M)]
        # app: S blocked in DAT and unblocked there, then on to Done and reopened. Its release ended with the pass, so
        # the reopen holds app as SYRD-276 has it, and T routed afterwards waits (Audit of dcb9dc0b).
        S, T, R, Q = "PGU-761", "PGU-762", "PGU-771", "PGU-772"
        for ticket in (S, T, R, Q):
            fixture.seed_postgres_ticket(life_admin, ticket, title=ticket, state="analysis", assignee="director",
                                         commit_exempt=True, needs_user_signoff=True)
        seen["s_steps"] = [life.act(S, "route", "director", state="in_progress", assignee="app")]
        life.one_pass()
        seen["s_steps"] += to_dat(life, S, "app") + [
            life.act(S, "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="deploy"),
            life.act(S, "release_external_blocker", "director", ref="operator:eric", reason="deployed"),
            urs.to_user(life, S), life.act(S, "user_sign_off", "user"), life.act(S, "mark_done", "director")]
        has_table = life.sql("SELECT to_regclass('ticket_board.blocked_slot_releases') IS NOT NULL;") == "t"
        seen["s_done"] = [life.where(S), life.sql(f"SELECT count(*) FROM ticket_board.blocked_slot_releases "
                                                 f"WHERE ticket_id = '{S}';") if has_table else "absent (main)"]
        seen["s_reopen"] = life.act(S, "reopen", "director", reason="regressed")
        seen["s_reopened"] = [life.where(S), life.reservation("app")]
        seen["t_route"] = life.act(T, "route", "director", state="in_progress", assignee="app")
        seen["t_waits"] = [life.reservation("app"), life.waiting(T)]
        # ops: R blocked in DAT and unblocked, then moved to a draft stage and back to analysis: the draft ended its
        # pass too, so in analysis it holds ops again and Q waits.
        seen["r_steps"] = [life.act(R, "route", "director", state="in_progress", assignee="ops")]
        life.one_pass()
        seen["r_steps"] += to_dat(life, R, "ops") + [
            life.act(R, "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="deploy"),
            life.act(R, "release_external_blocker", "director", ref="operator:eric", reason="deployed"),
            life.act(R, "force_move", "director", state="backlog", assignee="director"),
            life.act(R, "force_move", "director", state="analysis", assignee="director")]
        seen["r_back"] = [life.where(R), life.reservation("ops")]
        seen["q_route"] = life.act(Q, "route", "director", state="in_progress", assignee="ops")
        seen["q_waits"] = [life.reservation("ops"), life.waiting(Q)]
        life.close()
        fixture.TEST_WRITE_TOKEN = board.server.write_token

        # Copy parity: a fresh schema.sql board installs what the migrations do.
        try:
            fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
            fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
            fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
            functions = ("ticket_current_reserved_ticket(text, text)", "record_blocked_slot_release(text)",
                         "record_blocked_slot_release_for_ticket()", "record_blocked_slot_release_for_blocker()")
            definition = "SELECT md5(pg_get_functiondef('ticket_board.{}'::regprocedure));"
            seen["fresh_equals_migrated"] = all(fixture.psql(fresh, definition.format(f)).strip()
                                                == board.sql(definition.format(f)) for f in functions)
            triggers = ("SELECT string_agg(tgrelid::regclass::text || ':' || tgname || ':' || tgfoid::regproc::text, ',' "
                        "ORDER BY tgname) FROM pg_trigger WHERE tgname LIKE '%blocked_slot_release%';")
            seen["triggers"] = [fixture.psql(fresh, triggers).strip(), board.sql(triggers)]
            seen["grants"] = board.sql("SELECT string_agg(grantee || ':' || privilege_type, ',' ORDER BY grantee, "
                                       "privilege_type) FROM information_schema.role_table_grants WHERE table_schema = "
                                       "'ticket_board' AND table_name = 'blocked_slot_releases' AND grantee LIKE "
                                       "'ticket_board_%';")
        except Exception as exc:  # noqa: BLE001 - reported, not raised
            seen["fresh_equals_migrated"] = f"unavailable: {str(exc)[-160:]}"
        board.close()
    return seen


def upgrade(before_root: Path, prefix: str) -> dict:
    """main's board stuck as syrd was -- A in DAT behind operator:eric, B queued -- then this tree's migrations."""
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        admin = urs.build_board(fixture, cluster, before_root, "old")
        board = urs.Board(fixture, cluster, admin, "old", "old")
        board.app.apply_workflow(urs.workflow(before_root), expected_revision=0, dry_run=False, caller_role="director")
        A, B, Z = "PGU-801", "PGU-802", "PGU-809"
        for ticket in (A, B, Z):
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director",
                                         commit_exempt=True, needs_user_signoff=True)
        board.act(A, "route", "director", state="in_progress", assignee="main")
        board.one_pass()
        to_dat(board, A, "main")
        board.act(B, "route", "director", state="in_progress", assignee="main")
        board.act(A, "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="deploy for UAT")
        seen["stuck"] = [board.where(A), board.reservation("main"), board.waiting(B),
                         activated(board.one_pass(), "main", B)]
        # Z, finished while blocked: its pass is over, so the upgrade records nothing for it.
        board.act(Z, "set_blockers", "director", blocked_by=["operator:eric"], blocked_reason="x")
        seen["z_moved"] = board.act(Z, "force_move", "director", state="done", assignee="director")
        urs.migrate(fixture, admin, ROOT)
        upgraded = board.one_pass(board.new_listener())
        seen["upgraded"] = [board.reservation("main"), board.waiting(B), activated(upgraded, "main", B)]
        board.act(A, "release_external_blocker", "director", ref="operator:eric", reason="deployed")
        seen["upgraded_release"] = [board.reservation("main"), board.waiting(B), board.where(A)]
        seen["upgraded_records"] = board.sql("SELECT coalesce(string_agg(ticket_id, ',' ORDER BY ticket_id), '') "
                                             "FROM ticket_board.blocked_slot_releases;")
        board.close()
    return seen


def child(root: Path, mode: str, prefix: str) -> dict:
    """Each run in its own process: the before-run imports main's modules, never this tree's."""
    env = urs.clean_env()
    done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--" + mode, str(root), prefix],
                          capture_output=True, text=True, timeout=1200, env=env, cwd=str(root))
    check(done.returncode == 0, f"the {mode} run finishes: {done.stderr[-2000:]}")
    return json.loads(done.stdout.strip().splitlines()[-1])


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] in ("--scenario", "--upgrade"):
        root = Path(sys.argv[2])
        for extra in (str(root), str(root / "tests")):
            sys.path.insert(0, extra)
        result = (scenario if sys.argv[1] == "--scenario" else upgrade)(root, sys.argv[3])
        print(json.dumps(result, default=str))
        return 0
    urs.clean_env()

    s = child(ROOT, "scenario", "s772a")
    check(s["dat_holds"][0] == ["dat", "director"] and s["dat_holds"][1] == "PGU-701" and s["dat_holds"][2] == "t"
          and s["dat_holds"][3] == [],
          f"A in DAT holds main, and B waits behind it, unannounced: {s['dat_holds']} {s['a_to_dat']}")
    check("ok" in s["a_blocked"], f"the Director records operator:eric on A: {s['a_blocked']}")
    check(s["b_handed"][0] == "PGU-702" and s["b_handed"][1] == "f",
          f"A behind operator:eric gives up main's slot, and B is main's active ticket: {s['b_handed'][:2]}")
    check(len(s["b_handed"][2]) == 1 and s["b_handed"][3] == [],
          f"main is told once that B is its active ticket, and not again: {s['b_handed'][2:4]}")
    queue = s["b_handed"][4] or {}
    check(queue.get("active") is True and queue.get("position") == 1,
          f"B's serial_queue says it is active: {queue}")
    check(not (s["a_queue"] or {}).get("active"), f"A's serial_queue no longer says it holds main: {s['a_queue']}")
    check("ok" in s["a_release"], f"the Director releases operator:eric: {s['a_release']}")
    check(s["after_release"][0] == "PGU-702" and s["after_release"][1] == "f" and s["after_release"][2] == ["dat", "director"]
          and s["after_release"][3] == [] and s["after_release"][4] == [],
          f"released, A does not take the slot back from B, and nobody is told otherwise: {s['after_release']}")
    check("ok" in s["a_back"] and s["a_rework_waits"][0] == ["in_progress", "main"] and s["a_rework_waits"][1] == "t"
          and s["a_rework_waits"][2] == "PGU-702" and s["a_rework_waits"][3] == [],
          f"sent back to main, A queues behind B and is not handed over: {s['a_back']} {s['a_rework_waits']}")
    check(all("ok" in r for r in s["b_on"]) and s["a_reactivated"][0] == "PGU-701" and s["a_reactivated"][1] == "f"
          and len(s["a_reactivated"][2]) == 1,
          f"B with the User releases main: the rework is handed over once: {s['b_on']} {s['a_reactivated']}")
    check("ok" in s["a_resubmit"][0] and s["a_resubmit"][1] == ["audit", "audit"] and s["a_resubmit"][2] == "PGU-701",
          f"resubmitted, the rework holds main in Audit again: its release ended when it re-entered implementation: "
          f"{s['a_resubmit']}")
    check(s["audit_holds"] == [["audit", "audit"], "PGU-711", "t"],
          f"C in Audit holds app, and D waits: {s['audit_holds']} {s['c_submit']}")
    check("ok" in s["c_blocked"] and s["d_handed"][0] == "PGU-712" and s["d_handed"][1] == "f" and len(s["d_handed"][2]) == 1,
          f"C blocked by another ticket gives up app's slot; D is handed over once: {s['c_blocked']} {s['d_handed']}")
    check(s["ops_dat_holds"] == [["dat", "director"], "PGU-721", "t"],
          f"E in DAT holds ops, and F waits: {s['ops_dat_holds']} {s['e_to_dat']}")
    check("ok" in s["e_blocked"] and s["f_handed"][0] == "PGU-722" and s["f_handed"][1] == "f" and len(s["f_handed"][2]) == 1,
          f"E behind another board's ticket gives up ops's slot; F is handed over once: {s['e_blocked']} {s['f_handed']}")
    check(all("ok" in s[k] for k in ("g_blocked", "g_moved", "h_route", "g_release"))
          and s["g_stays_released"] == [["dat", "director"], "PGU-722", "t"],
          f"G, blocked in implementation and moved on, stays released once unblocked: F keeps ops, H waits: "
          f"{s['g_stays_released']} {[s[k] for k in ('g_blocked', 'g_moved', 'g_release')]}")
    check(s["lifecycle_holds"][1] == "system" and s["lifecycle_holds"][2] == "PGU-751" and s["lifecycle_holds"][3] == "t",
          f"on a lifecycle board, L sent back by the User waits in a system stage holding main, and M waits: "
          f"{s['lifecycle_holds']} {s['l_steps']} {s['l_sent_back']}")
    check("ok" in s["l_blocked"] and s["m_handed"][0] == "PGU-752" and s["m_handed"][1] == "f" and len(s["m_handed"][2]) == 1,
          f"blocked there, L releases main, and M is handed over once: {s['l_blocked']} {s['m_handed']}")
    check(all("ok" in r for r in s["s_steps"]) and s["s_done"] == [["done", "director"], "0"],
          f"S, blocked and unblocked in DAT, reaches Done with its release ended: {s['s_done']} {s['s_steps']}")
    check("ok" in s["s_reopen"] and s["s_reopened"][0][0] == "analysis" and s["s_reopened"][1] == "PGU-761",
          f"reopened on a lifecycle board, S holds app again, as SYRD-276 has it: {s['s_reopen']} {s['s_reopened']}")
    check("ok" in s["t_route"] and s["t_waits"] == ["PGU-761", "t"],
          f"and T, routed afterwards, waits behind it: {s['t_route']} {s['t_waits']}")
    check(all("ok" in r for r in s["r_steps"]) and s["r_back"] == [["analysis", "director"], "PGU-771"]
          and "ok" in s["q_route"] and s["q_waits"] == ["PGU-771", "t"],
          f"R, through a draft stage and back, holds ops again and Q waits: {s['r_back']} {s['q_waits']} {s['r_steps']}")
    check(s["triggers"][0] == s["triggers"][1] and s["triggers"][0].count("blocked_slot_release") == 4,
          f"a fresh board has the migrated triggers: {s['triggers']}")
    check(s["grants"] == "ticket_board_listener:SELECT,ticket_board_service:SELECT",
          f"the service and the listener may read the release record: {s['grants']}")
    check(s["fresh_equals_migrated"] is True,
          f"a fresh schema.sql board installs the migrated ticket_current_reserved_ticket: {s['fresh_equals_migrated']}")

    with tempfile.TemporaryDirectory(prefix="syrd772-before.", dir="/tmp") as tmp:
        before = urs.tree_at(BEFORE, Path(tmp) / "before")
        b = child(before, "scenario", "s772b")
        check(b["b_handed"][0] == "PGU-701" and b["b_handed"][1] == "t" and b["b_handed"][2] == [],
              f"before: main's code keeps A holding main behind operator:eric, and B is never handed over: {b['b_handed']}")
        u = child(before, "upgrade", "s772u")
        check(u["stuck"][1] == "PGU-801" and u["stuck"][2] == "t" and u["stuck"][3] == [],
              f"main's board, stuck as syrd was: {u['stuck']}")
        check(u["upgraded"][0] == "PGU-802" and u["upgraded"][1] == "f" and len(u["upgraded"][2]) == 1,
              f"the upgrade hands B over and tells main once: {u['upgraded']}")
        check(u["upgraded_release"] == ["PGU-802", "f", ["dat", "director"]],
              f"and the blocker released after it, A, recorded as released by the upgrade, does not take main back: "
              f"{u['upgraded_release']}")
        check("ok" in u["z_moved"] and u["upgraded_records"] == "PGU-801",
              f"the upgrade records A, blocked in DAT, and not Z, finished while blocked: {u['upgraded_records']} "
              f"{u['z_moved']}")
    print(f"blocked_ticket_slot_release_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
