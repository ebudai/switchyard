#!/usr/bin/env python3
"""SYRD-539: the listener claims admitted work for eligible idle implementers, and only for them.

The real listener (`listen_once`) on a disposable board built through
schema.sql and rbac.sql, delivering through a recording sender. The gate is a
real PaneActivityGate reading real hook-state files and a synthetic /proc for
the registered provider processes; only its pane probes are stood in for (a
`busy` set), since there is no tmux here.
"""

from __future__ import annotations

import json
import logging
import shutil
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import ticket_board_write_api_test as fixture  # noqa: E402
from pull_claim_postgres_test import build  # noqa: E402
from pull_scheduling_policy_test import pull_document  # noqa: E402
from scripts.ticket_board.notify_listener import PaneActivityGate, PaneHookStateStore, TicketBoardNotifyListener  # noqa: E402
from scripts.ticket_board.peer_identity import SessionIdentity  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
TARGETS = {role: f"pgu-{role}:0.0" for role in ("main", "app", "ops", "director", "audit", "inspector", "user")}


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Gate(PaneActivityGate):
    """A real gate whose pane probes answer from `busy`."""

    busy: set = set()

    def pre_send_busy(self, target: str) -> bool:
        return target in self.busy

    def is_working(self, target: str) -> bool:
        return target in self.busy


def proc_entry(root: Path, pid: int, start: int) -> None:
    (root / str(pid)).mkdir(parents=True, exist_ok=True)
    (root / str(pid) / "stat").write_text(f"{pid} (claude) S 1 " + " ".join(["0"] * 17) + f" {start} 0 0\n")


