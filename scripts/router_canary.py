#!/usr/bin/env python3
"""A bounded, priced canary for the Router role (SYRD-599).

Three steps, each one command the Director runs once. None of them waits or polls:
Router is told about the fixtures by the board, and the Director reads the
report when it chooses to (SYRD-597).

* `seed` creates the fixture tickets in triage, as the Director, and routes each
  the ordinary way, which on a Router board is through Router. Expectations are written to a local run file and never to the
  board, so Router cannot read the answer. Fixtures whose expected owner is not
  an active Implementation owner of this project are skipped and listed.
* `report` reads the board and prices what Router did. A justified abstention
  costs one Director triage, which routing by hand costs anyway. A wrong
  assignment costs Router's turn, an implementer's interruption and a Director
  correction, which is why abstaining is cheaper than guessing.
* `finish` cancels the fixtures that are not already closed, as the Director.

Weights are in whatever unit the operator prices turns in (tokens, minutes,
money); the defaults only say a Director turn costs more than a Router turn, and
an interruption more than either.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.dont_write_bytecode = True

from scripts.ticket_board.read_client import TicketBoardReadClient  # noqa: E402
from scripts.ticket_board.write_client import TicketBoardWriteClient  # noqa: E402

FIXTURES = ROOT / "examples" / "router_canary" / "fixtures.json"
TITLE_PREFIX = "[router canary] "
MAX_FIXTURES = 20
BODY_NOTE = ("\n\nRouter canary fixture (SYRD-599): if this reaches an implementer, there is nothing to build; "
             "the Director cancels it when the canary ends.")
CLOSED = {"done", "cancelled"}


def load_fixtures(path: Path) -> list[dict[str, str]]:
    fixtures = json.loads(path.read_text())["fixtures"]
    if not 0 < len(fixtures) <= MAX_FIXTURES:
        raise SystemExit(f"router canary: between 1 and {MAX_FIXTURES} fixtures, not {len(fixtures)}")
    for item in fixtures:
        if set(item) != {"expect", "title", "body"} or not all(isinstance(v, str) and v.strip() for v in item.values()):
            raise SystemExit(f"router canary: each fixture is exactly expect, title and body: {item}")
    return fixtures


def implementation_owners(workflow: dict[str, Any]) -> tuple[str, list[str]]:
    document = workflow.get("document") or workflow
    stage = next(s for s in document["stages"] if s["kind"] == "implementation")
    roles = {r["name"]: r for r in document["roles"]}
    if not (roles.get("router") or {}).get("active"):
        raise SystemExit("router canary: this project has no active router; `switchyard router-role enable` first")
    return stage["name"], sorted(o for o in stage["owners"] if roles.get(o, {}).get("active"))


def seed(reader: TicketBoardReadClient, writer: TicketBoardWriteClient, fixtures: list[dict[str, str]],
         run_file: Path) -> dict[str, Any]:
    if run_file.exists():
        raise SystemExit(f"router canary: {run_file} already records a run; report or finish it, or choose another file")
    stage, owners = implementation_owners(reader._get_json(f"{reader.root_url}/api/workflow"))
    run: dict[str, Any] = {"stage": stage, "owners": owners, "tickets": {}, "skipped": []}
    for item in fixtures:
        if item["expect"] != "return" and item["expect"] not in owners:
            run["skipped"].append({"title": item["title"], "expect": item["expect"]})
            continue
        created = writer.create_ticket(title=TITLE_PREFIX + item["title"], body=item["body"] + BODY_NOTE,
                                       state="analysis", assignee="director")
        ticket_id = str((created.get("ticket") or created)["id"])
        writer.route(ticket_id, state=stage)  # no owner named: through Router, or refused if it is not running
        run["tickets"][ticket_id] = {"expect": item["expect"], "title": item["title"]}
        run_file.write_text(json.dumps(run, indent=2) + "\n")  # after each, so a failure leaves a finishable record
    run_file.write_text(json.dumps(run, indent=2) + "\n")
    return run


def report(reader: TicketBoardReadClient, run: dict[str, Any], *, router_turn: float, director_turn: float,
           interruption: float) -> dict[str, Any]:
    board = {str(t["id"]): t for t in reader.get_board().get("tickets", [])}
    rows, totals = [], {k: 0 for k in ("correct_assign", "correct_return", "wrong_assign", "needless_return",
                                       "pending", "misroutes", "director_corrections")}
    for ticket_id, wanted in sorted(run["tickets"].items()):
        ticket = board.get(ticket_id, {})
        decisions = ((ticket.get("routing") or {}).get("decisions")) or []
        first = decisions[0] if decisions else None
        if first is None:
            verdict = "pending"
        elif first["decision"] == "return":
            verdict = "correct_return" if wanted["expect"] == "return" else "needless_return"
        else:
            verdict = "correct_assign" if first.get("assignee") == wanted["expect"] else "wrong_assign"
        totals[verdict] += 1
        totals["misroutes"] += sum(d.get("outcome") == "misroute" for d in decisions)
        totals["director_corrections"] += sum(d.get("outcome") == "director_correction" for d in decisions)
        rows.append({"ticket": ticket_id, "expect": wanted["expect"], "verdict": verdict,
                     "decision": None if first is None else (first.get("assignee") or "return"),
                     "reason": None if first is None else first.get("reason"), "state": ticket.get("state")})
    decided = len(rows) - totals["pending"]
    # Every decision is a Router turn. A return, and every wrong assignment, is a
    # Director turn as well; a wrong assignment is also an implementer's
    # interruption, observed or not yet observed.
    director_turns = totals["correct_return"] + totals["needless_return"] + totals["wrong_assign"]
    interruptions = totals["wrong_assign"]
    cost = decided * router_turn + director_turns * director_turn + interruptions * interruption
    by_hand = decided * director_turn
    return {"tickets": rows, "totals": totals, "decided": decided, "skipped": run.get("skipped", []),
            "accuracy": None if not decided else round((totals["correct_assign"] + totals["correct_return"]) / decided, 3),
            "cost": {"router_turns": decided, "director_turns": director_turns, "implementer_interruptions": interruptions,
                     "weights": {"router_turn": router_turn, "director_turn": director_turn, "interruption": interruption},
                     "with_router": cost, "director_routing_by_hand": by_hand, "saved": by_hand - cost}}


def finish(reader: TicketBoardReadClient, writer: TicketBoardWriteClient, run: dict[str, Any]) -> list[str]:
    board = {str(t["id"]): t for t in reader.get_board().get("tickets", [])}
    cancelled = []
    for ticket_id in sorted(run["tickets"]):
        if board.get(ticket_id, {}).get("state") in CLOSED or ticket_id not in board:
            continue
        writer.workflow_action(ticket_id, "cancel", {"reason": "Router canary finished (SYRD-599)."})
        cancelled.append(ticket_id)
    return cancelled


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="router_canary", description=__doc__.splitlines()[0])
    parser.add_argument("command", choices=("plan", "seed", "report", "finish"))
    parser.add_argument("--run-file", type=Path, default=Path("router-canary-run.json"))
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--board-url", default=os.environ.get("TICKET_BOARD_URL", ""))
    parser.add_argument("--router-turn", type=float, default=1.0)
    parser.add_argument("--director-turn", type=float, default=4.0)
    parser.add_argument("--interruption", type=float, default=8.0)
    args = parser.parse_args(argv)
    fixtures = load_fixtures(args.fixtures)
    if args.command == "plan":
        print(json.dumps([{"title": f["title"], "expect": f["expect"]} for f in fixtures], indent=2))
        return 0
    if not args.board_url:
        parser.error("no board: pass --board-url or run where TICKET_BOARD_URL is set")
    reader = TicketBoardReadClient(args.board_url)
    if args.command == "seed":
        writer = TicketBoardWriteClient(board_url=args.board_url, caller_role="director")
        print(json.dumps(seed(reader, writer, fixtures, args.run_file), indent=2))
        return 0
    run = json.loads(args.run_file.read_text())
    if args.command == "report":
        print(json.dumps(report(reader, run, router_turn=args.router_turn, director_turn=args.director_turn,
                                interruption=args.interruption), indent=2))
        return 0
    writer = TicketBoardWriteClient(board_url=args.board_url, caller_role="director")
    print(json.dumps({"cancelled": finish(reader, writer, run)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
