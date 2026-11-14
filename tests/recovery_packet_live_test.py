#!/usr/bin/env python3
"""SYRD-737: a provider safety hold's recovery packet, read from a production-built board, with no model turn.

The board is SYRD-672's rig: schema, rbac and every migration, the real listener, gate and pane hook, stand-in
refusals only, and no provider ever contacted. The Director is the first role refused, so the claude account holds
the Director, app and ops, and nobody can ask the refused model for anything.

* Before any hold the board says there is nothing to recover, and builds nothing.
* After the Director's own refusal, `GET /api/recovery-packet` answers the packet, as the board service alone builds
  it: nothing is sent to the Director or any held role, and the board's records do not change. It names the hold, the
  roles and tickets it affects, the pending notice that waits on it, the blockers and waits, and the board's events --
  and none of the refused words, a ticket's text, a comment, a notice's message, a blocked reason or the write token.
  A refused word written into an event the board keeps drops that event, counted.
* The same board read twice is the same packet. A change committed while the packet is being read is not in it: one
  snapshot. Read afterwards it is, and both consumers see the packet they hold is stale, and what changed.
* Both recovery consumers validate it and its provenance over their own transports: the break-glass role through
  `ticket-board-read recovery-packet --check`, and a Director replacement over the board's Unix socket, as root's
  proven connection reads it. Neither is given anything a packet could grant.
* A second hold, by hand, with a reason: the board asks which; the named one's packet keeps no reason.
* Once the hold is cleared, there is no packet for it, and a saved one no longer checks.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import provider_account_hold_test as s672  # noqa: E402
import ticket_board_write_api_test as fixture  # noqa: E402
from scripts.ticket_board import recovery_packet as rp  # noqa: E402
from scripts.ticket_board.server import TicketBoardEventHub, TicketBoardUnixServer  # noqa: E402
from scripts.ticket_board.write_client import UnixHTTPConnection  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

SECRET = s672.SECRET
CHECKS = 0
TABLES = ("tickets", "ticket_blockers", "ticket_comments", "ticket_notification_queue", "provider_account_holds",
          "provider_refusal_evidence", "board_progress_events", "user_events", "role_runtime_assignments")


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


class Rig(s672.Rig):
    def packet_http(self, query: str = "") -> tuple[int, object]:
        try:
            with urllib.request.urlopen(f"{self.base}{rp.PATH}{query}", timeout=20) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", "replace")

    def records(self) -> str:
        """Every table a packet reads, as it stands: a GET must leave it exactly so."""
        return self.sql("SELECT " + " || '|' || ".join(
            f"(SELECT md5(coalesce(string_agg(r::text, ',' ORDER BY r::text), '')) FROM ticket_board.{t} r)" for t in TABLES) + ";")

    def read_cli(self, *args: str) -> subprocess.CompletedProcess:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PGU_"))}
        env["TICKET_BOARD_PROJECT"] = "pgu"
        return subprocess.run([sys.executable, str(ROOT / "scripts/ticket-board-read"), "--board-url", self.base, *args],
                              capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL, timeout=60)


class Unix:
    """The board's Unix socket, as a Director replacement reads it: root's proven connection is a GET over it."""

    def __init__(self, rig: Rig, path: Path) -> None:
        self.path = path
        self.server = TicketBoardUnixServer(path, rig.app, events=TicketBoardEventHub(rig.app),
                                            director_notifier=fixture.QuietNotifier())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def get(self, request_path: str) -> object:
        connection = UnixHTTPConnection(str(self.path), timeout=20)
        try:
            connection.request("GET", request_path)
            response = connection.getresponse()
            body = response.read().decode("utf-8", "replace")
        finally:
            connection.close()
        if response.status != 200:
            raise ValueError(f"board answered {response.status}: {body[:300]}")
        return json.loads(body)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def attempt(call, *args, **kwargs):
    """A call that should answer: its refusal is this suite's failed check, never a crash."""
    try:
        return call(*args, **kwargs)
    except (rp.PacketError, ValueError, KeyError, TypeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:400]}


