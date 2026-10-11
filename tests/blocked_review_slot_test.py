#!/usr/bin/env python3
"""SYRD-581: Audit sat idle while its next tickets waited behind a blocked one.

Live on MEFP (release f92a3d4d, workflow revision 115), 2026-10-11: Audit
signed off MEFP-592's no-code resubmission, which had followed an Audit
kick-back, and 592 left Audit. MEFP-593 still read "finish current" past its
retry time, with 594 and 595 behind it, while Audit was live and idle.
SYRD-542's idle release (pgu975) did not clear it.

The board read-only at the time showed what held them. MEFP-529 had been in
Audit since 2026-10-07, blocked on MEFP-665, with nothing queued. Every other
Audit ticket was pending as "finish current". The listener drops a blocked
ticket's notice, so 529 was never handed over again. Serial review admission
still counted it as Audit's current ticket, being the earliest arrival, and
held everything behind it. The idle release asks the same check, so it
released nothing either.

This suite plays that sequence on a production-built board (companion roles,
schema.sql, the real migrate runner, rbac.sql). The workflow is declared and
shaped like MEFP's: implementers submit straight to a serial Audit. The real
notify listener is driven one loop iteration at a time, with an activity gate
whose idle evidence the scenario controls. The sequence:
- K (529) is submitted, kicked back, resubmitted and then blocked;
- A (592) is submitted, kicked back, resubmitted as no-code and approved;
- B (593) and C (594) are held behind them.

It is run three ways:
- on main, from a git archive in a child process;
- on the deployed release f92a3d4, the same way, which is then upgraded with
  this tree's migrations, as MEFP will be;
- on this tree.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "0b3e2c66293d1f92a548fa0c3e4b12342dd9630f"  # main before SYRD-581
DEPLOYED = "f92a3d4df21dd15b50e66703f9d81d3817d5fd3b"  # MEFP's release when it was seen
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


class Gate:
    """The listener's activity gate: busy or idle per target, and the idle evidence its passes read."""

    def __init__(self) -> None:
        self.busy: set[str] = set()
        self.idle: dict[str, str] = {}

    def check(self, target: str) -> bool:
        return target in self.busy

    def idle_since_by_role(self, roles):
        return {role: since for role, since in self.idle.items() if role in roles}


