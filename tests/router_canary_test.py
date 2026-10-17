#!/usr/bin/env python3
"""SYRD-599: the Router canary seeds bounded fixtures, prices what Router did, and cleans up.

On a production-built Router board with the real HTTP server and listener,
`scripts/router_canary.py` runs as the Director would run it. A stand-in Router
-- a keyword classifier where the low-cost model would be, so no provider is
called -- acts only when the listener hands it a notice: one
`ticket-board-read ticket <id> --json`, then one `router-assign` or
`router-return`, through the real CLI. It misroutes one fixture on purpose, and
that implementer hands it back with `implementer-kick-back`, so the report has
an interruption and a misroute to price. Then the canary finishes, and nothing
it made is left open.

The live run, with a real low-cost model in the tenant's Router pane, is the
same three commands; it belongs to UAT, since it spends provider quota.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import router_role_board_test as rb  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CANARY = ROOT / "scripts" / "router_canary.py"
CHECKS = 0
#: The stand-in model. It misroutes the log rotation fixture to main on purpose.
RULES = (("specification", "return"), ("decide", "return"), ("feels slow", "return"), ("web UI", "app"),
         ("browser UI", "app"), ("systemd", "ops"), ("rotation", "main"), ("core", "main"), ("merge", "main"))


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def classify(ticket: dict) -> str:
    text = f"{ticket['title']} {ticket['body']}"
    return next((owner for word, owner in RULES if word in text), "return")


def scenario() -> dict:
    for key in [k for k in os.environ if k.startswith(rb.PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    seen: dict = {}
    with temporary_cluster(prefix="syrd599k-", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd599-canary.") as scratch:
        board = rb.Board(cluster, "canary", rb.example(router=True))
        try:
            run_file = Path(scratch) / "run.json"
            env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "TICKET_BOARD_URL": board.base,
                   "TICKET_BOARD_WRITE_TOKEN": board.server.write_token}

            def canary(*args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run([sys.executable, str(CANARY), *args, "--run-file", str(run_file)],
                                      capture_output=True, text=True, timeout=120, env=env, stdin=subprocess.DEVNULL)

            def cli(role: str, *args: str) -> subprocess.CompletedProcess[str]:
                return subprocess.run([str(ROOT / "scripts" / args[0]), *args[1:]], capture_output=True, text=True,
                                      timeout=60, env={**env, "TICKET_BOARD_CALLER_ROLE": role}, stdin=subprocess.DEVNULL)

            seeded = canary("seed")
            seen["seed"] = [seeded.returncode, seeded.stderr[-400:]]
            run = json.loads(run_file.read_text())
            seen["run"] = run
            seen["held_by_router"] = {t: board.where(t) for t in run["tickets"]}
            seen["leak"] = [t for t in run["tickets"]
                            if run["tickets"][t]["expect"] in json.dumps(board.ticket(t).get("body", ""))]
            seen["second_seed"] = canary("seed").returncode

            reads, writes, notices = [], [], []
            for _ in range(len(run["tickets"]) + 3):  # one Router turn per pass; spare passes must be idle
                delivered = rb.to(board.one_pass(), "router")
                notices.extend(delivered)
                for message in delivered:
                    ticket_id = message.split(" ")[0]
                    read = cli("router", "ticket-board-read", "ticket", ticket_id, "--json")
                    reads.append(ticket_id)
                    ticket = json.loads(read.stdout)
                    choice, revision = classify(ticket), ticket["routing"]["revision"]
                    if choice == "return":
                        done = cli("router", "ticket-board-write", "router-return", ticket_id, "--reason",
                                   "Not implementation work yet, or not clearly one owner's.", "--revision", revision)
                    else:
                        done = cli("router", "ticket-board-write", "router-assign", ticket_id, "--assignee", choice,
                                   "--revision", revision)
                    writes.append([ticket_id, choice, done.returncode, done.stderr[-200:]])
                board.busy.clear()
                board.sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                          "WHERE target_role = 'router' AND dead_lettered_at IS NULL;")
            seen.update(reads=reads, writes=writes, notices=notices)

            misrouted = next(t for t, w in run["tickets"].items() if w["title"].startswith("Listener logs"))
            kicked = cli("main", "ticket-board-write", "implementer-kick-back", misrouted, "--reason",
                         "Log rotation is ops's deployment work, not main's.")
            seen["kick_back"] = [kicked.returncode, kicked.stderr[-300:], board.where(misrouted)]

            reported = canary("report", "--router-turn", "1", "--director-turn", "4", "--interruption", "8")
            seen["report"] = json.loads(reported.stdout) if reported.returncode == 0 else reported.stderr[-600:]
            finished = canary("finish")
            seen["finish"] = json.loads(finished.stdout) if finished.returncode == 0 else finished.stderr[-600:]
            seen["after_finish"] = {t: board.where(t)[0] for t in run["tickets"]}
            seen["finish_again"] = json.loads(canary("finish").stdout)
        finally:
            board.close()
    return seen


def verify(seen: dict) -> None:
    run = seen["run"]
    check(seen["seed"][0] == 0, f"seed runs as the Director: {seen['seed']}")
    check(len(run["tickets"]) == 9 and run["owners"] == ["app", "main", "ops"] and run["skipped"] == [],
          f"every bounded fixture is seeded for this project's owners: {run}")
    check(all(w == ["in_progress", "router"] for w in seen["held_by_router"].values()),
          f"and routed to Router: {seen['held_by_router']}")
    check(seen["leak"] == [] and seen["second_seed"] != 0,
          "the expectation is never on the ticket, and a recorded run is not seeded twice")
    check(sorted(seen["reads"]) == sorted(run["tickets"]) and len(seen["notices"]) == 9,
          f"Router read each fixture once, only when told, and the spare passes were idle: {seen['reads']}")
    check(all(code == 0 for _t, _c, code, _e in seen["writes"]) and len(seen["writes"]) == 9,
          f"one write per fixture: {seen['writes']}")
    check(seen["kick_back"][0] == 0 and seen["kick_back"][2] == ["analysis", "director"],
          f"the misrouted implementer hands it back: {seen['kick_back']}")
    report = seen["report"]
    check(isinstance(report, dict), f"report runs: {report}")
    check(report["totals"] == {"correct_assign": 5, "correct_return": 3, "wrong_assign": 1, "needless_return": 0,
                               "pending": 0, "misroutes": 1, "director_corrections": 0},
          f"each decision is judged against the run file: {report['totals']}")
    check(report["accuracy"] == round(8 / 9, 3), f"accuracy: {report['accuracy']}")
    cost = report["cost"]
    check(cost["router_turns"] == 9 and cost["director_turns"] == 4 and cost["implementer_interruptions"] == 1
          and cost["with_router"] == 9 * 1 + 4 * 4 + 1 * 8 and cost["director_routing_by_hand"] == 36
          and cost["saved"] == 36 - 33,
          f"the price includes the Director's corrections and the implementer's interruption: {cost}")
    wrong = next(r for r in report["tickets"] if r["verdict"] == "wrong_assign")
    check(wrong["expect"] == "ops" and wrong["decision"] == "main", f"the wrong one is named: {wrong}")
    check(sorted(seen["finish"]["cancelled"]) == sorted(run["tickets"])
          and set(seen["after_finish"].values()) == {"cancelled"} and seen["finish_again"]["cancelled"] == [],
          f"finish closes every fixture, once: {seen['finish']} {seen['after_finish']}")


def main() -> int:
    verify(scenario())
    print(f"router_canary_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