def test_the_listener_claims_only_for_eligible_idle_workers() -> None:
    with temporary_cluster(prefix="syrd539-pickup.", shutdown="immediate") as cluster:
        admin = build(cluster, "pickup", "fresh")
        tmp = Path(tempfile.mkdtemp(prefix="syrd539-pickup."))
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "pickup", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            def sql(statement: str) -> str:
                return fixture.psql(admin, statement).strip()

            def act(ticket: str, operation: str, actor: str, **payload):
                return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)

            def where(ticket: str) -> str:
                return sql(f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id='{ticket}';")

            cfg = pull_document()
            cfg["project"] = "pgu"
            for role in cfg["roles"]:
                if role.get("target"):
                    role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
            for ticket in ("PGU-1", "PGU-2"):
                fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
                act(ticket, "admit", "director")

            proc = tmp / "proc"
            store = PaneHookStateStore(tmp / "state")
            gate = Gate(state_store=store)
            gate.proc_root = proc
            gate.role_targets = dict(TARGETS)
            gate.busy = set()
            # main: registered, live, idle -- eligible. app: registered and live, no hook state.
            # ops: registered and live, stopped on a prompt.
            for n, role in enumerate(("main", "app", "ops"), start=1):
                proc_entry(proc, 4100 + n, 5000 + n)
            gate.role_identities = {role: SessionIdentity(4100 + n, 5000 + n) for n, role in enumerate(("main", "app", "ops"), start=1)}
            store.write(TARGETS["main"], "idle", source="claude.Stop")
            store.write(TARGETS["ops"], "blocked", source="claude.Notification.permission_prompt")
            sent: list = []
            listener = TicketBoardNotifyListener(
                conninfo=fixture.conninfo(cluster.socket_dir, cluster.port, "pickup", "ticket_board_listener"),
                sender=lambda target, message: sent.append((target, message)),
                activity_gate=gate.is_working, target_exists=lambda _t: True, submission_witness=lambda *_a: True,
                poll_seconds=0, logger=logging.getLogger("syrd539"))
            listener.role_targets = dict(TARGETS)
            listener.listen_once(max_notifications=5)
            check(where("PGU-1") == "in_progress/main" and where("PGU-2") == "ready/unassigned",
                  f"only the eligible idle worker got work: PGU-1 {where('PGU-1')}, PGU-2 {where('PGU-2')}")
            check(sql("SELECT string_agg(role || ':' || via, ',') FROM ticket_board.pull_claims;") == "main:listener",
                  "and the claim is recorded as the listener's, for main")
            check(any(target == TARGETS["main"] and "PGU-1" in message for target, message in sent),
                  f"main is told of its new ticket by the ordinary assignment notice: {sent}")
            check(not any(target == TARGETS["director"] for target, _ in sent),
                  f"a successful pickup tells the Director nothing: {sent}")

            # Every ineligibility is a reason, and none of them claims.
            for label, setup, reason in (
                ("no hook state", lambda: None, "readiness unknown"),
                ("a prompt", lambda: None, "stopped on a prompt"),
                ("a busy pane", lambda: (store.write(TARGETS["app"], "idle", source="claude.Stop"), gate.busy.add(TARGETS["app"])), "working"),
                ("a dead provider", lambda: (gate.busy.clear(), shutil.rmtree(proc / "4102")), "registered provider process is gone"),
                ("no registration", lambda: gate.role_identities.pop("app"), "no registered provider process"),
            ):
                setup()
                listener.listen_once(max_notifications=5)
                check(where("PGU-2") == "ready/unassigned", f"{label}: nothing is claimed for app or ops: {where('PGU-2')}")

            # Idle by its hook, but its turn ended on running background work (SYRD-538): not eligible.
            ops_state = tmp / "state" / "pgu-ops_0.0.json"
            ops_state.write_text(json.dumps({
                "target": TARGETS["ops"], "state": "idle", "source": "claude.Stop", "updated_at": __import__("time").time(),
                "background_work": {"pane_pid": 4103, "pane_start_time": 5003, "session_id": "s",
                                    "tasks": [{"id": "a1", "type": "subagent", "status": "running"}]}}))
            listener.listen_once(max_notifications=5)
            check(where("PGU-2") == "ready/unassigned",
                  f"background work: nothing is claimed for a worker whose subagent is still running: {where('PGU-2')}")
            store.write(TARGETS["ops"], "blocked", source="claude.Notification.permission_prompt")

            # The idle alert: once the waiting work is older than the interval, ONE Director notice, with reasons.
            sql("UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = clock_timestamp() - interval '20 minutes' "
                "WHERE ticket_id='PGU-2';")
            sent.clear()
            listener.listen_once(max_notifications=5)
            listener.listen_once(max_notifications=5)
            alerts = [m for t, m in sent if t == TARGETS["director"]]
            # app has no registered provider process: it is not running, so it is not idle capacity (SYRD-573).
            check(len(alerts) == 1 and "PGU-2" in alerts[0] and "ops (stopped on a prompt)" in alerts[0]
                  and "app" not in alerts[0].split("claiming it:")[1],
                  f"one alert, naming each idle running worker's reason, and no stopped one: {alerts}; traced: "
                  + sql("SELECT coalesce(string_agg(event || ':' || coalesce(busy_reason,'') || ':' || left(detail::text, 160), ' | ' ORDER BY id), 'none') "
                        "FROM ticket_board.notification_trace WHERE ticket_id='PGU-2' AND kind='ticket_update';"))
            check(sql("SELECT count(*) FROM ticket_board.notification_trace WHERE kind='ticket_update' AND event='enqueue' "
                      "AND detail->>'dedupe_key' LIKE 'pull-idle:PGU-2:%';") == "1", "and it is enqueued once, not per pass")

            # Eligible again: the waiting ticket is claimed, and the alert does not recur.
            gate.role_identities["app"] = SessionIdentity(4102, 5002)
            proc_entry(proc, 4102, 5002)
            sent.clear()
            listener.listen_once(max_notifications=5)
            check(where("PGU-2") == "in_progress/app" and not any(t == TARGETS["director"] for t, _ in sent),
                  f"recovery is quiet: app claims PGU-2 and the Director hears nothing more: {where('PGU-2')} {sent}")

            # No policy: the same listener claims nothing.
            doc = json.loads(sql("SELECT document::text FROM ticket_board.workflow_configuration;"))
            rev = int(sql("SELECT revision FROM ticket_board.workflow_configuration;"))
            doc.pop("scheduling")
            doc["transitions"] = [t for t in doc["transitions"] if t["action"] != "claim"]
            app.apply_workflow(doc, expected_revision=rev, dry_run=False, caller_role="director")
            fixture.seed_postgres_ticket(admin, "PGU-3", title="PGU-3", state="analysis", assignee="director", commit_exempt=True)
            act("PGU-3", "admit", "director")
            before = sql("SELECT count(*) FROM ticket_board.pull_claims;")
            listener.listen_once(max_notifications=5)
            check(sql("SELECT count(*) FROM ticket_board.pull_claims;") == before and not where("PGU-3").startswith("in_progress"),
                  f"a workflow without scheduling is never pulled from: {where('PGU-3')}")
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"pull_pickup_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
