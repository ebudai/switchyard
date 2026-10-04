#!/usr/bin/env python3
"""SYRD-542: Audit's next ticket waited about five minutes after the previous review left it.

Live on MEFP: serial review admission held MEFP-570's Audit notice ("finish
current") behind MEFP-569 eight times, each requeue doubling its delay to the
300-second cap. Audit then finished 569 and its pane went idle -- yet 570 was
not delivered until its accumulated deadline came round, about five minutes
later. Rows held behind 570 (571..580) were held again, correctly.

The listener already re-arms a role's waiting rows when that role's trusted
idle evidence arrives, but only rows waiting for a busy pane. A row held only
because another ticket was current is now re-armed the same way once that hold
no longer applies -- and only then: rows still behind a current ticket keep
their deadline, a busy pane still holds, and nothing is delivered twice.

Production-built board (companion roles, schema.sql, the real
ticket-board-migrate, rbac.sql, a declared workflow with Audit serial); the
real notify listener with an injected sender, an activity gate whose idle
evidence the scenario controls, and its real requeue backoff. The before-run
is main's code and SQL, in a child from a git archive.
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
BEFORE = "e69805642efb27e61586cf3d3a221bfc6aeda5a4"  # main before SYRD-542
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


def scenario(root: Path, prefix: str) -> dict:
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
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        fixture.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        fixture.create_roles(admin)
        fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        result = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
        assert result.returncode == 0, result.stderr[-1500:]
        fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")

        def act(ticket: str, operation: str, actor: str, **payload):
            return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        def row(ticket: str, role: str) -> list:
            raw = sql("SELECT coalesce(json_agg(json_build_array(coalesce(last_error, ''), attempts, "
                      "extract(epoch FROM next_attempt_at - clock_timestamp())::int))::text, '[]') "
                      f"FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}' "
                      f"AND kind = 'transition' AND target_role = '{role}';")
            return json.loads(raw)

        sent: list[tuple[str, str]] = []
        gate = Gate()

        def new_listener():
            return TicketBoardNotifyListener(conninfo=listener_url, sender=lambda target, message: sent.append((target, message)),
                                             activity_gate=gate.check, target_exists=lambda _target: True,
                                             submission_witness=lambda target, _since: any(t == target for t, _m in sent),
                                             poll_seconds=0, project="pgu")

        listener = new_listener()

        import psycopg
        try:
            from scripts.ticket_board import pull_pickup
        except ImportError:  # a baseline older than SYRD-539
            pull_pickup = None

        def one_pass(listener=listener) -> list[str]:
            """One iteration of the listener's loop, as listen_once runs it, without its wait."""
            before = len(sent)
            with psycopg.connect(listener_url, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                for step in ("process_reminder_snooze_due", "process_idle_turn_end_nudges", "process_idle_stall_nudges",
                             "process_serial_focus_queue_wakeups"):
                    if hasattr(listener, step):  # a baseline older than a pass has no such pass
                        getattr(listener, step)(conn)
                if pull_pickup is not None:
                    pull_pickup.run(listener, conn)
                listener.process_due_notifications(conn)
            return [message for _target, message in sent[before:]]

        cfg = json.loads((root / "examples/workflows/inspection.json").read_text())
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            if role["name"] == "audit":
                role["serial"] = True
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

        A, B, C, D = "PGU-569", "PGU-570", "PGU-571", "PGU-590"
        for ticket in (A, B, C):
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="inspection", assignee="inspector",
                                         commit_exempt=True)
        fixture.seed_postgres_ticket(admin, D, title=D, state="analysis", assignee="director", commit_exempt=True)
        one_pass()
        act(A, "inspector_sign_off", "inspector")
        seen["a_delivered"] = any(f"{A} entered Audit" in m for m in one_pass())
        act(B, "inspector_sign_off", "inspector")
        act(C, "inspector_sign_off", "inspector")
        # B (and C behind it) is held while Audit has A, again and again: each hold
        # requeues with the listener's own doubling delay, as on MEFP.
        for _ in range(8):
            sql(f"UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                f"WHERE ticket_id IN ('{B}', '{C}') AND kind = 'transition';")
            one_pass()
        seen["b_held"] = row(B, "audit")
        seen["c_held"] = row(C, "audit")

        # A leaves Audit, and Audit's pane goes idle -- trusted idle evidence.
        act(A, "audit_sign_off", "audit", text="Audited.")
        seen["a_to_next"] = [m for m in one_pass() if A in m]
        gate.idle["audit"] = datetime.now(timezone.utc).isoformat()
        # The idle pass releases B; then this listener stops, and a restarted one
        # delivers it -- the release is durable, not held in a process.
        with psycopg.connect(listener_url, autocommit=True) as conn:
            listener.refresh_workflow(conn)
            listener.process_idle_stall_nudges(conn)
        seen["b_released"] = row(B, "audit")
        prompt = one_pass(new_listener())
        seen["prompt_b"] = [m for m in prompt if f"{B} entered Audit" in m]
        seen["prompt_c"] = [m for m in prompt if f"{C} entered Audit" in m]
        seen["b_after"] = row(B, "audit")
        seen["c_after"] = row(C, "audit")
        # Later passes deliver nothing twice, and C stays behind B.
        later = one_pass() + one_pass()
        seen["later_b"] = [m for m in later if f"{B} entered Audit" in m]
        seen["later_c"] = [m for m in later if f"{C} entered Audit" in m]
        seen["c_still"] = row(C, "audit")

        # B leaves too. Without idle evidence, C keeps its deadline: nothing says
        # Audit is free. With idle evidence but a pane the live check finds busy
        # (someone typing), C is held as before. When it clears, C is delivered.
        gate.idle.pop("audit", None)
        gate.idle["app"] = datetime.now(timezone.utc).isoformat()  # another role's evidence is not Audit's
        act(B, "audit_sign_off", "audit", text="Audited.")
        seen["no_evidence_c"] = [m for m in one_pass() if f"{C} entered Audit" in m]
        seen["no_evidence_row"] = row(C, "audit")
        gate.idle.pop("app", None)
        gate.busy.add("pgu-audit:0.0")
        gate.idle["audit"] = datetime.now(timezone.utc).isoformat()
        seen["busy_c"] = [m for m in one_pass() if f"{C} entered Audit" in m]
        seen["busy_row"] = row(C, "audit")
        gate.busy.clear()
        seen["idle_c"] = [m for m in one_pass() if f"{C} entered Audit" in m]

        # An unrelated notice and its deadline are untouched by Audit's idleness.
        gate.busy.add("pgu-app:0.0")
        act(D, "route", "director", state="in_progress", assignee="app")
        one_pass()
        seen["unrelated_row"] = row(D, "app")
        gate.idle["audit"] = datetime.now(timezone.utc).isoformat()
        one_pass()
        seen["unrelated_after"] = row(D, "app")
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
        fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
        fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
        definition = ("SELECT md5(pg_get_functiondef('ticket_board.reset_notification_backoff_for_idle_roles(jsonb, timestamptz)'"
                      "::regprocedure));")
        seen["parity"] = fixture.psql(fresh, definition).strip() == sql(definition)
        seen["sends"] = {t: sql("SELECT count(*) FROM ticket_board.notification_trace WHERE event = 'send' "
                                f"AND ticket_id = '{t}' AND target_role = 'audit' AND kind = 'transition';")
                         for t in (A, B, C)}
        server.shutdown()
        server.server_close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd542b-")))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd542.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    check(before["b_held"][0][:2] == ["finish current", 8] and before["b_held"][0][2] >= 250,
          f"before: B was held eight times, its deadline doubled to the cap: {before['b_held']}")
    check(before["prompt_b"] == [] and before["b_after"][0][:2] == ["finish current", 8] and before["b_after"][0][2] >= 250,
          f"reproduced: A left Audit and Audit went idle, and B still waited out its deadline: {before['b_after']}")

    a = scenario(ROOT, "syrd542a-")
    check(a["a_delivered"] and a["b_held"][0][:2] == ["finish current", 8] and a["b_held"][0][2] >= 250,
          f"the same holds accumulate: {a['b_held']}")
    check(a["b_released"][0][0] == "" and a["b_released"][0][2] <= 0 and a["b_released"][0][1] == 8,
          f"Audit's idle pass releases B's hold and deadline, keeping its attempt count: {a['b_released']}")
    check(a["prompt_b"] == ["PGU-570 -- PGU-570 entered Audit"] and a["b_after"] == [],
          f"once A has left and Audit's idle evidence arrives, B is delivered on that pass: {a['prompt_b']}")
    check(a["prompt_c"] == [] and a["c_after"][0][:2] == ["finish current", 8] and a["c_after"][0][2] >= 250
          and a["later_c"] == [] and a["c_still"][0][0] == "finish current",
          f"C, behind B, selected but not yet delivered, keeps its hold and its deadline: {a['c_after']} {a['c_still']}")
    check(a["later_b"] == [] and a["sends"] == {"PGU-569": "1", "PGU-570": "1", "PGU-571": "1"},
          f"each assignment is delivered once: {a['sends']}")
    check(a["no_evidence_c"] == [] and a["no_evidence_row"][0][0] == "finish current" and a["no_evidence_row"][0][2] >= 250,
          f"without Audit's own idle evidence nothing of Audit's is released early (App's idleness is not Audit's): "
          f"{a['no_evidence_row']}")
    check(a["busy_c"] == [] and a["busy_row"][0][0] == "pane busy" and a["idle_c"] == ["PGU-571 -- PGU-571 entered Audit"],
          f"a pane the live check finds busy still holds it; when it clears, it is delivered: {a['busy_row']} {a['idle_c']}")
    check(a["parity"], "a fresh schema.sql board installs the same function as the migrated one")
    check(a["unrelated_row"] == a["unrelated_after"] and a["unrelated_row"][0][0] == "pane busy",
          f"another role's waiting notice and its deadline are untouched: {a['unrelated_after']}")
    print(f"serial_handoff_latency_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