def scenario(root: Path, prefix: str, upgrade_root: Path | None = None) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from scripts.ticket_board.notify_listener import TicketBoardNotifyListener
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster:
        dbname = "board"
        admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)

        def install(into: str, source: Path, *, migrate: bool) -> None:
            fixture.psql(into, (source / "scripts/ticket_board/schema.sql").read_text())
            fixture.psql(into, (source / "scripts/ticket_board/rbac.sql").read_text())
            if migrate:
                upgrade(into, source)

        def upgrade(into: str, source: Path) -> None:
            result = subprocess.run(["bash", str(source / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                    env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": into}, timeout=300)
            assert result.returncode == 0, result.stderr[-1500:]
            fixture.psql(into, (source / "scripts/ticket_board/rbac.sql").read_text())

        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        fixture.create_roles(admin)
        install(admin, root, migrate=True)
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")

        def act(ticket: str, operation: str, actor: str, **payload):
            return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)

        def sql(statement: str, conn: str = admin) -> str:
            return fixture.psql(conn, statement).strip()

        def row(ticket: str, role: str = "audit") -> list:
            raw = sql("SELECT coalesce(json_agg(json_build_array(coalesce(last_error, ''), attempts, "
                      "extract(epoch FROM next_attempt_at - clock_timestamp())::int))::text, '[]') "
                      f"FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}' "
                      f"AND kind = 'transition' AND target_role = '{role}';")
            return json.loads(raw)

        def where(ticket: str) -> list:
            return json.loads(sql(f"SELECT json_build_array(state, assignee)::text FROM ticket_board.tickets WHERE id = '{ticket}';"))

        sent: list[tuple[str, str]] = []
        gate = Gate()

        def new_listener():
            return TicketBoardNotifyListener(conninfo=listener_url, sender=lambda target, message: sent.append((target, message)),
                                             activity_gate=gate.check, target_exists=lambda _target: True,
                                             submission_witness=lambda target, _since: any(t == target for t, _m in sent),
                                             poll_seconds=0, project="pgu")

        listener = new_listener()

        import psycopg

        def one_pass(current=None) -> list[tuple[str, str]]:
            """One iteration of the listener's loop, as listen_once runs it, without its wait."""
            current = current or listener
            before = len(sent)
            with psycopg.connect(listener_url, autocommit=True) as conn:
                current.refresh_workflow(conn)
                for step in ("process_reminder_snooze_due", "process_idle_turn_end_nudges", "process_idle_stall_nudges",
                             "process_serial_focus_queue_wakeups"):
                    if hasattr(current, step):
                        getattr(current, step)(conn)
                current.process_due_notifications(conn)
            return sent[before:]

        def audit_got(batch, ticket: str) -> list[str]:
            """Handoffs to Audit: a ticket entering Audit, entering it again, or unblocked in it."""
            return [m for t, m in batch if t == "pgu-audit:0.0" and m.startswith(ticket + " ")
                    and (m.endswith((" entered Audit", " is active again in Audit", " is unblocked")))]

        def hold(*tickets: str) -> None:
            """The listener's own doubling delay: make each held row due and let a pass hold it again."""
            sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                f"WHERE ticket_id IN ({', '.join(repr(t) for t in tickets)}) AND kind = 'transition';")
            one_pass()

        def idle_release(current=None) -> None:
            """Audit's pane goes idle: the pass that reads that evidence releases what nothing holds any more."""
            gate.idle["audit"] = datetime.now(timezone.utc).isoformat()
            current = current or listener
            with psycopg.connect(listener_url, autocommit=True) as conn:
                current.refresh_workflow(conn)
                current.process_idle_stall_nudges(conn)

        # MEFP's shape: implementers submit straight to Audit, and Audit is serial.
        cfg = json.loads((root / "examples/workflows/inspection.json").read_text())
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            if role["name"] == "audit":
                role["serial"] = True
            if role["name"] == "perf":  # a fourth implementer, as MEFP has many
                role.update(active=True, runtime="claude", target="pgu-perf:0.0")
        next(s for s in cfg["stages"] if s["name"] == "in_progress")["owners"].append("perf")
        for transition in cfg["transitions"]:
            if transition["action"] in {"submit_to_audit", "submit_to_audit_without_commit"}:
                transition["to"] = "audit"
                transition["actors"] = sorted({*transition["actors"], "perf"})
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

        K, A, B, C = "PGU-529", "PGU-592", "PGU-593", "PGU-594"
        X, D = "PGU-665", "PGU-700"
        authors = {K: "app", A: "ops", B: "main", C: "perf"}
        for ticket in (*authors, X, D):  # X stays with the Director: work K waits on
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
        for ticket in authors:
            act(ticket, "route", "director", state="in_progress", assignee=authors[ticket])
        one_pass()
        sent.clear()

        # K: submitted, handed to Audit, kicked back, resubmitted -- then blocked
        # on another ticket's work, in Audit, as MEFP-529 was on MEFP-665.
        act(K, "submit_to_audit_without_commit", "app", reason="finding")
        seen["k_first"] = audit_got(one_pass(), K)
        act(K, "audit_kick_back", "audit", reason="the fixture is not executable")
        one_pass()
        act(K, "submit_to_audit_without_commit", "app", reason="reworked")
        seen["k_resubmitted"] = audit_got(one_pass(), K)
        seen["k_blocked"] = act(K, "set_blockers", "director", blocked_by=[X], blocked_reason="needs X")
        seen["k_dropped"] = [audit_got(one_pass(), K), row(K)]

        # A, then B and C, arrive after it. B and C are held again and again,
        # each hold doubling the delay to the cap.
        act(A, "submit_to_audit_without_commit", "ops", reason="candidate")
        seen["a_first"] = audit_got(one_pass(), A)
        seen["a_first_row"] = row(A)
        act(B, "submit_to_audit_without_commit", "main", reason="candidate")
        act(C, "submit_to_audit_without_commit", "perf", reason="candidate")
        for _ in range(8):
            hold(B, C)
        seen["b_held"] = row(B)
        seen["c_held"] = row(C)

        # Audit reviews A anyway and kicks it back; A is resubmitted as a no-code
        # deferral, and Audit approves that new submission. A leaves Audit, and
        # the Director defers it to the backlog, unassigned.
        act(A, "audit_kick_back", "audit", reason="kick-back on the exact commit")
        one_pass()
        act(A, "submit_to_audit_without_commit", "ops", reason="bounded no-code deferral")
        one_pass()
        seen["a_resubmitted_row"] = row(A)
        seen["a_signoff"] = act(A, "audit_sign_off", "audit", text="No-code deferral accepted.")
        seen["a_defer"] = act(A, "defer", "director")
        seen["a_where"] = where(A)
        one_pass()
        seen["b_before_idle"] = row(B)

        # Audit's pane goes idle. The idle pass releases B, and a restarted
        # listener delivers it on its first pass.
        idle_release()
        seen["b_released"] = row(B)
        prompt = one_pass(new_listener())
        seen["prompt_b"] = audit_got(prompt, B)
        seen["prompt_c"] = audit_got(prompt, C)
        seen["b_after"] = row(B)
        seen["c_after"] = row(C)

        if upgrade_root is not None:
            # The release that was running is upgraded with this tree's
            # migrations. The rows it left stuck are released by the same idle pass.
            upgrade(admin, upgrade_root)
            idle_release()
            seen["upgraded_b_released"] = row(B)
            prompt = one_pass(new_listener())
            seen["upgraded_prompt_b"] = audit_got(prompt, B)
            seen["upgraded_prompt_c"] = audit_got(prompt, C)
            seen["upgraded_c_after"] = row(C)

        later = one_pass() + one_pass()
        seen["later"] = {t: audit_got(later, t) for t in (K, A, B, C)}
        seen["c_still"] = row(C)

        # X is cancelled and K unblocks while Audit has B. K waits for B to
        # leave, then goes before C, which arrived after it.
        gate.idle.pop("audit", None)
        seen["x_cancel"] = act(X, "cancel", "director")
        seen["k_unblocked"] = [audit_got(one_pass(), K), row(K)]
        idle_release()
        seen["k_with_b_current"] = [audit_got(one_pass(), K), row(K)]
        act(B, "audit_sign_off", "audit", text="Audited.")
        gate.idle.pop("audit", None)
        batch = one_pass()
        idle_release()
        batch += one_pass()
        seen["after_b"] = {t: audit_got(batch, t) for t in (K, C)}
        seen["c_behind_k"] = row(C)

        # K leaves. A pane the live check finds busy still holds C; when it
        # clears, C is delivered.
        act(K, "audit_sign_off", "audit", text="Audited.")
        gate.idle.pop("audit", None)
        one_pass()
        gate.busy.add("pgu-audit:0.0")
        idle_release()
        seen["busy_c"] = [audit_got(one_pass(), C), row(C)]
        gate.busy.clear()
        idle_release()
        seen["idle_c"] = audit_got(one_pass(), C)

        # Another role's waiting notice and its deadline are untouched by Audit's idleness.
        gate.busy.add("pgu-ops:0.0")  # ops is free: A, its ticket, is in the backlog
        act(D, "route", "director", state="in_progress", assignee="ops")
        one_pass()
        seen["unrelated_row"] = row(D, "ops")
        idle_release()
        one_pass()
        seen["unrelated_after"] = row(D, "ops")

        # Two tickets never wait on each other. P arrives first but is blocked,
        # so Q is handed over. P unblocks and waits behind Q. Q is then blocked
        # and unblocked before anything else runs, so both are waiting at once:
        # Q, which Audit already has, goes first, and P after it.
        act(C, "audit_sign_off", "audit", text="Audited.")
        for ticket in (K, B, C):  # their authors are free again
            act(ticket, "defer", "director")
        P, Q, Y1, Y2 = "PGU-801", "PGU-802", "PGU-811", "PGU-812"
        for ticket in (P, Q, Y1, Y2):
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
        act(P, "route", "director", state="in_progress", assignee="app")
        act(Q, "route", "director", state="in_progress", assignee="main")
        gate.idle.pop("audit", None)
        one_pass()
        act(P, "submit_to_audit_without_commit", "app", reason="candidate")
        act(P, "set_blockers", "director", blocked_by=[Y1], blocked_reason="needs Y1")
        act(Q, "submit_to_audit_without_commit", "main", reason="candidate")
        # P's notice is still waiting out an earlier delay when Q is handed over,
        # and is dropped afterwards, P being blocked: dropped is not handed over.
        due = "UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() + interval '{}' " \
              f"WHERE ticket_id = '{P}' AND kind = 'transition';"
        sql(due.format("1 hour"))
        batch = one_pass()
        sql(due.format("0 seconds"))
        batch += one_pass()
        seen["q_first"] = {t: audit_got(batch, t) for t in (P, Q)}
        seen["p_dropped"] = row(P)
        act(Y1, "cancel", "director")
        for _ in range(3):
            hold(P)
        seen["p_behind_q"] = row(P)
        act(Q, "set_blockers", "director", blocked_by=[Y2], blocked_reason="needs Y2")
        act(Y2, "cancel", "director")
        idle_release()
        batch = one_pass()
        seen["both_waiting"] = {t: audit_got(batch, t) for t in (P, Q)}
        seen["p_still"] = row(P)
        act(Q, "audit_sign_off", "audit", text="Audited.")
        gate.idle.pop("audit", None)
        one_pass()
        idle_release()
        seen["p_after_q"] = audit_got(one_pass(), P)

        seen["sends"] = {t: sql("SELECT count(*) FROM ticket_board.notification_trace WHERE event = 'send' "
                                f"AND ticket_id = '{t}' AND target_role = 'audit' AND kind = 'transition';")
                         for t in (K, A, B, C, P, Q)}

        # A fresh install of this tree, beside the migrated board: the same
        # function, the same owner and grants.
        source = upgrade_root or root
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
        fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
        install(fresh, source, migrate=False)
        shape = ("SELECT json_build_array(md5(pg_get_functiondef(p.oid)), pg_get_userbyid(p.proowner), "
                 "coalesce(p.proacl::text, ''))::text FROM pg_proc p WHERE p.oid = "
                 "'ticket_board.finish_current_stage_blocker(text, text, text, timestamptz, interval)'::regprocedure;")
        seen["fresh_shape"] = json.loads(sql(shape, fresh))
        seen["migrated_shape"] = json.loads(sql(shape))
        seen["source_definition"] = json.loads(sql(
            "SELECT to_json(md5(pg_get_functiondef('ticket_board.finish_current_stage_blocker(text, text, text, "
            "timestamptz, interval)'::regprocedure)));", fresh))
        privilege = ("SELECT json_build_array(has_function_privilege('ticket_board_listener', '{f}', 'EXECUTE'), "
                     "has_function_privilege('{svc}', '{f}', 'EXECUTE'))::text;").format(
            f="ticket_board.finish_current_stage_blocker(text, text, text, timestamptz, interval)", svc=fixture.SERVICE_ROLE)
        seen["privilege"] = json.loads(sql(privilege))
        server.shutdown()
        server.server_close()
    return seen


def first(rows: list) -> list:
    """A notice's row, or a stand-in that fails every check about a held row instead of crashing."""
    return rows[0] if rows else ["(no row)", -1, -1]


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def child(commit: str, tmp: Path, *extra: str) -> dict:
    tree = tree_at(commit, tmp / commit[:7])
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree), *extra],
                            capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr[-3000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def main() -> int:
    clean_env()
    if len(sys.argv) >= 3 and sys.argv[1] == "--scenario":
        upgrade_root = Path(sys.argv[3]) if len(sys.argv) == 4 else None
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd581b-", upgrade_root)))
        return 0
    held = "finish current"
    with tempfile.TemporaryDirectory(prefix="syrd581.") as tmp:
        before = child(BEFORE, Path(tmp))
        deployed = child(DEPLOYED, Path(tmp), str(ROOT))
    for name, run in (("main", before), ("f92a3d4", deployed)):
        check(run["k_blocked"].get("ok") is not False and run["k_dropped"][0] == [] and run["k_dropped"][1] == []
              and run["a_first"] == [] and first(run["a_first_row"])[0] == held,
              f"{name}: K, blocked in Audit, has nothing queued, and A is held behind it: {run['k_dropped']} {run['a_first_row']}")
        check(first(run["b_held"])[:2] == [held, 8] and first(run["b_held"])[2] >= 250,
              f"{name}: B is held eight times, its deadline doubled to the cap: {run['b_held']}")
        check(run["a_where"] == ["backlog", "unassigned"] and first(run["b_before_idle"])[:2] == [held, 8],
              f"{name}: A was kicked back, resubmitted as no-code and approved, and left Audit: {run['a_where']}")
        check(run["prompt_b"] == [] and first(run["b_released"])[:2] == [held, 8] and first(run["b_after"])[2] >= 250,
              f"{name}: reproduced -- Audit is idle and nothing holds B but the blocked K, and B stays held: "
              f"{run['b_released']} {run['b_after']}")
    check(first(deployed["upgraded_b_released"])[0] == "" and first(deployed["upgraded_b_released"])[1] == 8
          and deployed["upgraded_prompt_b"] == ["PGU-593 -- PGU-593 entered Audit"]
          and deployed["upgraded_prompt_c"] == [] and first(deployed["upgraded_c_after"])[0] == held,
          f"upgraded from f92a3d4, the rows it left stuck are released by Audit's next idle pass: B once, C still behind it: "
          f"{deployed['upgraded_b_released']} {deployed['upgraded_prompt_b']} {deployed['upgraded_c_after']}")

    a = scenario(ROOT, "syrd581a-")
    check(a["k_first"] == ["PGU-529 -- PGU-529 entered Audit"]
          and a["k_resubmitted"] == ["PGU-529 -- PGU-529 is active again in Audit"]
          and a["k_dropped"] == [[], []],
          f"K was handed to Audit, kicked back and resubmitted, then blocked with nothing queued: {a['k_dropped']}")
    check(a["a_first"] == ["PGU-592 -- PGU-592 entered Audit"] and a["a_first_row"] == [],
          f"a blocked ticket holds no review slot: A is handed over when it arrives: {a['a_first']}")
    check(first(a["b_held"])[:2] == [held, 8] and first(a["b_held"])[2] >= 250 and first(a["c_held"])[:2] == [held, 8],
          f"B and C are held behind A, the ticket Audit has: {a['b_held']} {a['c_held']}")
    check(first(a["a_resubmitted_row"])[0] == held and a["a_where"] == ["backlog", "unassigned"]
          and first(a["b_before_idle"])[:2] == [held, 8] and first(a["b_before_idle"])[2] >= 250,
          f"A's resubmission arrived after B, so it waits behind B; Audit approves it anyway and it leaves. "
          f"Until Audit is idle, B keeps its deadline: {a['a_resubmitted_row']} {a['b_before_idle']}")
    check(first(a["b_released"])[0] == "" and first(a["b_released"])[1] == 8 and first(a["b_released"])[2] <= 0,
          f"Audit's idle pass releases B's hold and deadline, keeping its attempt count: {a['b_released']}")
    check(a["prompt_b"] == ["PGU-593 -- PGU-593 entered Audit"] and a["b_after"] == [],
          f"B is delivered on that pass: {a['prompt_b']}")
    check(a["prompt_c"] == [] and first(a["c_after"])[:2] == [held, 8] and first(a["c_after"])[2] >= 250
          and first(a["c_still"])[0] == held,
          f"C, behind B, keeps its hold and its deadline: {a['c_after']} {a['c_still']}")
    check(a["x_cancel"].get("ok") is not False and a["k_unblocked"][0] == [] and first(a["k_unblocked"][1])[0] == held
          and a["k_with_b_current"][0] == [] and first(a["k_with_b_current"][1])[0] == held,
          f"K unblocks while Audit has B: its notice waits behind B, even when Audit's pane is idle: "
          f"{a['k_unblocked']} {a['k_with_b_current']}")
    check(a["after_b"] == {"PGU-529": ["PGU-529 -- PGU-529 is unblocked"], "PGU-594": []} and first(a["c_behind_k"])[0] == held,
          f"B leaves: K, which arrived before C, is delivered, and C waits behind it: {a['after_b']} {a['c_behind_k']}")
    check(a["busy_c"][0] == [] and first(a["busy_c"][1])[0] == "pane busy" and a["idle_c"] == ["PGU-594 -- PGU-594 entered Audit"],
          f"K leaves: a pane the live check finds busy still holds C; when it clears, C is delivered: {a['busy_c']} {a['idle_c']}")
    check(a["later"] == {"PGU-529": [], "PGU-592": [], "PGU-593": [], "PGU-594": []}
          and a["sends"] == {"PGU-529": "3", "PGU-592": "1", "PGU-593": "1", "PGU-594": "1", "PGU-801": "1", "PGU-802": "2"},
          f"each handover is delivered once (K: submission, resubmission, unblock; Q: submission, unblock): {a['sends']}")
    check(a["q_first"] == {"PGU-801": [], "PGU-802": ["PGU-802 -- PGU-802 entered Audit"]} and a["p_dropped"] == []
          and first(a["p_behind_q"])[0] == held,
          f"P, blocked, is passed over and its notice dropped after Q is handed over; P, unblocked, waits behind Q: "
          f"{a['q_first']} {a['p_dropped']} {a['p_behind_q']}")
    check(a["both_waiting"] == {"PGU-801": [], "PGU-802": ["PGU-802 -- PGU-802 is unblocked"]} and first(a["p_still"])[0] == held
          and a["p_after_q"] == ["PGU-801 -- PGU-801 entered Audit"],  # never handed over before
          f"both waiting at once: Q, which Audit already had, goes first, then P -- neither waits on the other: "
          f"{a['both_waiting']} {a['p_still']} {a['p_after_q']}")
    check(a["unrelated_row"] == a["unrelated_after"] and first(a["unrelated_row"])[0] == "pane busy",
          f"another role's waiting notice and its deadline are untouched: {a['unrelated_after']}")
    check(a["migrated_shape"] == a["fresh_shape"] and a["privilege"] == [True, False]
          and a["source_definition"] == deployed["source_definition"],
          f"schema.sql and the migrations install the same function, owner and grants: {a['migrated_shape']} {a['fresh_shape']}")
    # Parity last, so a behaviour fault is reported by the behaviour it breaks.
    check(deployed["migrated_shape"] == deployed["fresh_shape"] and deployed["privilege"] == [True, False],
          f"the upgraded board's function, owner and grants are a fresh install's: {deployed['migrated_shape']} "
          f"{deployed['fresh_shape']} {deployed['privilege']}")
    print(f"blocked_review_slot_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
