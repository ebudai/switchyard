#!/usr/bin/env python3
"""SYRD-688: work admitted after a launch that left the pull workers stopped still gets exactly one of them.

The real listener (`listen_once`) on a disposable board built through
schema.sql and rbac.sql, as `pull_pickup_test` runs it. When ready work is
left that no running worker took, its pass asks the project's admission
(`worker_admission.py admit --mode demand`). Here that child is run
in-process, for real, against this board's own HTTP reads, with the project
account's tmux server stood in for: a started worker registers as a live,
idle provider, exactly as a real start leaves it. Then the next pass claims.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import ticket_board_write_api_test as fixture  # noqa: E402
from pull_claim_postgres_test import build  # noqa: E402
from pull_pickup_test import TARGETS, Gate, proc_entry  # noqa: E402
from pull_scheduling_policy_test import pull_document  # noqa: E402
from scripts import worker_admission as wa  # noqa: E402
from scripts.role_context_recovery import board_get  # noqa: E402
from scripts.ticket_board import demand_start  # noqa: E402
from scripts.ticket_board.notify_listener import PaneHookStateStore, TicketBoardNotifyListener  # noqa: E402
from scripts.ticket_board.peer_identity import SessionIdentity  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Finished:
    """A child that has already run: what `admit` printed, as the listener reads it back."""

    def __init__(self, answer: dict, stdout=None) -> None:
        self.returncode = 0
        out = stdout or tempfile.TemporaryFile("w+")
        out.write(json.dumps(answer) + "\n")  # into the file the listener gave it, as the real child prints
        self.outputs = (out, tempfile.TemporaryFile("w+"))

    def poll(self):
        return 0

    def wait(self):
        return 0


class Running(Finished):
    def poll(self):
        return None


def test_ready_work_after_launch_starts_and_claims_exactly_one_worker() -> None:
    with temporary_cluster(prefix="syrd688-demand.", shutdown="immediate") as cluster:
        admin = build(cluster, "demand", "fresh")
        tmp = Path(tempfile.mkdtemp(prefix="syrd688-demand."))
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "demand", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            def sql(statement: str) -> str:
                return fixture.psql(admin, statement).strip()

            def where(ticket: str) -> str:
                return sql(f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id='{ticket}';")

            def admit_ticket(ticket: str) -> None:
                fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director",
                                             commit_exempt=True)
                fixture.post_json(base, f"/api/tickets/{ticket}/actions/admit", {}, caller="director")

            cfg = pull_document()
            cfg["project"] = "pgu"
            for role in cfg["roles"]:
                if role.get("target"):
                    role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

            # The launcher configuration the registry names: the three pull workers, detached and ephemeral.
            config_path = tmp / "pgu.json"
            config_path.write_text(json.dumps({
                "project": "pgu", "run_as_user": os.environ.get("USER") or __import__("pwd").getpwuid(os.getuid()).pw_name,
                "layout": str(tmp / "layout.json"), "board_url": base, "session_dir": str(tmp / "sessions"),
                "roles": [{"role": name, "detached": True, "ephemeral": True, "cli": ["claude"],
                           "target": TARGETS[name], "tmux_session": TARGETS[name].split(":")[0],
                           "workdir": str(tmp / "work" / name)} for name in ("main", "app", "ops")]}))
            (tmp / "registry").mkdir()
            (tmp / "registry" / "pgu.json").write_text(json.dumps({"slug": "pgu", "config_path": str(config_path)}))

            proc = tmp / "proc"
            store = PaneHookStateStore(tmp / "state")
            gate = Gate(state_store=store)
            gate.proc_root, gate.role_targets, gate.busy, gate.role_identities = proc, dict(TARGETS), set(), {}
            started: list[str] = []
            pids = iter(range(4200, 4300))

            def tmux(args, **kwargs):
                """The project account's tmux server: a started worker is a live, registered, idle provider."""
                args = list(args)
                if "has-session" in args:
                    session = args[args.index("-t") + 1].lstrip("=")
                    return subprocess.CompletedProcess(args, 0 if any(TARGETS[r].startswith(session) for r in started) else 1)
                role = args[args.index("attach-or-start") + 1]
                started.append(role)
                pid = next(pids)
                proc_entry(proc, pid, pid + 1000)
                gate.role_identities[role] = SessionIdentity(pid, pid + 1000)
                store.write(TARGETS[role], "idle", source="claude.Stop")
                return subprocess.CompletedProcess(args, 0)

            def observe(config, member, *, runner=None, watch=False):
                return ("running", "live") if member in started else ("stopped", "no session")

            asks: list[list[str]] = []

            def spawn(argv, **kwargs):
                """The admission child, run here for real against this board."""
                asks.append(list(argv))
                busy = argv[argv.index("--busy") + 1:] if "--busy" in argv else []
                with patch("scripts.worker_pool.observe_worker", observe):
                    answer = wa.admit("pgu", Path(argv[argv.index("--config") + 1]), mode="demand",
                                      runner=tmux, getter=board_get, busy={role: "busy" for role in busy})
                return Finished(answer, kwargs["stdout"])

            clock = [1000.0]
            log = io.StringIO()
            logger = logging.getLogger("syrd688")
            logger.addHandler(logging.StreamHandler(log))
            logger.setLevel(logging.INFO)
            listener = TicketBoardNotifyListener(
                conninfo=fixture.conninfo(cluster.socket_dir, cluster.port, "demand", "ticket_board_listener"),
                sender=lambda target, message: None, activity_gate=gate.is_working, target_exists=lambda _t: True,
                submission_witness=lambda *_a: True, poll_seconds=0, logger=logger)
            listener.role_targets = dict(TARGETS)
            environ = {"TICKET_BOARD_PROCESS_AUTHORITY": "1", "SWITCHYARD_PROJECT_REGISTRY_DIR": str(tmp / "registry")}
            real_trigger = demand_start.trigger

            def trigger(listener_, conn, eligibility, not_running, **kwargs):
                return real_trigger(listener_, conn, eligibility, not_running, environ=environ, spawn=spawn,
                                    clock=lambda: clock[0])

            with patch.object(demand_start, "trigger", trigger):
                # Launched with no work: every pull worker stopped, and nothing asks for one.
                listener.listen_once(max_notifications=5)
                check(asks == [] and started == [], f"no ready work: no worker is asked for: {asks}")

                # One ticket admitted after the launch.
                admit_ticket("PGU-1")
                listener.listen_once(max_notifications=5)
                check(len(asks) == 1 and asks[0][1].endswith("scripts/worker_admission.py")
                      and asks[0][2:] == ["admit", "--mode", "demand", "pgu", "--config", str(config_path), "--busy"],
                      f"one ask, of this project's own admission, as this account: {asks}")
                check(started == ["app"], f"exactly one worker started, the first declared claimant: {started}")
                check(where("PGU-1") == "ready/unassigned", "the pass that started it did not claim blind")
                clock[0] += 5
                listener.listen_once(max_notifications=5)
                check(where("PGU-1") == "in_progress/app" and started == ["app"] and len(asks) == 1,
                      f"the next pass: the started worker claims it; nothing else starts: {where('PGU-1')} {started} {asks}")
                check("Demand start: started app for ready work" in log.getvalue(),
                      f"the listener says what it started: {log.getvalue()[-400:]}")

                # Two more tickets while app works: two more workers, once each, never more.
                gate.busy.add(TARGETS["app"])
                store.write(TARGETS["app"], "busy", source="claude.UserPromptSubmit")
                admit_ticket("PGU-2")
                admit_ticket("PGU-3")
                clock[0] += demand_start.MIN_INTERVAL_SECONDS
                listener.listen_once(max_notifications=5)
                check(sorted(started) == ["app", "main", "ops"] and len(asks) == 2 and asks[1][-2:] == ["--busy", "app"],
                      f"two ready tickets, one worker busy, and the ask says so: the two stopped workers start: {started} {asks[1][-2:]}")
                clock[0] += 5
                listener.listen_once(max_notifications=5)
                check(where("PGU-2").startswith("in_progress/") and where("PGU-3").startswith("in_progress/")
                      and {where("PGU-2"), where("PGU-3")} == {"in_progress/main", "in_progress/ops"},
                      f"and they claim them: {where('PGU-2')} {where('PGU-3')}")

                # More work than workers: the pool is the bound, and the asks are spaced.
                for role in ("main", "ops"):
                    gate.busy.add(TARGETS[role])
                admit_ticket("PGU-4")
                clock[0] += demand_start.MIN_INTERVAL_SECONDS
                listener.listen_once(max_notifications=5)
                check(sorted(started) == ["app", "main", "ops"] and len(asks) == 3,
                      f"every declared worker is running: asking starts nothing more: {started}")
                listener.listen_once(max_notifications=5)
                check(len(asks) == 3, "and the next pass, within the interval, does not ask again")
                check("no worker could be started" in log.getvalue(), "the listener says why nothing started")

                # One ask at a time: while a child is still deciding, no second one.
                listener._demand_child = Running({"decisions": [], "reason": ""})
                clock[0] += demand_start.MIN_INTERVAL_SECONDS
                listener.listen_once(max_notifications=5)
                check(len(asks) == 3, "a second ask never overlaps the first")
                listener._demand_child = None

                # A worker that dies holding its ticket: the listener asks, and the child recovers it.
                gate.role_identities.pop("main")
                started.remove("main")
                clock[0] += demand_start.MIN_INTERVAL_SECONDS
                listener.listen_once(max_notifications=5)
                check(len(asks) == 4 and started.count("main") == 1
                      and "in_progress/main" in {where("PGU-2"), where("PGU-3")},
                      f"main holds its ticket and is not running: asked, and recovered: {started} {asks[-1][-3:]}")
                check(any("holds PGU-" in line and "is not running" in line for line in log.getvalue().splitlines()),
                      "the listener says why it asked")

                # Not the project account: never asks.
                environ["TICKET_BOARD_PROCESS_AUTHORITY"] = "0"
                gate.role_identities.pop("ops")
                started.remove("ops")
                clock[0] += demand_start.MIN_INTERVAL_SECONDS
                listener.listen_once(max_notifications=5)
                check(len(asks) == 4, "a listener without process authority asks for nothing, not even for a dead holder")
        finally:
            server.shutdown()
            server.server_close()
            shutil.rmtree(tmp, ignore_errors=True)


def test_what_is_left_unmet() -> None:
    gone = frozenset({"no registered provider process", "registered provider process is gone"})
    check(demand_start.unmet([], {}, gone) == "", "nothing ready, nothing unmet")
    check(demand_start.unmet([{"id": "X-1", "claimable": False}], {}, gone) == "", "unclaimable work asks for nobody")
    check("X-2 is ready" in demand_start.unmet([{"id": "X-2", "claimable": True}], {}, gone), "ready unpinned work is unmet")
    check("waits for luna-3, which is not running" in demand_start.unmet(
        [{"id": "X-3", "claimable": True, "waiting_for": "luna-3"}], {"luna-3": "no registered provider process"}, gone),
          "rework pinned to a stopped author is unmet")
    check(demand_start.unmet([{"id": "X-4", "claimable": True, "waiting_for": "luna-3"}], {"luna-3": "working"}, gone) == "",
          "rework pinned to a running author waits for it")
    check(demand_start.unmet([{"id": "X-5", "claimable": True, "waiting_for": "luna-3"}], {}, gone) == "",
          "rework pinned to a held author (not in the pass's eligibility) asks for nobody")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"demand_start_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
