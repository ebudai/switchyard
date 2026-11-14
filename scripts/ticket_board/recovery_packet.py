"""SYRD-737: a fixed-schema recovery packet for a provider safety hold, built by the board from its own records.

When a provider refuses on safety grounds, its whole account is held (SYRD-672), and the Director may be the role
refused. Nothing may then ask that model for a handoff. This builds one mechanically, from trusted board rows only,
so an alternate runtime -- a replacement Director (SYRD-738) or a one-operation break-glass role (SYRD-739) -- can
see the coordination state without the refused model.

What it holds, each field from an explicit allowlist and each string matched against a fixed pattern (identifiers,
stage and role names, runtimes, timestamps, commit hashes, digests):
- the hold: its id, runtime, kind, category, source, refused role and anchor ticket, who began it and when, how many
  refusals it took, the roles it holds, and whether the Director is one;
- the other current holds; the affected roles and tickets; the recovery choices the board supports, as fixed
  commands, and which unheld roles may take them;
- every role's provider account (registered or declared) and whether it is held;
- the active tickets: stage, assignee, parent, blockers, waits, hold and park flags, candidate commit;
- the pending notifications: id, ticket, kind, target, payload kind, attempts, times, and whether held;
- a window of the board's durable progress events, parsed back into fields from the grammar the board writes them in.

What it never holds: a ticket's title, body or comments, a notice's message, a hold's or blocker's free-text reason, a
provider's error text, pane text, a refused prompt or response, a credential or token. A row whose any field does not
match its pattern is dropped whole and counted, never cleaned up and kept: nothing outside the allowlist can ride in.

Deterministic: every list is in a fixed order, the encoding is canonical JSON, and `state_digest` covers everything but
the provenance, so the same trusted state gives the same packet, whenever it is built. Bounded: each list has a cap,
and what a cap leaves out is chosen in a fixed order (what the hold affects first) and counted; the whole packet can
never exceed MAX_BYTES. Provenance: the project and ticket prefix, the board's build, when it was built, the database
snapshot's xmax, and the newest event, notification and hold it saw. Built in one REPEATABLE READ, READ ONLY
transaction, so a change in the middle of the read cannot tear it.

Reading it is a GET (`/api/recovery-packet[?hold=N]`, `ticket-board-read recovery-packet`). It changes nothing,
invokes no provider and needs no model turn, and it carries less than the board's own ticket reads do, and no token:
having it grants nothing. A consumer checks it with `verify`, against a fresh one it fetched itself over its own
transport, so its authority is the transport's, never the packet's.
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

SCHEMA = "switchyard.recovery-packet"
VERSION = 1
MAX_TICKETS = 100
MAX_NOTIFICATIONS = 50
MAX_EVENTS = 50
MAX_ROLES = 64
MAX_BLOCKERS = 8
MAX_OTHER_HOLDS = 8
#: The most the encoded packet can be. The caps above keep the fullest packet (194,307 bytes) within it.
MAX_BYTES = 196608
RUNTIMES = ("agy", "claude", "codex", "copilot", "hermes")
PATH = "/api/recovery-packet"

TICKET = r"[A-Z][A-Z0-9]{0,15}-[0-9]{1,9}"
ROLE = r"[a-z][a-z0-9_-]{0,63}"
STAGE = r"[a-z][a-z0-9_]{0,63}"
BLOCKER = rf"(?:[a-z][a-z0-9_]{{0,31}}:)?{TICKET}|operator:[a-z][a-z0-9_]{{0,31}}"
PATTERNS = {
    "ticket": TICKET, "role": ROLE, "stage": STAGE, "blocker": BLOCKER, "runtime": "|".join(RUNTIMES),
    "time": r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{6}Z",
    "commit": r"[0-9a-f]{7,64}", "category": r"[a-z0-9_-]{0,32}", "source": r"[A-Za-z0-9_.-]{0,64}",
    "hold_kind": "refusal|suspected", "notice": "transition|ticket_update|nudge|escalation|idle_reminder|awaiting_role",
    "about": r"[a-z][a-z0-9_]{0,63}", "project": r"[a-z][a-z0-9_-]{0,63}", "prefix": r"[A-Z][A-Z0-9]{0,15}",
    "build": r"[A-Za-z0-9._-]{1,64}", "digest": r"sha256:[0-9a-f]{64}",
}
_MATCH = {name: re.compile(f"(?:{pattern})") for name, pattern in PATTERNS.items()}

#: The board's progress events (board_progress_events), as its triggers write them: each type, its grammar, and the
#: field each group is. A row matching none is dropped and counted.
EVENTS: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    ("created", rf"({TICKET}) created in ({STAGE})", (("ticket", "ticket"), ("stage", "stage"))),
    ("moved", rf"({TICKET}) moved ({STAGE}) -> ({STAGE})", (("ticket", "ticket"), ("from", "stage"), ("to", "stage"))),
    ("assigned", rf"({TICKET}) assigned to ({ROLE})", (("ticket", "ticket"), ("role", "role"))),
    ("held", rf"({TICKET}) held", (("ticket", "ticket"),)),
    ("released", rf"({TICKET}) released from hold", (("ticket", "ticket"),)),
    ("parked", rf"({TICKET}) parked", (("ticket", "ticket"),)),
    ("unparked", rf"({TICKET}) unparked", (("ticket", "ticket"),)),
    ("candidate", rf"({TICKET}) candidate ([0-9a-f]{{7,40}})", (("ticket", "ticket"), ("commit", "commit"))),
    ("candidate_cleared", rf"({TICKET}) candidate ", (("ticket", "ticket"),)),
    ("blocker", rf"({TICKET}) blocker ({BLOCKER}) (set|removed|resolved|reopened)",
     (("ticket", "ticket"), ("blocker", "blocker"), ("change", "change"))),
    ("dependency_cleared", rf"({TICKET}) dependency cleared", (("ticket", "ticket"),)),
    ("waits", rf"({TICKET}) waits on ({ROLE})", (("ticket", "ticket"), ("role", "role"))),
    ("delivered", rf"({TICKET}) (transition|awaiting_role|publication) notice delivered to ({ROLE}|its owner)",
     (("ticket", "ticket"), ("notice", "delivered"), ("role", "owner"))),
    ("checkpoint", rf"({TICKET}) checkpoint by ({ROLE})", (("ticket", "ticket"), ("role", "role"))),
)
_EVENT_RE = tuple((kind, re.compile(grammar), fields) for kind, grammar, fields in EVENTS)
_FIELD_KIND = {"change": "set|removed|resolved|reopened", "delivered": "transition|awaiting_role|publication"}

CHOICES = ("inspect_manually", "move_work", "clear_hold")
#: The packet's content sections, in the order `verify` names what changed.
SECTIONS = ("hold", "other_holds", "affected", "choices", "roles", "tickets", "notifications", "events", "omitted")
MOVE_COMMAND = "switchyard set-role-runtime"
REQUIRES = "operator_confirms_account_safe"


def clear_command(runtime: str) -> str:
    return f"ticket-board-write clear-provider-account-hold {runtime} --reason-file PATH"


class PacketError(ValueError):
    """The packet does not validate: each problem, by path."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("recovery packet is not valid: " + "; ".join(problems[:12])
                         + (f"; and {len(problems) - 12} more" if len(problems) > 12 else ""))
        self.problems = problems


