#!/usr/bin/env python3
"""SYRD-836: a Director-set test tier (`partial` | `full`, optional reason) on real boards.

Production-built boards (schema.sql, companion roles, rbac.sql, the real ticket-board-migrate, rbac.sql again), the
real HTTP server, the real `ticket-board-write` and `ticket-board-read`, and the real notify listener, as
`ticket_board_listener`, with an injected sender:

* a fresh board from the shipped example workflow;
* the board as it shipped before pgu1050, upgraded with this tree's migrations, so the migration's copy is what runs.

On each:
* the Director sets the tier at create-ticket and changes or clears it later with set-test-tier; each change is a
  Director comment and a field-audit row; a gate-only write is exactly what it was;
* anyone else is refused over HTTP and again in PostgreSQL as the service, and an invalid tier or reason is refused
  by the database itself, leaving the ticket as it was;
* the tier is in `--json` (top level and workflow_flags) and under Flags in the text read;
* the implementer's and Audit's hand-off say it, and a ticket without one is handed off with the same words as before.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

from router_role_board_test import PANE_ENV_PREFIXES, Board, example, fixture, temporary_cluster, to  # noqa: E402
from scripts.ticket_board import read_client, test_tier  # noqa: E402
from scripts.ticket_board.workflow_config import validate  # noqa: E402

MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu1050_syrd836_test_tier.sql"
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def before_tree(into: Path) -> Path:
    """The tree as it stood before pgu1050 joined it: that release's schema, migrations and grants."""
    def git(*args: str) -> str:
        return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout
    adding = git("log", "--format=%H", "--diff-filter=A", "--", str(MIGRATION.relative_to(ROOT))).split()
    assert adding, f"{MIGRATION.name} is not committed; the upgraded board is built from the commit before it"
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", f"{adding[-1]}^", "scripts"], check=True,
                             capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive.stdout, check=True)
    assert not (into / "scripts/ticket_board/migrations" / MIGRATION.name).exists()
    return into


def units() -> None:
    """The module's own rules, which the CLI applies before anything is sent."""
    check(test_tier.patch("full", " corpus reader ") == {"test_tier": "full", "test_tier_reason": "corpus reader"},
          "a tier and its trimmed reason")
    check(test_tier.patch("partial") == {"test_tier": "partial"}, "a tier without a reason")
    check(test_tier.patch(None) == {"test_tier": None}, "None clears it")
    for args in (("huge",), (None, "why"), ("full", "two\nlines"), ("full", "x" * 301), ("full", "bell\x07"),
                 ("full", "c1\x85c1")):
        try:
            test_tier.patch(*args)
        except ValueError:
            check(True, f"refused: {args!r}")
        else:
            check(False, f"refused: {args!r}")
    check(test_tier.fields({"test_tier": "full", "test_tier_reason": "r", "x": True})
          == {"test_tier": "full", "test_tier_reason": "r"}, "fields read the tier")
    check(test_tier.fields({}) == {"test_tier": "", "test_tier_reason": ""}
          and test_tier.fields(None) == {"test_tier": "", "test_tier_reason": ""}, "no tier reads as empty")
    check(test_tier.fields({"test_tier": "bogus", "test_tier_reason": "r"}) == {"test_tier": "", "test_tier_reason": ""},
          "a stored value that is not a tier reads as none")
    check(test_tier.handoff_line({"tier": "full", "reason": "a\x1b[2Jb"}) == "Test tier: full (a [2Jb).",
          "the hand-off line carries no control character from the reason")
    check(test_tier.handoff_line(None) == "" and test_tier.handoff_line({"tier": "x"}) == "", "no line without a tier")
    check(read_client.format_test_tier({"id": "X-1"}) == ["  test_tier: (not set)", "  test_tier_reason: (none)"],
          "a ticket from a board older than the field reads as not set")
    document = example(router=False)
    for name in ("test_tier", "test_tier_reason"):
        clash = json.loads(json.dumps(document))
        clash["flags"][name] = {"kind": "gate", "default": False}
        try:
            validate(clash)
        except ValueError as exc:
            check("protected" in str(exc), f"a declared flag cannot take the tier's name {name}: {exc}")
        else:
            check(False, f"a declared flag cannot take the tier's name {name}")