def problems(packet: object) -> list[str]:
    try:
        rp.validate(packet)
    except rp.PacketError as exc:
        return exc.problems
    return []


def leaks(packet: object, rig: Rig) -> list[str]:
    rendered = attempt(rp.render, packet)
    text = rp.canonical(packet).decode("ascii") + (rendered if isinstance(rendered, str) else "")
    return [word for word in (SECRET, rig.server.write_token, "Director: ops", "pgu-director:0.0", "/tmp")
            if word and word in text]


def scenario_no_hold_no_packet(rig: Rig) -> None:
    before = rig.records()
    status, said = rig.packet_http()
    cli = rig.read_cli("recovery-packet")
    check(status == 400 and "no provider account is held now" in str(said) and cli.returncode == 1
          and "no provider account is held now" in cli.stderr and rig.records() == before,
          f"no hold, no packet, and nothing changed: {status} {said!r} {cli.returncode} {cli.stderr[-200:]!r}")


def scenario_director_first_refusal(rig: Rig, unix: Unix, tmp: Path) -> None:
    rig.start("PGU-1", "main")
    rig.start("PGU-2", "ops")
    rig.passes(1.5)
    rig.act("PGU-2", "set_blockers", blocked_by=["PGU-1"], blocked_reason=f"{SECRET} waits on PGU-1")
    rig.act("PGU-1", "add_comment", text=f"Director: ops, {SECRET}")
    mark = time.monotonic()
    # The Director's own turn is refused: the real hook, from the Director's own process.
    rig.claude_refusal("director", uuid="refusal-director-1")
    rig.passes(1.5)
    hold = rig.holds()[-1]
    check(hold["runtime"] == "claude" and hold["role"] == "director" and hold["cleared_at"] is None,
          f"the Director's refusal holds the claude account: {hold}")
    # More notices to an unheld role than a packet keeps, and then, newest of all, one to the held Director about
    # unheld work, which waits, requeued, never dropped: the cap keeps the held Director's first. A notice to a
    # declared role that is not active is not a held role's, even on the held account: it takes no held slot.
    rig.sql("SELECT ticket_board.enqueue_notification('PGU-1', 'ticket_update', 'main', 'note ' || n, "
            "jsonb_build_object('kind', 'ticket_update', 'id', 'PGU-1', 'target_role', 'main'), 'syrd737-many-' || n) "
            f"FROM generate_series(1, {rp.MAX_NOTIFICATIONS + 10}) n;"
            # The inactive designer is declared on the held claude account: its notice is not a held role's.
            "INSERT INTO ticket_board.ticket_notification_queue (ticket_id, kind, target_role, message, payload, dedupe_key)"
            " VALUES ('PGU-1', 'ticket_update', 'designer', 'note', jsonb_build_object('kind', 'ticket_update'),"
            " 'syrd737-designer');"
            "SELECT ticket_board.enqueue_notification('PGU-1', 'escalation', 'director', "
            f"'{SECRET}: main may be stuck on PGU-1.', jsonb_build_object('kind', 'escalation', 'id', 'PGU-1', "
            "'assignee', 'main', 'target_role', 'director'), 'syrd737-esc');"
            "INSERT INTO ticket_board.board_progress_events (ticket_id, event) VALUES "
            f"('PGU-1', 'PGU-1 moved analysis -> in_progress {SECRET}');")
    total = int(rig.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE dead_lettered_at IS NULL;"))
    sends, before = len(rig.sends), rig.records()
    status, packet = rig.packet_http()
    again_status, again = rig.packet_http()
    check(status == 200 and again_status == 200 and len(rig.sends) == sends and rig.records() == before,
          f"the board builds it alone: a GET sends nothing to any pane and changes no record: {status} {str(packet)[:300]}")
    check(rig.to("director", mark) == [] and rig.to("app", mark) == [] and rig.to("ops", mark) == [],
          f"no turn of the refused Director, or any held role, was asked for: "
          f"{[(r, m[:60]) for t, r, m in rig.sends if t >= mark]}")
    check(problems(packet) == [], f"the board's packet validates: {problems(packet)}")
    held = packet["hold"]
    check(held["id"] == hold["id"] and held["kind"] == "refusal" and held["category"] == "cyber"
          and held["source"] == "claude.Stop" and held["role"] == "director" and held["began_by"] == "listener"
          and held["refusals"] == 1 and held["director_held"] is True and held["director_notified"] is False
          and held["roles"] == ["app", "audit", "director", "inspector", "ops"],
          f"the hold, as the board recorded it: the Director held and not told: {held}")
    roles = {r["role"]: r for r in packet["roles"]}
    check(roles["main"]["runtime"] == "codex" and roles["main"]["held_by"] is None and roles["main"]["registered"]
          and roles["director"]["held_by"] == hold["id"] and roles["director"]["controls"]
          and roles["audit"]["registered"] is False and roles["audit"]["held_by"] == hold["id"],
          f"each role's account, registered or declared: {roles}")
    clear = packet["choices"][2]
    check(clear["by"] == ["user"] and clear["command"] == rp.clear_command("claude")
          and packet["choices"][1]["to"] == ["codex"],
          f"only the User may clear the Director's account; the work may move to codex: {packet['choices']}")
    tickets = {t["id"]: t for t in packet["tickets"]}
    check(tickets["PGU-1"]["stage"] == "in_progress" and tickets["PGU-1"]["assignee"] == "main"
          and not tickets["PGU-1"]["affected"] and tickets["PGU-2"]["assignee"] == "ops" and tickets["PGU-2"]["affected"]
          and tickets["PGU-2"]["blockers"] == [{"id": "PGU-1", "resolved": False}] and "PGU-2" in packet["affected"]["tickets"],
          f"the active tickets, their owners and blockers; ops's is affected: {tickets}")
    waiting = [n for n in packet["notifications"] if n["ticket"] == "PGU-1" and n["to"] == "director"]
    check(len(waiting) == 1 and waiting[0]["held"] and waiting[0]["kind"] == "escalation" and waiting[0]["about"] == "escalation"
          and len(packet["notifications"]) == rp.MAX_NOTIFICATIONS
          and all(n["held"] == (n["to"] in held["roles"]) for n in packet["notifications"])
          and "designer" not in {n["to"] for n in packet["notifications"]}
          and packet["omitted"]["notifications"] == total - rp.MAX_NOTIFICATIONS,
          f"of {total} pending notices, the cap keeps the one waiting on the held Director first, and counts the rest: "
          f"{waiting} {packet['omitted']} {[(n['id'], n['to'], n['held']) for n in packet['notifications']]}")
    kinds = {(e["ticket"], e["type"]) for e in packet["events"]}
    check({("PGU-1", "moved"), ("PGU-1", "delivered"), ("PGU-2", "blocker")} <= kinds
          and packet["omitted"]["malformed"]["events"] == 1,
          f"the board's events, parsed; the one carrying refused words is dropped and counted: {kinds} {packet['omitted']}")
    check(leaks(packet, rig) == [],
          f"no refused word, comment, blocked reason, notice message, pane target, path or write token: {leaks(packet, rig)}")
    prov = packet["provenance"]
    check(prov["project"] == "pgu" and prov["ticket_prefix"] == "PGU" and prov["db_xmax"] > 0
          and prov["high_water"]["hold"] == hold["id"]
          and prov["high_water"]["event"] == int(rig.sql("SELECT max(id) FROM ticket_board.board_progress_events;")),
          f"provenance: this board, its snapshot, the newest rows it saw: {prov}")
    check(again["provenance"]["state_digest"] == prov["state_digest"]
          and {k: v for k, v in again.items() if k not in ("provenance", "packet_digest")}
          == {k: v for k, v in packet.items() if k not in ("provenance", "packet_digest")},
          "the same board read twice is the same packet")
    text = rig.read_cli("recovery-packet")
    check(text.returncode == 0 and f"Recovery packet for hold {hold['id']}: the claude account" in text.stdout
          and "*PGU-2 [in_progress/ops]" in text.stdout and SECRET not in text.stdout,
          f"ticket-board-read renders it, from its fields: {text.stdout[:300]} {text.stderr[-200:]}")

    # A change committed while the packet is being read is not in it.
    real = rig.app._pg_connect

    class Racing:
        def __init__(self, conn) -> None:
            self.conn, self.raced = conn, False

        def __enter__(self):
            self.conn.__enter__()
            return self

        def __exit__(self, *exc):
            return self.conn.__exit__(*exc)

        def __getattr__(self, name):
            return getattr(self.conn, name)

        def execute(self, statement, *args, **kwargs):
            found = self.conn.execute(statement, *args, **kwargs)
            if "FROM ticket_board.provider_account_holds h WHERE h.cleared_at IS NULL" in statement and not self.raced:
                self.raced = True
                rig.sql("INSERT INTO ticket_board.ticket_blockers (ticket_id, blocker_ticket_id, position) "
                        "VALUES ('PGU-1', 'PGU-2', 0);")
            return found

    with mock.patch.object(rig.app, "_pg_connect", lambda: Racing(real())):
        torn_status, torn = rig.packet_http()
    newest = int(rig.sql("SELECT max(id) FROM ticket_board.board_progress_events;"))
    one = {t["id"]: t for t in torn["tickets"]}["PGU-1"] if torn_status == 200 else {}
    check(torn_status == 200 and one.get("blockers") == [] and torn["provenance"]["high_water"]["event"] < newest
          and not any(e["type"] == "blocker" and e["ticket"] == "PGU-1" for e in torn["events"])
          and torn["provenance"]["state_digest"] == prov["state_digest"],
          f"a blocker set mid-read is in neither its tickets nor its events: one snapshot: {one} {str(torn)[:200]}")
    _status, after = rig.packet_http()
    check({t["id"]: t for t in after["tickets"]}["PGU-1"]["blockers"] == [{"id": "PGU-2", "resolved": False}]
          and any(e["type"] == "blocker" and e["ticket"] == "PGU-1" for e in after["events"]),
          "read afterwards, it is in both")

    # The break-glass consumer, through ticket-board-read; the Director replacement, over the Unix socket.
    saved = tmp / "packet.json"
    saved.write_text(json.dumps(after))
    fresh = rig.read_cli("recovery-packet", "--check", str(saved))
    verdict = attempt(rp.fetch_verified, unix.get, project="pgu", packet=json.loads(saved.read_text()))
    check(fresh.returncode == 0 and "the board still reads as it did" in fresh.stdout
          and verdict.get("current") is True and verdict.get("hold") == hold["id"] and verdict.get("changed") == [],
          f"both consumers find the packet valid, this board's, for the current hold, and current: "
          f"{fresh.stdout} {fresh.stderr[-200:]} {verdict}")
    rig.sql("UPDATE ticket_board.ticket_blockers SET resolved = true WHERE ticket_id = 'PGU-2' AND blocker_ticket_id = 'PGU-1';")
    stale = rig.read_cli("recovery-packet", "--check", str(saved), "--json")
    seen = json.loads(stale.stdout) if stale.stdout.strip().startswith("{") else {}
    verdict = attempt(rp.fetch_verified, unix.get, project="pgu", packet=json.loads(saved.read_text()))
    check(stale.returncode == 1 and seen.get("current") is False and "tickets" in seen.get("changed", [])
          and verdict.get("current") is False and "tickets" in verdict.get("changed", [])
          and "events" in verdict.get("changed", []),
          f"after a change both see the packet is stale, and what changed: {stale.stdout} {verdict}")
    for what, project, packet_ in (("another project", "mefp", after), ("a tampered packet", "pgu",
                                                                         dict(after, version=2))):
        try:
            rp.fetch_verified(unix.get, project=project, packet=packet_)
            refused = ""
        except rp.PacketError as exc:
            refused = str(exc)
        check(refused != "", f"{what} is refused: {refused!r}")
    tampered = json.loads(saved.read_text())
    tampered["choices"][2]["by"].append("director")
    saved.write_text(json.dumps(tampered))
    cli = rig.read_cli("recovery-packet", "--check", str(saved))
    check(cli.returncode == 1 and "choices: not the board's fixed recovery choices" in cli.stderr,
          f"a packet given a held clearer is refused by the CLI: {cli.stderr[-300:]}")
    saved.write_text(json.dumps(after))


def scenario_a_second_hold_by_hand(rig: Rig, claude_hold: int) -> None:
    rig.may("hold_provider_account", "user", runtime="codex", reason=f"looked like a refusal: {SECRET}")
    codex = rig.holds()[-1]
    status, said = rig.packet_http()
    check(status == 400 and "more than one provider account is held now" in str(said)
          and f"{codex['id']} (codex)" in str(said) and f"{claude_hold} (claude)" in str(said),
          f"two holds and none named: the board asks which: {status} {said!r}")
    status, packet = rig.packet_http(f"?hold={codex['id']}")
    check(status == 200 and packet["hold"]["kind"] == "suspected" and packet["hold"]["began_by"] == "user"
          and packet["hold"]["director_held"] is False and packet["hold"]["role"] is None
          and packet["hold"]["source"] == "by_hand" and packet["hold"]["roles"] == ["main"]
          and [h["id"] for h in packet["other_holds"]] == [claude_hold] and leaks(packet, rig) == []
          and packet["choices"][2]["by"] == ["user"],
          f"the named hold's packet: held by hand, its reason left out, the other hold named: {str(packet)[:600]}")
    for bad in ("?hold=x", "?hold=999"):
        status, said = rig.packet_http(bad)
        check(status == 400, f"{bad}: refused: {status} {said!r}")
    rig.op("clear_provider_account_hold", caller="user", runtime="codex", reason="checked by hand")


def scenario_cleared_hold_has_no_packet(rig: Rig, unix: Unix, tmp: Path) -> None:
    rig.op("clear_provider_account_hold", caller="user", runtime="claude", reason="operator confirmed the account is safe")
    status, said = rig.packet_http()
    check(status == 400 and "no provider account is held now" in str(said), f"cleared: no packet: {status} {said!r}")
    cli = rig.read_cli("recovery-packet", "--check", str(tmp / "packet.json"))
    try:
        rp.fetch_verified(unix.get, project="pgu", packet=json.loads((tmp / "packet.json").read_text()))
        refused = ""
    except ValueError as exc:
        refused = str(exc)
    check(cli.returncode == 1 and "is not a current provider account hold" in cli.stderr
          and "is not a current provider account hold" in refused,
          f"a saved packet for a cleared hold no longer checks, on either path: {cli.stderr[-200:]} {refused!r}")


def test_on_a_production_board() -> None:
    with temporary_cluster(prefix="s737", shutdown="immediate") as cluster, tempfile.TemporaryDirectory(prefix="syrd737.") as tmp:
        state_dir, session_dir = Path(tmp) / "state", Path(tmp) / "sessions"
        state_dir.mkdir(), session_dir.mkdir()
        rig = Rig(cluster, state_dir, session_dir)
        unix = Unix(rig, Path(tmp) / "board.sock")
        try:
            scenario_no_hold_no_packet(rig)
            scenario_director_first_refusal(rig, unix, Path(tmp))
            scenario_a_second_hold_by_hand(rig, rig.holds()[0]["id"])
            scenario_cleared_hold_has_no_packet(rig, unix, Path(tmp))
        finally:
            unix.close()
            rig.close()


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"recovery_packet_live_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