# -- the encoding -----------------------------------------------------------------------------------------------------
def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _ok(kind: str, value: Any) -> bool:
    return isinstance(value, str) and bool(_MATCH[kind].fullmatch(value))


def _time(value: Any) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("not an aware timestamp")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _opt_time(value: Any) -> str | None:
    return None if value is None else _time(value)


def _need(kind: str, value: Any) -> str:
    if not _ok(kind, value):
        raise ValueError(f"not a {kind}")
    return value


def _opt(kind: str, value: Any) -> str | None:
    return None if value in (None, "") else _need(kind, value)


def _int(value: Any, low: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or value > 2**63 - 1:
        raise ValueError("not a count")
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("not a flag")
    return value


def _ticket_key(ticket: str) -> tuple[str, int]:
    prefix, _, number = ticket.rpartition("-")
    return prefix, int(number)


# -- building ---------------------------------------------------------------------------------------------------------
def build(raw: dict[str, Any], *, hold: int | None = None) -> dict[str, Any]:
    """The packet for one current hold, from the rows `collect` read. Pure: the same rows give the same packet."""
    holds, bad_holds = _rows(raw.get("holds") or [], _hold_row)
    if bad_holds:
        raise ValueError("a current provider account hold does not read as one; no packet is built from it")
    holds.sort(key=lambda h: h["id"])
    if hold is None:
        if len(holds) != 1:
            raise ValueError("no provider account is held now; a recovery packet is for a current safety hold" if not holds
                             else "more than one provider account is held now; name one with hold=N: "
                                  + ", ".join(f"{h['id']} ({h['runtime']})" for h in holds))
        hold = holds[0]["id"]
    this = next((h for h in holds if h["id"] == hold), None)
    if this is None:
        raise ValueError(f"hold {hold} is not a current provider account hold")
    roles, bad_roles = _rows(raw.get("roles") or [], _role_row)
    registered = {r["role"]: r for r in _rows(raw.get("assignments") or [], _assignment_row)[0]}
    held_by = {h["runtime"]: h["id"] for h in holds}
    entries = []
    for r in roles:
        if not r["active"]:
            continue
        reg = registered.get(r["role"])
        entries.append({"role": r["role"], "runtime": r["runtime"], "registered": reg is not None,
                        "generation": reg["generation"] if reg else None,
                        "registered_at": reg["registered_at"] if reg else None,
                        "held_by": held_by.get(r["runtime"]) if r["runtime"] else None,
                        "controls": r["controls"], "may_clear": r["may_clear"]})
    entries.sort(key=lambda e: e["role"])
    # Every active role on a held account, before the cap: the notice read ranks by the same test.
    on_held = {e["role"] for e in entries if e["held_by"] is not None}
    omitted_roles = max(0, len(entries) - MAX_ROLES)
    entries = entries[:MAX_ROLES]
    held_roles = [e["role"] for e in entries if e["held_by"] == this["id"]]
    affected_roles = set(held_roles)

    tickets, bad_tickets = _rows(raw.get("tickets") or [], _ticket_row)
    blockers: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    bad_blockers = 0
    for row in raw.get("blockers") or []:
        try:
            blockers.setdefault(_need("ticket", row.get("ticket_id")), []).append(
                (_int(row.get("position")), {"id": _need("blocker", row.get("blocker_ticket_id")),
                                             "resolved": _bool(row.get("resolved"))}))
        except (ValueError, AttributeError):
            bad_blockers += 1
    for t in tickets:
        found = [b for _p, b in sorted(blockers.get(t["id"], []), key=lambda pb: (pb[0], pb[1]["id"]))]
        t["blockers"], t["blockers_omitted"] = found[:MAX_BLOCKERS], max(0, len(found) - MAX_BLOCKERS)
        t["affected"] = (t["assignee"] in affected_roles or t["awaiting"] in affected_roles or t["id"] == this["ticket"])
    ranked = sorted(tickets, key=lambda t: (not t["affected"], t["assignee"] == "unassigned", _ticket_key(t["id"])))
    kept = sorted(ranked[:MAX_TICKETS], key=lambda t: _ticket_key(t["id"]))

    notices, bad_notices = _rows(raw.get("notifications") or [], _notice_row)
    for n in notices:
        n["held"] = n["to"] in on_held
    ranked_notices = sorted(notices, key=lambda n: (not n["held"], n["id"]))
    kept_notices = sorted(ranked_notices[:MAX_NOTIFICATIONS], key=lambda n: n["id"])
    notice_total = max(_count(raw.get("notification_total")), len(notices) + bad_notices)

    window = list(raw.get("events") or [])
    events, bad_events = _rows(window, _event_row)
    events = sorted(events, key=lambda e: e["id"])[-MAX_EVENTS:]
    event_total = max(_count(raw.get("event_total")), len(window))

    unheld = [e for e in entries if e["held_by"] is None]
    content = {
        "schema": SCHEMA, "version": VERSION,
        "hold": {**{k: this[k] for k in ("id", "runtime", "kind", "category", "source", "role", "ticket", "began_at",
                                         "began_by", "refusals")},
                 "director_notified": this["notified"], "roles": held_roles,
                 "director_held": any(e["controls"] and e["held_by"] == this["id"] for e in entries)},
        "other_holds": [{"id": h["id"], "runtime": h["runtime"], "kind": h["kind"], "began_at": h["began_at"],
                         "roles": [e["role"] for e in entries if e["held_by"] == h["id"]]}
                        for h in holds if h["id"] != this["id"]][:MAX_OTHER_HOLDS],
        "affected": {"roles": held_roles, "tickets": [t["id"] for t in kept if t["affected"]]},
        "choices": [
            {"choice": "inspect_manually"},
            {"choice": "move_work", "command": MOVE_COMMAND, "roles": held_roles,
             "to": sorted({e["runtime"] for e in unheld if e["runtime"]})},
            {"choice": "clear_hold", "command": clear_command(this["runtime"]), "requires": REQUIRES,
             "by": [e["role"] for e in unheld if e["may_clear"]]},
        ],
        "roles": entries,
        "tickets": kept,
        "notifications": kept_notices,
        "events": events,
        "omitted": {"roles": omitted_roles, "tickets": len(tickets) - len(kept),
                    "notifications": notice_total - len(kept_notices) - bad_notices,
                    "events": event_total - len(window),
                    "malformed": {"roles": bad_roles, "tickets": bad_tickets, "blockers": bad_blockers,
                                  "notifications": bad_notices, "events": bad_events}},
    }
    high = raw.get("high_water") or {}
    packet = {**content, "provenance": {
        "project": _need("project", raw.get("project")), "ticket_prefix": _need("prefix", raw.get("ticket_prefix")),
        "build": raw.get("build") if _ok("build", raw.get("build")) else "unknown",
        "generated_at": _time(raw.get("generated_at")), "db_xmax": _int(raw.get("db_xmax")),
        "high_water": {name: _count(high.get(name)) for name in ("event", "notification", "hold")},
        "state_digest": digest(content)}}
    packet["packet_digest"] = digest(packet)
    size = len(canonical(packet))
    if size > MAX_BYTES:  # unreachable while the caps hold; never hand out an unbounded packet
        raise ValueError(f"recovery packet is {size} bytes, over its {MAX_BYTES}-byte bound")
    return packet


def _count(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _rows(rows: Iterable[Any], read: Callable[[dict[str, Any]], dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    good, bad = [], 0
    for row in rows:
        try:
            good.append(read(row))
        except (ValueError, TypeError, AttributeError, KeyError):
            bad += 1
    return good, bad


def _hold_row(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": _int(row["id"], 1), "runtime": _need("runtime", row["runtime"]), "kind": _need("hold_kind", row["kind"]),
            "category": _need("category", row.get("category") or ""), "source": _need("source", row.get("source") or ""),
            "role": _opt("role", row.get("role")), "ticket": _opt("ticket", row.get("ticket_id")),
            "began_at": _time(row["began_at"]), "began_by": _need("role", row["began_by"]),
            "refusals": _int(row.get("refusals") or 0), "notified": _bool(row["notified_director"])}


def _role_row(row: dict[str, Any]) -> dict[str, Any]:
    return {"role": _need("role", row["name"]), "active": _bool(row["active"]), "runtime": _opt("runtime", row.get("runtime")),
            "controls": _bool(row["controls"]), "may_clear": _bool(row["may_clear"])}


def _assignment_row(row: dict[str, Any]) -> dict[str, Any]:
    return {"role": _need("role", row["role"]), "generation": _int(row["generation"], 1),
            "registered_at": _time(row["registered_at"])}


def _ticket_row(row: dict[str, Any]) -> dict[str, Any]:
    commit = row.get("commit_hash") or None
    return {"id": _need("ticket", row["id"]), "stage": _need("stage", row["state"]), "assignee": _need("role", row["assignee"]),
            "parent": _opt("ticket", row.get("parent_id")), "awaiting": _opt("role", row.get("awaiting_role")),
            "awaiting_since": _opt_time(row.get("awaiting_since_at")) if row.get("awaiting_role") else None,
            "in_stage_since": _opt_time(row.get("entered_current_state_at")),
            "manually_controlled": _bool(row["manually_controlled"]), "parked": _bool(row["parked"]),
            "commit": _opt("commit", commit.lower() if isinstance(commit, str) else commit)}


def _notice_row(row: dict[str, Any]) -> dict[str, Any]:
    return {"id": _int(row["id"], 1), "ticket": _need("ticket", row["ticket_id"]), "kind": _need("notice", row["kind"]),
            "to": _need("role", row["target_role"]), "about": _opt("about", row.get("payload_kind")),
            "attempts": _int(row["attempts"]), "created_at": _time(row["created_at"]),
            "next_attempt_at": _time(row["next_attempt_at"]), "claimed": _bool(row["claimed"])}


def _event_row(row: dict[str, Any]) -> dict[str, Any]:
    text = row["event"]
    if not isinstance(text, str):
        raise ValueError("not an event")
    for kind, grammar, fields in _EVENT_RE:
        found = grammar.fullmatch(text)
        if found:
            event = {"id": _int(row["id"], 1), "at": _time(row["at"]), "type": kind}
            for (name, _what), value in zip(fields, found.groups()):
                event[name] = None if value == "its owner" else value
            if event["ticket"] != row.get("ticket_id"):
                raise ValueError("an event about another ticket than its row's")
            return event
    raise ValueError("not an event the board writes")


# -- validating -------------------------------------------------------------------------------------------------------
def _p(kind: str, *, null: bool = False) -> tuple[str, str, bool]:
    return ("str", kind, null)


SPEC: dict[str, Any] = {
    "hold": {"id": ("int", 1), "runtime": _p("runtime"), "kind": _p("hold_kind"), "category": _p("category"),
             "source": _p("source"), "role": _p("role", null=True), "ticket": _p("ticket", null=True),
             "began_at": _p("time"), "began_by": _p("role"), "refusals": ("int", 0), "director_notified": ("bool",),
             "director_held": ("bool",), "roles": ("list", _p("role"), MAX_ROLES)},
    "other_holds": ("list", {"id": ("int", 1), "runtime": _p("runtime"), "kind": _p("hold_kind"), "began_at": _p("time"),
                             "roles": ("list", _p("role"), MAX_ROLES)}, MAX_OTHER_HOLDS),
    "affected": {"roles": ("list", _p("role"), MAX_ROLES), "tickets": ("list", _p("ticket"), MAX_TICKETS)},
    "roles": ("list", {"role": _p("role"), "runtime": _p("runtime", null=True), "registered": ("bool",),
                       "generation": ("int", 1, True), "registered_at": _p("time", null=True),
                       "held_by": ("int", 1, True), "controls": ("bool",), "may_clear": ("bool",)}, MAX_ROLES),
    "tickets": ("list", {"id": _p("ticket"), "stage": _p("stage"), "assignee": _p("role"), "parent": _p("ticket", null=True),
                         "blockers": ("list", {"id": _p("blocker"), "resolved": ("bool",)}, MAX_BLOCKERS),
                         "blockers_omitted": ("int", 0), "awaiting": _p("role", null=True),
                         "awaiting_since": _p("time", null=True), "in_stage_since": _p("time", null=True),
                         "manually_controlled": ("bool",), "parked": ("bool",), "commit": _p("commit", null=True),
                         "affected": ("bool",)}, MAX_TICKETS),
    "notifications": ("list", {"id": ("int", 1), "ticket": _p("ticket"), "kind": _p("notice"), "to": _p("role"),
                               "about": _p("about", null=True), "attempts": ("int", 0), "created_at": _p("time"),
                               "next_attempt_at": _p("time"), "claimed": ("bool",), "held": ("bool",)}, MAX_NOTIFICATIONS),
    "omitted": {"roles": ("int", 0), "tickets": ("int", 0), "notifications": ("int", 0), "events": ("int", 0),
                "malformed": {name: ("int", 0) for name in ("roles", "tickets", "blockers", "notifications", "events")}},
    "provenance": {"project": _p("project"), "ticket_prefix": _p("prefix"), "build": _p("build"),
                   "generated_at": _p("time"), "db_xmax": ("int", 0),
                   "high_water": {name: ("int", 0) for name in ("event", "notification", "hold")},
                   "state_digest": _p("digest")},
}


def _check(spec: Any, value: Any, where: str, problems: list[str]) -> None:
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            problems.append(f"{where}: not an object")
            return
        for key in sorted(set(value) - set(spec)):
            problems.append(f"{where}.{key}: not a field of this schema")
        for key in sorted(set(spec) - set(value)):
            problems.append(f"{where}.{key}: missing")
        for key in sorted(set(spec) & set(value)):
            _check(spec[key], value[key], f"{where}.{key}", problems)
        return
    kind = spec[0]
    if kind == "str":
        if not (value is None and spec[2]) and not _ok(spec[1], value):
            problems.append(f"{where}: not a {spec[1]}")
    elif kind == "int":
        if not (value is None and len(spec) > 2 and spec[2]) and (
                isinstance(value, bool) or not isinstance(value, int) or not spec[1] <= value <= 2**63 - 1):
            problems.append(f"{where}: not an integer from {spec[1]}")
    elif kind == "bool":
        if not isinstance(value, bool):
            problems.append(f"{where}: not true or false")
    elif kind == "list":
        if not isinstance(value, list):
            problems.append(f"{where}: not a list")
            return
        if len(value) > spec[2]:
            problems.append(f"{where}: {len(value)} entries, over its cap of {spec[2]}")
        for index, item in enumerate(value[:spec[2]]):
            _check(spec[1], item, f"{where}[{index}]", problems)


def _sorted(values: list[Any], key: Callable[[Any], Any]) -> bool:
    try:
        keys = [key(v) for v in values]
    except (TypeError, KeyError, ValueError, AttributeError):
        return False
    return all(a < b for a, b in zip(keys, keys[1:]))


def validate(packet: Any) -> dict[str, Any]:
    """Check a packet against this schema, field by field, and its two digests; answer it, or raise PacketError."""
    problems: list[str] = []
    if not isinstance(packet, dict):
        raise PacketError(["the packet is not an object"])
    if packet.get("schema") != SCHEMA or packet.get("version") != VERSION:
        raise PacketError([f"not a {SCHEMA} version {VERSION} packet: {str(packet.get('schema'))[:64]!r} "
                           f"version {str(packet.get('version'))[:16]!r}"])
    top = {"schema", "version", "choices", "events", "packet_digest", *SPEC}
    problems += [f"{key}: not a field of this schema" for key in sorted(set(packet) - top)]
    problems += [f"{key}: missing" for key in sorted(top - set(packet))]
    for key in SPEC:
        if key in packet:
            _check(SPEC[key], packet[key], key, problems)
    try:
        size = len(canonical(packet))
    except (TypeError, ValueError):
        raise PacketError(problems + ["the packet does not encode as JSON"]) from None
    if size > MAX_BYTES:
        problems.append(f"the packet is {size} bytes, over its {MAX_BYTES}-byte bound")
    if not problems:
        problems += _check_events(packet["events"]) + _check_meaning(packet)
    if not problems:
        content = {k: v for k, v in packet.items() if k not in ("provenance", "packet_digest")}
        if packet["provenance"]["state_digest"] != digest(content):
            problems.append("provenance.state_digest: does not match the packet's content")
        if packet.get("packet_digest") != digest({k: v for k, v in packet.items() if k != "packet_digest"}):
            problems.append("packet_digest: does not match the packet")
    if problems:
        raise PacketError(problems)
    return packet


def _check_events(events: Any) -> list[str]:
    if not isinstance(events, list):
        return ["events: not a list"]
    problems = [f"events: {len(events)} entries, over its cap of {MAX_EVENTS}"] if len(events) > MAX_EVENTS else []
    grammar = {kind: fields for kind, _g, fields in EVENTS}
    for index, event in enumerate(events[:MAX_EVENTS]):
        fields = grammar.get(event.get("type")) if isinstance(event, dict) else None
        if fields is None:
            problems.append(f"events[{index}]: not an event type of this schema")
            continue
        want = {"id", "at", "type", *(name for name, _w in fields)}
        if set(event) != want:
            problems.append(f"events[{index}]: fields {sorted(event)}, not {sorted(want)}")
            continue
        if not isinstance(event["id"], int) or isinstance(event["id"], bool) or event["id"] < 1 or not _ok("time", event["at"]):
            problems.append(f"events[{index}]: not an id and a time")
        for name, what in fields:
            value = event[name]
            if what == "owner":
                ok = value is None or _ok("role", value)
            elif what in _FIELD_KIND:
                ok = isinstance(value, str) and re.fullmatch(_FIELD_KIND[what], value) is not None
            else:
                ok = _ok(what, value)
            if not ok:
                problems.append(f"events[{index}].{name}: not a {what}")
    if not problems and not _sorted(events, lambda e: e["id"]):
        problems.append("events: not in id order")
    return problems


def _check_meaning(packet: dict[str, Any]) -> list[str]:
    """What the field checks cannot see: fixed order, one consistent picture, and the fixed choice vocabulary."""
    problems = []
    hold, roles, tickets = packet["hold"], packet["roles"], packet["tickets"]
    for name, values, key in (("roles", roles, lambda r: r["role"]), ("tickets", tickets, lambda t: _ticket_key(t["id"])),
                              ("notifications", packet["notifications"], lambda n: n["id"]),
                              ("other_holds", packet["other_holds"], lambda h: h["id"]),
                              ("hold.roles", hold["roles"], lambda r: r),
                              ("affected.tickets", packet["affected"]["tickets"], _ticket_key)):
        if not _sorted(values, key):
            problems.append(f"{name}: not in its fixed order, or repeated")
    held = [r["role"] for r in roles if r["held_by"] == hold["id"]]
    if hold["roles"] != held or packet["affected"]["roles"] != held:
        problems.append("hold.roles: not the roles held by this hold")
    if packet["affected"]["tickets"] != [t["id"] for t in tickets if t["affected"]]:
        problems.append("affected.tickets: not the tickets marked affected")
    if hold["director_held"] != any(r["controls"] and r["held_by"] == hold["id"] for r in roles):
        problems.append("hold.director_held: not what the roles say")
    if any(h["id"] == hold["id"] for h in packet["other_holds"]):
        problems.append("other_holds: names this hold")
    unheld = [r for r in roles if r["held_by"] is None]
    want = [{"choice": "inspect_manually"},
            {"choice": "move_work", "command": MOVE_COMMAND, "roles": held,
             "to": sorted({r["runtime"] for r in unheld if r["runtime"]})},
            {"choice": "clear_hold", "command": clear_command(hold["runtime"]), "requires": REQUIRES,
             "by": [r["role"] for r in unheld if r["may_clear"]]}]
    if packet["choices"] != want:
        problems.append("choices: not the board's fixed recovery choices for this hold")
    return problems


# -- consuming --------------------------------------------------------------------------------------------------------
def verify(packet: Any, *, project: str, fresh: Any) -> dict[str, Any]:
    """A recovery consumer's check: the packet validates, is this project's, and is for a hold that is still current;
    and whether the board still reads as it did. `fresh` is a packet the consumer fetched itself, now, over its own
    transport, for the same hold: its authority is that transport's. Raises PacketError when it cannot be used."""
    validate(packet)
    try:
        validate(fresh)
    except PacketError as exc:
        raise PacketError(["the board's own packet now: " + p for p in exc.problems]) from None
    problems = []
    for name, value in (("packet", packet), ("the board's packet now", fresh)):
        if value["provenance"]["project"] != project:
            problems.append(f"{name} is project {value['provenance']['project']!r}, not {project!r}")
    if fresh["hold"]["id"] != packet["hold"]["id"]:
        problems.append(f"the board answered for hold {fresh['hold']['id']}, not hold {packet['hold']['id']}")
    if fresh["provenance"]["generated_at"] < packet["provenance"]["generated_at"]:
        problems.append("the board's packet now is older than the packet")
    if problems:
        raise PacketError(problems)
    changed = [key for key in SECTIONS if packet[key] != fresh[key]]
    return {"hold": packet["hold"]["id"], "runtime": packet["hold"]["runtime"],
            "generated_at": packet["provenance"]["generated_at"], "state_digest": packet["provenance"]["state_digest"],
            "current": packet["provenance"]["state_digest"] == fresh["provenance"]["state_digest"], "changed": changed}


def fetch_verified(get: Callable[[str], Any], *, project: str, packet: Any = None, hold: int | None = None) -> dict[str, Any]:
    """Fetch the board's packet through `get` (any GET transport: the read client, or root's proven socket) and verify
    `packet` against it -- or, with no packet, verify the one fetched against a second read."""
    if packet is None:
        packet = get(PATH + (f"?hold={int(hold)}" if hold is not None else ""))
    held = packet.get("hold") if isinstance(packet, dict) else None
    hold_id = held.get("id") if isinstance(held, dict) else None
    if not isinstance(hold_id, int) or isinstance(hold_id, bool):
        validate(packet)  # raises, naming why
    fresh = get(f"{PATH}?hold={hold_id}")
    return {"packet": packet, **verify(packet, project=project, fresh=fresh)}


# -- reading the board ------------------------------------------------------------------------------------------------
def collect(conn: Any) -> dict[str, Any]:
    """Read the trusted rows a packet is built from, in one REPEATABLE READ, READ ONLY transaction: one snapshot."""
    def rows(sql: str, *args: Any) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute(sql, args or None).fetchall()]

    def one(sql: str) -> Any:
        found = conn.execute(sql).fetchone()
        return next(iter(found.values())) if isinstance(found, dict) else found[0]

    with conn.transaction():
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        now = conn.execute("SELECT now() AS at, pg_snapshot_xmax(pg_current_snapshot())::text::bigint AS xmax").fetchone()
        raw = {"generated_at": now["at"], "db_xmax": now["xmax"]}
        raw["holds"] = rows(
            "SELECT h.id, h.runtime, h.kind, h.category, h.source, h.role, h.ticket_id, h.began_at, h.began_by,"
            " h.notified_director, (SELECT count(*) FROM ticket_board.provider_refusal_evidence e"
            " WHERE e.hold_id = h.id)::int AS refusals"
            " FROM ticket_board.provider_account_holds h WHERE h.cleared_at IS NULL ORDER BY h.id")
        # Who controls the project and who may clear a hold, as role_controls_project and provider_account_hold_actor
        # decide them: the control capabilities, set_manually_controlled, or the User's kind; by name with no workflow.
        raw["roles"] = rows(
            "SELECT w.name, coalesce((w.definition->>'active')::boolean, false) AS active,"
            " ticket_board.role_account_runtime(w.name) AS runtime, c.controls,"
            " (c.controls OR coalesce(w.definition->'capabilities' ? 'set_manually_controlled', false)"
            "  OR coalesce(w.definition->>'kind', '') = 'user' OR (c.legacy AND w.name IN ('director', 'user'))) AS may_clear"
            " FROM ticket_board.workflow_roles w, LATERAL (SELECT ticket_board.declared_workflow() IS NULL AS legacy) l,"
            " LATERAL (SELECT l.legacy, CASE WHEN l.legacy THEN w.name = 'director' ELSE"
            "  coalesce((w.definition->>'active')::boolean, false)"
            "  AND coalesce(w.definition->'capabilities' ?& ARRAY['set_manually_controlled', 'merge'], false) END AS controls) c"
            " ORDER BY w.name")
        raw["assignments"] = rows("SELECT role, generation::bigint AS generation, registered_at"
                                  " FROM ticket_board.role_runtime_assignments ORDER BY role")
        terminal = ("coalesce((SELECT array_agg(s->>'name') FROM jsonb_array_elements("
                    "coalesce(ticket_board.declared_workflow()->'stages', '[]'::jsonb)) s"
                    " WHERE coalesce((s->>'terminal')::boolean, false)), ARRAY['done', 'cancelled'])")
        raw["tickets"] = rows(
            "SELECT t.id, t.state, t.assignee, t.parent_id, t.manually_controlled, t.parked, t.commit_hash,"
            " ns.awaiting_role, ns.awaiting_since_at, ns.entered_current_state_at"
            " FROM ticket_board.tickets t LEFT JOIN ticket_board.ticket_notification_state ns ON ns.ticket_id = t.id"
            f" WHERE t.state <> ALL ({terminal}) ORDER BY t.id")
        raw["blockers"] = rows(
            "SELECT b.ticket_id, b.blocker_ticket_id, b.resolved, b.position FROM ticket_board.ticket_blockers b"
            f" JOIN ticket_board.tickets t ON t.id = b.ticket_id WHERE t.state <> ALL ({terminal})"
            " ORDER BY b.ticket_id, b.position")
        # The held targets' first (an active role on a held account), then the oldest: the same order `build` keeps
        # them in, so its cap reads what it keeps.
        raw["notifications"] = rows(
            "SELECT q.id, q.ticket_id, q.kind, q.target_role, q.payload->>'kind' AS payload_kind, q.attempts,"
            " q.created_at, q.next_attempt_at, q.claimed_at IS NOT NULL AS claimed"
            " FROM ticket_board.ticket_notification_queue q WHERE q.dead_lettered_at IS NULL"
            " ORDER BY NOT EXISTS (SELECT FROM ticket_board.provider_account_holds h WHERE h.cleared_at IS NULL"
            "  AND h.runtime = ticket_board.role_account_runtime(q.target_role)"
            "  AND EXISTS (SELECT FROM ticket_board.workflow_roles w WHERE w.name = q.target_role"
            "   AND coalesce((w.definition->>'active')::boolean, false))), q.id LIMIT %s", MAX_NOTIFICATIONS)
        raw["notification_total"] = one("SELECT count(*)::bigint FROM ticket_board.ticket_notification_queue"
                                        " WHERE dead_lettered_at IS NULL")
        raw["events"] = rows("SELECT id, at, ticket_id, event FROM ticket_board.board_progress_events"
                             " ORDER BY id DESC LIMIT %s", MAX_EVENTS)
        raw["event_total"] = one("SELECT count(*)::bigint FROM ticket_board.board_progress_events")
        raw["high_water"] = {
            "event": one("SELECT coalesce(max(id), 0)::bigint FROM ticket_board.board_progress_events"),
            "notification": one("SELECT coalesce(max(id), 0)::bigint FROM ticket_board.ticket_notification_queue"),
            "hold": one("SELECT coalesce(max(id), 0)::bigint FROM ticket_board.provider_account_holds")}
    return raw


@functools.lru_cache(maxsize=1)
def _build_id() -> str:
    from .build_identity import board_build_id

    found = board_build_id()
    return found if _ok("build", found) else "unknown"


def serve(app: Any, query: dict[str, list[str]]) -> dict[str, Any]:
    """GET /api/recovery-packet[?hold=N]: the packet for the current hold, or the one named. Read-only."""
    wanted = (query.get("hold") or [""])[0].strip()
    if wanted and not wanted.isdigit():
        raise ValueError("hold must be a hold's number")
    with app._pg_connect() as conn:
        raw = collect(conn)
    raw.update(project=str(getattr(app, "project", "")), ticket_prefix=str(getattr(app, "ticket_prefix", "")),
               build=_build_id())
    return build(raw, hold=int(wanted) if wanted else None)


# -- rendering --------------------------------------------------------------------------------------------------------
def render(packet: dict[str, Any]) -> str:
    """The packet as plain text, from its validated fields alone: no provider, no model, nothing interpreted."""
    validate(packet)
    hold, prov, omitted = packet["hold"], packet["provenance"], packet["omitted"]
    why = (f"{hold['role'] or 'a role'}'s provider refused on safety grounds" + (f" ({hold['category']})" if hold["category"] else "")
           if hold["kind"] == "refusal" else f"held by hand by {hold['began_by']} as a suspected refusal")
    lines = [f"Recovery packet for hold {hold['id']}: the {hold['runtime']} account, since {hold['began_at']}: {why}.",
             f"  {prov['project']} ({prov['ticket_prefix']}), built {prov['generated_at']} by board build {prov['build']},"
             f" database xmax {prov['db_xmax']}; state {prov['state_digest']}",
             f"  holds: {', '.join(hold['roles']) or 'no role now'}"
             + ("; the Director is held, and was not told" if hold["director_held"] else "")
             + (f"; {hold['refusals']} refusal(s) taken" if hold["refusals"] else ""),
             "  affected tickets: " + (", ".join(packet["affected"]["tickets"]) or "none")]
    for h in packet["other_holds"]:
        lines.append(f"  also held: hold {h['id']}, the {h['runtime']} account ({', '.join(h['roles']) or 'no role now'})")
    lines.append("Choices:")
    for c in packet["choices"]:
        if c["choice"] == "inspect_manually":
            lines.append("  - inspect by hand; nothing asks the held account anything meanwhile")
        elif c["choice"] == "move_work":
            lines.append(f"  - move the work to another provider ({c['command']}): {', '.join(c['roles']) or 'no role'}"
                         f" -> {', '.join(c['to']) or 'no unheld provider is configured'}")
        else:
            lines.append(f"  - once the operator confirms the account is safe, clear it: {c['command']}"
                         f" (by {', '.join(c['by']) or 'no unheld role: the operator'})")
    lines.append("Roles:")
    for r in packet["roles"]:
        lines.append(f"  {r['role']}: {r['runtime'] or 'no runtime'}" + (f" generation {r['generation']}" if r["registered"] else ", not running")
                     + (f", HELD by hold {r['held_by']}" if r["held_by"] else ""))
    lines.append("Tickets:")
    for t in packet["tickets"]:
        extra = [f"blocked by {', '.join(b['id'] + (' (resolved)' if b['resolved'] else '') for b in t['blockers'])}"] if t["blockers"] else []
        extra += [f"waits on {t['awaiting']}"] if t["awaiting"] else []
        extra += ["held by the Director"] if t["manually_controlled"] else []
        extra += ["parked"] if t["parked"] else []
        extra += [f"candidate {t['commit'][:12]}"] if t["commit"] else []
        lines.append(f"  {'*' if t['affected'] else ' '}{t['id']} [{t['stage']}/{t['assignee']}]" + (f" {'; '.join(extra)}" if extra else ""))
    lines.append("Pending notifications:")
    for n in packet["notifications"]:
        lines.append(f"  {n['id']}: {n['kind']} on {n['ticket']} to {n['to']}" + (f" ({n['about']})" if n["about"] else "")
                     + f", {n['attempts']} attempt(s), next {n['next_attempt_at']}" + (", HELD" if n["held"] else ""))
    lines.append("Recent board events:")
    for e in packet["events"]:
        detail = " ".join(f"{k}={e[k]}" for k in sorted(e) if k not in ("id", "at", "type", "ticket") and e[k] is not None)
        lines.append(f"  {e['at']} {e['ticket']} {e['type']}" + (f" {detail}" if detail else ""))
    left = {k: v for k, v in omitted.items() if k != "malformed" and v}
    bad = {k: v for k, v in omitted["malformed"].items() if v}
    if left or bad:
        lines.append("Left out: " + ", ".join([f"{v} more {k}" for k, v in left.items()]
                                              + [f"{v} unreadable {k}" for k, v in bad.items()]))
    return "\n".join(lines)