def board_checks(board: Board, name: str) -> None:
    fixture.TEST_WRITE_TOKEN = board.server.write_token
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": os.environ.get("HOME", "/tmp"),
           "TICKET_BOARD_URL": board.base, "TICKET_BOARD_WRITE_TOKEN": board.server.write_token}

    def run(*args: str, caller: str = "director") -> subprocess.CompletedProcess[str]:
        return subprocess.run([str(ROOT / "scripts" / args[0]), *args[1:]], capture_output=True, text=True,
                              timeout=120, env={**env, "TICKET_BOARD_CALLER_ROLE": caller}, stdin=subprocess.DEVNULL)

    def count() -> int:
        return int(board.sql("SELECT count(*) FROM ticket_board.tickets;"))

    def comments(ticket: str) -> list[str]:
        return json.loads(board.sql(f"SELECT coalesce(json_agg(who || ': ' || text ORDER BY position), '[]')::text "
                                    f"FROM ticket_board.ticket_comments WHERE ticket_id = '{ticket}';"))

    def create(caller: str, **payload) -> dict:
        try:
            return {"ok": fixture.post_json(board.base, "/api/tickets/actions/create_ticket", payload, caller=caller)}
        except AssertionError as exc:  # a refusal is an answer
            return {"refused": str(exc)}

    def audit(ticket: str) -> list[dict]:
        return json.loads(board.sql(
            "SELECT coalesce(json_agg(json_build_object('actor', actor, 'reason', reason, 'old', old_value, 'new', "
            f"new_value) ORDER BY id), '[]')::text FROM ticket_board.ticket_field_audit WHERE ticket_id = '{ticket}' "
            "AND field = 'test_tier';"))

    def flags(answer: dict) -> tuple[str, str, dict]:
        ticket = answer.get("ticket", answer)
        return ticket.get("test_tier"), ticket.get("test_tier_reason"), ticket.get("workflow_flags")

    # --- create-ticket, the Director's
    made = run("ticket-board-write", "create-ticket", "--title", "Corpus parser", "--body", "the parser",
               "--test-tier", "full", "--test-tier-reason", "touches the corpus reader")
    check(made.returncode == 0, f"{name}: the Director creates a ticket with a tier: {made.stderr[-400:]}")
    tiered = json.loads(made.stdout)["id"]
    detail = board.ticket(tiered)
    check(flags(detail) == ("full", "touches the corpus reader",
                            {"test_tier": "full", "test_tier_reason": "touches the corpus reader"}),
          f"{name}: the tier is in the ticket's JSON, top level and in workflow_flags: {flags(detail)}")
    check("director: Set the test tier to full: touches the corpus reader" in comments(tiered)
          and not any("Updated workflow gates" in c for c in comments(tiered)),
          f"{name}: and in its history, as that and nothing else: {comments(tiered)}")
    check(audit(tiered) == [{"actor": "director", "reason": "touches the corpus reader", "old": None,
                             "new": {"tier": "full", "reason": "touches the corpus reader"}}],
          f"{name}: and in its field audit: {audit(tiered)}")
    plain = json.loads(run("ticket-board-write", "create-ticket", "--title", "Docs typo", "--body", "b").stdout)["id"]
    check(flags(board.ticket(plain))[:2] == ("", "") and not any("test tier" in c for c in comments(plain)),
          f"{name}: a ticket created without one has none, and no history of one")

    # --- create-ticket, anyone else
    before = count()
    other = run("ticket-board-write", "create-ticket", "--title", "x", "--body", "b", "--test-tier", "partial",
                caller="ops")
    check(other.returncode != 0 and "the test tier can only be set by director" in other.stderr and count() == before,
          f"{name}: another role's create with a tier is refused and files nothing: {other.stderr[-300:]}")
    alone = run("ticket-board-write", "create-ticket", "--title", "x", "--body", "b", "--test-tier-reason", "why")
    check(alone.returncode != 0 and "--test-tier-reason needs --test-tier" in alone.stderr
          and "Traceback" not in alone.stderr and count() == before,
          f"{name}: a reason without a tier is refused before anything is sent: {alone.stderr[-300:]}")
    for payload in ({"test_tier": "huge"}, {"test_tier_reason": "why"}, {"test_tier": "full", "test_tier_reason": 5}):
        sent = create("director", title="x", body="b", **payload)
        check("refused" in sent and count() == before, f"{name}: the board refuses {payload!r} at create: {sent}")
    check("refused" in create("ops", title="x", body="b", test_tier="full") and count() == before,
          f"{name}: and another role's tier over HTTP")

    # --- set-test-tier: change, then clear
    changed = run("ticket-board-write", "set-test-tier", tiered, "--tier", "partial", "--reason", "unit tier is enough")
    check(changed.returncode == 0 and flags(board.ticket(tiered))[:2] == ("partial", "unit tier is enough"),
          f"{name}: the Director changes it: {changed.stderr[-300:]}")
    check("director: Set the test tier to partial (was full): unit tier is enough" in comments(tiered),
          f"{name}: the change names what it was: {comments(tiered)}")
    check(audit(tiered)[-1] == {"actor": "director", "reason": "unit tier is enough",
                                "old": {"tier": "full", "reason": "touches the corpus reader"},
                                "new": {"tier": "partial", "reason": "unit tier is enough"}},
          f"{name}: and is audited old to new: {audit(tiered)[-1]}")
    plain_set = run("ticket-board-write", "set-test-tier", plain, "--tier", "full")
    check(plain_set.returncode == 0 and flags(board.ticket(plain))[:2] == ("full", "")
          and "director: Set the test tier to full" in comments(plain)
          and audit(plain)[-1]["reason"] == "test tier set by the Director",
          f"{name}: a tier without a reason: {plain_set.stderr[-300:]} {audit(plain)}")
    cleared = run("ticket-board-write", "set-test-tier", plain, "--clear")
    check(cleared.returncode == 0 and flags(board.ticket(plain))[:2] == ("", "")
          and "test_tier" not in flags(board.ticket(plain))[2]
          and "director: Cleared the test tier (was full)" in comments(plain)
          and audit(plain)[-1]["new"] is None,
          f"{name}: and clears it: {cleared.stderr[-300:]}")
    both = run("ticket-board-write", "set-test-tier", plain, "--clear", "--reason", "x")
    check(both.returncode != 0 and "a cleared test tier takes no reason" in both.stderr and "Traceback" not in both.stderr,
          f"{name}: a cleared tier takes no reason: {both.stderr[-300:]}")
    neither = run("ticket-board-write", "set-test-tier", plain)
    check(neither.returncode != 0, f"{name}: set-test-tier needs --tier or --clear")

    # --- refusals: another role, over HTTP and in PostgreSQL
    snapshot = board.snapshot(tiered)
    by_main = run("ticket-board-write", "set-test-tier", tiered, "--tier", "full", caller="main")
    check(by_main.returncode != 0 and "only director may set gates" in by_main.stderr,
          f"{name}: the implementer cannot set it: {by_main.stderr[-300:]}")
    for role in ("main", "audit", "ops", "user"):
        answer = board.act(tiered, "set_workflow_flags", role, test_tier="full")
        check("refused" in answer, f"{name}: {role} is refused over HTTP: {answer}")
        pg = board.as_role_sql(role, f"SELECT ticket_board.set_declared_flags('{tiered}', '{{\"test_tier\": \"full\"}}')")
        check("only director may set gates" in pg.get("refused", ""), f"{name}: and in PostgreSQL: {pg}")
    # --- refusals: what the database itself refuses from the Director
    for bad in ({"test_tier": "huge"}, {"test_tier": 1}, {"test_tier_reason": "why"},
                {"test_tier": None, "test_tier_reason": "why"}, {"test_tier": "full", "test_tier_reason": 5},
                {"test_tier": "full", "test_tier_reason": "two\nlines"}, {"test_tier": "full", "test_tier_reason": "x" * 301},
                {"test_tier": "full", "test_tier_reason": "esc\x1b"}, {"test_tier": "full", "needs_nothing": True}):
        answer = board.act(tiered, "set_workflow_flags", "director", **bad)
        check("refused" in answer, f"{name}: the board refuses {bad!r}: {answer}")
    check(board.snapshot(tiered) == snapshot, f"{name}: and every refusal leaves the ticket as it was")
    listener_only = board.as_role_sql("director", f"SELECT ticket_board.ticket_test_tier('{tiered}')")
    check("permission denied for function ticket_test_tier" in listener_only.get("refused", ""),
          f"{name}: only the listener reads it so; the service has no grant: {listener_only}")

    # --- a gate write is what it was; a mixed write does both
    gate = board.act(plain, "set_workflow_flags", "director", needs_inspection=True)
    check("ok" in gate and comments(plain)[-1] == 'director: Updated workflow gates: {"needs_inspection": true}',
          f"{name}: a gate-only write is unchanged: {comments(plain)[-1:]}")
    mixed = board.act(plain, "set_workflow_flags", "director", needs_inspection=False, test_tier="partial")
    check("ok" in mixed and comments(plain)[-2:] == ['director: Updated workflow gates: {"needs_inspection": false}',
                                                    "director: Set the test tier to partial"]
          and board.sql(f"SELECT needs_inspection FROM ticket_board.tickets WHERE id = '{plain}';") == "f",
          f"{name}: a mixed write sets both, each recorded: {comments(plain)[-2:]}")
    board.act(plain, "set_workflow_flags", "director", test_tier=None)

    # --- the text read
    read = subprocess.run([str(ROOT / "scripts/ticket-board-read"), "ticket", tiered], capture_output=True, text=True,
                          timeout=60, env={**env, "TICKET_BOARD_CALLER_ROLE": "main"})
    flags_block = read.stdout.split("Flags:", 1)[-1].split("Blockers:", 1)[0]
    check(read.returncode == 0 and "  test_tier: partial\n" in flags_block
          and "  test_tier_reason: unit tier is enough\n" in flags_block,
          f"{name}: the text read shows it under Flags: {flags_block!r} {read.stderr[-300:]}")
    read_plain = subprocess.run([str(ROOT / "scripts/ticket-board-read"), "ticket", plain], capture_output=True,
                                text=True, timeout=60, env={**env, "TICKET_BOARD_CALLER_ROLE": "main"})
    check("  test_tier: (not set)\n  test_tier_reason: (none)\n" in read_plain.stdout,
          f"{name}: and says when there is none: {read_plain.stdout[:600]!r}")
    as_json = subprocess.run([str(ROOT / "scripts/ticket-board-read"), "ticket", tiered, "--json"], capture_output=True,
                             text=True, timeout=60, env={**env, "TICKET_BOARD_CALLER_ROLE": "main"})
    check(as_json.returncode == 0 and json.loads(as_json.stdout)["test_tier"] == "partial",
          f"{name}: and `--json` carries it: {as_json.stdout[:200]}")

    # --- hand-offs: the implementer's and Audit's say it; none for a ticket without one
    board.one_pass()
    owner = {tiered: "main", plain: "ops"}  # one ticket each: a second for main would wait in its serial queue
    for ticket in (tiered, plain):
        routed = run("ticket-board-write", "route", ticket, "--state", "in_progress", "--assignee", owner[ticket])
        check(routed.returncode == 0, f"{name}: routed {ticket}: {routed.stderr[-300:]}")
    handed = []
    for _ in range(4):  # a pane handed a notice is busy until its turn ends: one hand-off per pass
        batch = board.one_pass()
        handed += to(batch, "main") + to(batch, "ops")
        board.busy.clear()
    title = {tiered: "Corpus parser", plain: "Docs typo"}
    check(f"{tiered} -- {title[tiered]} entered Implementation. Test tier: partial (unit tier is enough)." in handed,
          f"{name}: the implementer's hand-off says the tier: {handed}")
    check(f"{plain} -- {title[plain]} entered Implementation" in handed,
          f"{name}: a ticket without one is handed off with the same words as before: {handed}")
    board.busy.clear()
    said = run("ticket-board-write", "add-comment", tiered, "--text", "Please keep the reader's API.")
    other_notices = []
    for _ in range(3):
        other_notices += [m for m in to(board.one_pass(), "main") if tiered in m]
        board.busy.clear()
    check(said.returncode == 0 and other_notices and not any("Test tier" in m for m in other_notices),
          f"{name}: a notice that hands nothing over (a comment) says no tier: {other_notices} {said.stderr[-200:]}")
    for ticket in (tiered, plain):
        done = run("ticket-board-write", "submit-to-audit-without-commit", ticket, "--reason", "measured",
                   caller=owner[ticket])
        check(done.returncode == 0, f"{name}: submitted {ticket}: {done.stderr[-300:]}")
    to_audit = []
    for _ in range(4):
        to_audit += to(board.one_pass(), "audit")
        board.busy.clear()
    check(any(m.startswith(f"{tiered} -- ") and m.endswith(" Test tier: partial (unit tier is enough).") for m in to_audit),
          f"{name}: Audit's hand-off says it: {to_audit}")
    # The untiered ticket waits behind it: Audit reviews one at a time (its hand-off is checked at ops above).


def main() -> int:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    units()
    with temporary_cluster(prefix="syrd836-", shutdown="immediate") as cluster:
        for name in ("fresh", "upgraded"):
            before = before_tree(cluster.root / "before") if name == "upgraded" else None
            board = Board(cluster, f"tier_{name}", example(router=False), before=before)
            try:
                board_checks(board, name)
            finally:
                board.close()
    print(f"test_tier_board_test: ok ({CHECKS} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
