#!/usr/bin/env python3
"""SYRD-672: a provider's safety refusal holds every role on that provider account, until a person clears it.

No provider is ever contacted, and nothing here is a refusal-provoking request: each refusal is a stand-in, in the
shape the installed CLIs write one -- Claude Code's transcript entry ending on `stop_reason: "refusal"`, fed to the
real hook program by the pane's own process, and Codex's own cybersecurity line on a stand-in screen read by the real
gate through a fake tmux. The board is production-built (schema, rbac, every migration), and the listener, gate and
hook are the real ones (SYRD-644's rig, whose listener is a new one each pass: every pass is a restart).

The fixture's roles: the Director, app and ops on Claude, main on Codex; audit, inspector and designer declared on
Claude but not running.

* Codex refuses main: one hold on the codex account, one Director notice, one operator event; nothing more reaches main
  -- not a comment's notice, a hand-over, a reminder, a nudge -- across passes, listener restarts and a new main
  process, and no account-wide expiry or new registration ends it. Router and a worker's own claim refuse main and
  keep app. The ticket moves to app, on another account, with its board state, and main is told nothing.
* Clearing: refused to an implementer, and without a reason; the Director's clear records who and why, and only then
  does main's waiting work go. The refusal still on the screen afterwards holds nothing again; a new turn's does.
* Claude refuses app: the claude account holds app, ops, the Director and the declared-but-stopped roles. The
  Director's own account is held, so the Director is told nothing and the operator's event is the one notice; no
  refused text is kept anywhere. The User clears it.
* Quota, overload, authentication and Codex's capacity error make no account hold.
* By hand: the Director or the User holds an account on a suspicion; repeating it changes nothing.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / "tests")]

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import codex_capacity_retry_test as c594  # noqa: E402
import provider_quota_hold_test as s644  # noqa: E402
from scripts.ticket_board import provider_refusal  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

HOOK = ROOT / "scripts/ticket-board-pane-idle-hook"
TARGETS = s644.TARGETS
SECRET = "PRIVATE-REFUSED-EXCHANGE"
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def codex_screen_of(turns: list[tuple[str, str]]) -> dict:
    """A Codex screen as it draws one: SYRD-594's measured header and composer, and each turn's prompt echo, then its
    outcome -- Codex's own cybersecurity line ("refused"), a reply ("ok"), or still working ("working")."""
    rows = c594.screens()["capacity"]["rows"]
    body: list[str] = []
    for prompt, outcome in turns:
        body += [f"› {prompt}", ""] + {"refused": [f"■ {provider_refusal.CODEX_REFUSAL_LINES[0]} {SECRET}", ""],
                                       "ok": ["• Done.", ""], "working": ["• Working (3s • esc to interrupt)"]}[outcome]
    top = (rows[:5] + body)[-41:]
    drawn = top + [""] * (41 - len(top)) + rows[41:]
    return {"rows": drawn, "joined": drawn, "cursor": [2, 41]}


def codex_refusal_screen() -> dict:
    """The one refused turn: "say hi", and Codex's own cybersecurity line."""
    return codex_screen_of([("say hi", "refused")])


class Rig(s644.Rig):
    def json(self, statement: str):
        return json.loads(self.sql(statement) or "null")

    def holds(self) -> list[dict]:
        return self.json("SELECT coalesce(json_agg(h ORDER BY h.id), '[]')::text FROM ticket_board.provider_account_holds h;")

    def events(self) -> list[dict]:
        return self.json("SELECT coalesce(json_agg(e ORDER BY e.id), '[]')::text FROM ticket_board.user_events e "
                         "WHERE kind = 'provider_safety_hold';")

    def may(self, operation: str, caller: str, **body) -> dict:
        """An operation that must be allowed: a refusal is this suite's failed check, not the fixture's assertion."""
        try:
            return self.op(operation, caller=caller, **body)["hold"]
        except AssertionError as exc:
            return {"refused": str(exc)[:300]}

    def held_notices(self) -> list[str]:
        return [m for _at, role, m in self.sends if role == "director" and "account held" in m]

    def claude_refusal(self, role: str, *, source: str = "claude.Stop", uuid: str = "r-1") -> None:
        """Claude Code's turn ending on a refusal, through the real hook program run by the pane's own process."""
        transcript = self.root / f"{role}-{time.time_ns()}.jsonl"
        records = [{"type": "user", "uuid": "u-1", "message": {"role": "user", "content": SECRET}},
                   {"type": "assistant", "uuid": uuid, "timestamp": "2026-11-07T12:00:00.000Z",
                    "message": {"role": "assistant", "stop_reason": "refusal", "stop_details": {"category": "cyber"},
                                "content": [{"type": "text", "text": SECRET}]}}]
        transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
        state = "idle" if source == "claude.Stop" else "busy"
        payload = {"session_id": "s-1", "transcript_path": str(transcript), "last_assistant_message": SECRET,
                   "hook_event_name": source.split(".")[1], **({"error": "invalid_request"} if source.endswith("Failure") else {})}
        self.providers[role].run([sys.executable, str(HOOK), state, "--source", source, "--target", TARGETS[role],
                                  "--state-dir", str(self.state_dir), "--session-dir", str(self.session_dir)],
                                 json.dumps(payload), {"PATH": os.environ["PATH"], "HOME": str(self.root)})


def scenario_codex_refusal_holds_its_account(rig: Rig) -> None:
    rig.start("PGU-1", "main")
    rig.passes(1.5)
    check(any("PGU-1" in m for m in rig.to("main", 0)), f"main is handed PGU-1 first: {rig.sends}")
    # main's provider refuses its next turn: the turn starts (the real hook, from main's process), and Codex draws
    # its own cybersecurity line and runs no Stop hook.
    rig.turn("main")
    rig.panes["main"].screen = codex_refusal_screen()
    mark = time.monotonic()
    rig.passes(1.5)
    holds = rig.holds()
    check(len(holds) == 1 and holds[0]["runtime"] == "codex" and holds[0]["kind"] == "refusal" and holds[0]["role"] == "main"
          and holds[0]["category"] == "cyber" and holds[0]["source"] == "codex.screen"
          and holds[0]["evidence"].startswith(f"codex:{rig.providers['main'].pid}:") and holds[0]["cleared_at"] is None,
          f"the refusal on main's screen, after a turn main's own process took, holds the codex account: {holds}")
    check(SECRET not in json.dumps(holds), "the hold keeps the refusal's classification and identity, never its words")
    check(rig.sql("SELECT ticket_board.role_provider_hold_until('main', clock_timestamp()) IS NOT NULL "
                  "AND ticket_board.role_provider_hold_is_current('main', clock_timestamp()) "
                  "AND ticket_board.role_provider_hold_until('app', clock_timestamp()) IS NULL;") == "t",
          "SYRD-644's two questions say main is held -- so alerts about it are dropped -- and app is not")
    notices = rig.held_notices()
    check(len(notices) == 1 and "codex account held" in notices[0] and "main" in notices[0]
          and "clear-provider-account-hold codex" in notices[0] and "set-role-runtime" in notices[0] and SECRET not in notices[0],
          f"the Director is told once: which account, the roles it holds, and the choices: {notices}")
    check(len(rig.events()) == 1 and rig.events()[0]["detail"] == "codex" and rig.events()[0]["ticket_id"] is None,
          f"the operator gets one board-wide event for the phone bridge: {rig.events()}")
    check(rig.to("main", mark) == [], f"the refused pane is not nudged in the same wave: {rig.to('main', mark)}")
    # The board re-checks the process itself: a report from any process but the registered one is stale.
    reg = rig.json("SELECT json_build_object('pid', process_pid, 'start', process_start_time)::text "
                   "FROM ticket_board.role_runtime_assignments WHERE role = 'app';")
    for pid, start, runtime in ((1, reg["start"], "claude"), (reg["pid"], 1, "claude"), (reg["pid"], reg["start"], "codex")):
        answer = json.loads(rig.sql(
            "SET ROLE ticket_board_listener;\nSELECT ticket_board.note_provider_refusal('app', jsonb_build_object("
            f"'runtime', '{runtime}', 'pid', {pid}, 'start_time', {start}, 'evidence', 'claude:forged-{pid}-{start}'))::text;"
        ).splitlines()[-1])
        check(answer.get("stale") is True, f"another pid, another start, or another runtime is not app's process: {answer}")
    check(len(rig.holds()) == 1, "and holds nothing")

    # While held: a comment's notice, a new hand-over, reminders and nudges, many passes, each a listener restart.
    mark = time.monotonic()
    rig.act("PGU-1", "add_comment", text="Director: please try again")
    rig.start("PGU-2", "main")
    rig.passes(3.0)
    waiting = rig.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE target_role = 'main' "
                      "AND last_error LIKE 'provider_account_hold%';")
    check(rig.to("main", mark) == [] and waiting != "0",
          f"nothing reaches main: its notices wait, requeued, not dropped ({waiting}): {rig.to('main', mark)}")
    check(rig.sql("SELECT ticket_board.ticket_turn_is_resolved('PGU-1', clock_timestamp());") == "t",
          "reminders, escalations and stall notices ask the board's one question, and main's turn is not news")
    check(len(rig.held_notices()) == 1 and len(rig.holds()) == 1 and len(rig.events()) == 1,
          f"across listener restarts: still one hold, one Director notice, one event: {rig.held_notices()}")

    # An alert about main for the Director: the hold's notice said it all, so it is dropped, not delivered.
    rig.sql("SELECT ticket_board.enqueue_notification('PGU-2', 'escalation', 'director', 'main was reminded about PGU-2 "
            "and still has not advanced it -- may be stuck.', jsonb_build_object('kind', 'escalation', 'id', 'PGU-2', "
            "'state', 'in_progress', 'assignee', 'main', 'target_role', 'director'), 'syrd672-about-main');")
    rig.passes(1.5)
    check(not any("may be stuck" in m for m in rig.to("director", mark)),
          f"no alert about a held role reaches the Director: {rig.to('director', mark)}")

    # A new main process -- a restart, a new registration -- neither ends the hold nor begins another.
    rig.register("main", s644.Provider(rig.root))
    rig.passes(1.5)
    check(rig.holds()[0]["cleared_at"] is None and len(rig.holds()) == 1 and rig.to("main", mark) == []
          and len(rig.held_notices()) == 1,
          f"a restart or a new registration does not clear it, and the old screen holds nothing new: {rig.holds()}")

    # Router and a worker's own claim go around main, to the eligible owners.
    refused = rig.sql("SELECT ticket_board.router_scope_refusal('{\"id\": \"PGU-1\"}'::jsonb, 'main');")
    check("provider account is held" in refused
          and rig.sql("SELECT ticket_board.router_scope_refusal('{\"id\": \"PGU-1\"}'::jsonb, 'app') IS NULL;") == "t",
          f"Router may not give main work, and may still give app: {refused}")
    check("provider account is held" in rig.sql("SELECT ticket_board.cohort_claim_refusal('main');")
          and rig.sql("SELECT ticket_board.cohort_claim_refusal('app') IS NULL;") == "t",
          "main claims nothing, app may")
    status = rig.status()
    held = (status.get("accounts") or {}).get("held") or []
    check(len(held) == 1 and held[0]["runtime"] == "codex" and held[0]["roles"] == ["main"] and SECRET not in json.dumps(status),
          f"/api/provider-holds names the held account and the roles it holds: {status}")

    # Moving the work to another provider: app, on Claude. Its board state goes with it; main hears nothing.
    mark = time.monotonic()
    before = rig.json("SELECT json_build_object('state', state, 'comments', (SELECT count(*) FROM ticket_board.ticket_comments "
                      "WHERE ticket_id = 'PGU-1'))::text FROM ticket_board.tickets WHERE id = 'PGU-1';")
    rig.act("PGU-1", "reassign", assignee="app", reason="main's codex account is held; app is on another account")
    rig.passes(1.5)
    after = rig.json("SELECT json_build_object('state', state, 'assignee', assignee) ::text FROM ticket_board.tickets "
                     "WHERE id = 'PGU-1';")
    check(after == {"state": before["state"], "assignee": "app"} and any("PGU-1" in m for m in rig.to("app", mark))
          and rig.to("main", mark) == [],
          f"PGU-1 is app's, in the same stage, and app is told; main is not: {after} {rig.to('app', mark)} {rig.to('main', mark)}")

    # Clearing: only the Director or the User, with a reason, and recorded before anything resumes.
    rig.op("clear_provider_account_hold", caller="main", expect=403, runtime="codex", reason="I am fine")
    rig.op("clear_provider_account_hold", caller="director", expect=400, runtime="codex", reason=" ")
    check(rig.holds()[0]["cleared_at"] is None and rig.to("main", mark) == [],
          "an implementer cannot clear it, and nobody can without a reason")
    cleared = rig.op("clear_provider_account_hold", caller="director", runtime="codex",
                     reason="operator confirmed the codex account is in good standing")["hold"]
    later = rig.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE target_role = 'main' "
                    "AND last_error LIKE 'provider_account_hold%' AND next_attempt_at > clock_timestamp();")
    check(later == "0", f"what waited on the hold is due at once, not at its next recheck ({later} still later)")
    check(cleared["cleared_by"] == "director" and cleared["clear_reason"].startswith("operator confirmed")
          and rig.holds()[0]["cleared_at"] is not None,
          f"the clear is recorded with its actor and reason: {cleared}")
    events = rig.json("SELECT (ticket_board.user_events_after(0, 50)->'events')::text;")
    check([e["standing"] for e in events if e["kind"] == "provider_safety_hold"] == [False],
          f"the operator's event no longer stands: {events}")

    # The refusal still on main's screen, after the clear: never holds the account again.
    rig.passes(1.0)
    check(len(rig.holds()) == 1, f"the same refusal seen again after the clear holds nothing: {rig.holds()}")
    # Audit regression 1: nor under a new, harmless turn -- first with the old line still drawn exactly as it was (the
    # turn's hook ran before Codex redrew), then under the new turn's own output.
    rig.turn("main")
    rig.passes(1.0)
    rig.panes["main"].screen = codex_screen_of([("say hi", "refused"), ("carry on", "working")])
    rig.passes(1.0)
    check(len(rig.holds()) == 1, f"an old refusal line under a new harmless turn, after the clear, holds nothing: {rig.holds()}")
    rig.panes["main"].screen = codex_screen_of([("say hi", "refused"), ("carry on", "ok")])
    rig.store.write(TARGETS["main"], "idle", source="codex.Stop")
    # Audit regression 2: a refusal drawn afresh in a new turn holds again -- told while the Director is mid-turn.
    rig.store.write(TARGETS["director"], "busy", source="claude.UserPromptSubmit")
    rig.turn("main")
    rig.panes["main"].screen = codex_screen_of([("say hi", "refused"), ("carry on", "ok"), ("check again", "refused")])
    rig.passes(1.5)
    check(len(rig.holds()) == 2 and rig.holds()[1]["cleared_at"] is None and rig.holds()[1]["notified_director"] is True
          and rig.holds()[1]["evidence"] != rig.holds()[0]["evidence"],
          f"a new turn's refusal holds the account again, with its own identity and notice: {rig.holds()}")
    # Audit regression 3: another refusal while that hold stands is taken by it, and after the clear holds nothing.
    rig.turn("main")
    rig.panes["main"].screen = codex_screen_of([("say hi", "refused"), ("carry on", "ok"), ("check again", "refused"),
                                                ("once more", "refused")])
    rig.passes(1.0)
    joined = next(h for h in rig.status()["accounts"]["held"] if h["runtime"] == "codex")
    check(len(rig.holds()) == 2 and joined["refusals"] == 2
          and rig.sql("SELECT count(*) FROM ticket_board.provider_refusal_evidence;") == "3",
          f"a refusal seen during a hold joins it, and is recorded as taken: {joined}")
    # Cleared before its notice could go: the notice is about a hold that no longer stands, so it is dropped.
    rig.op("clear_provider_account_hold", caller="director", runtime="codex", reason="second test clear")
    rig.store.write(TARGETS["director"], "idle", source="claude.Stop")
    rig.sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() WHERE target_role = 'director';")
    rig.passes(1.5)
    dropped = rig.sql("SELECT count(*) FROM ticket_board.notification_trace WHERE event = 'drop' "
                      "AND busy_reason = 'stale_notification' AND detail->'payload'->>'hold' = '2';")
    check(len(rig.held_notices()) == 1 and dropped == "1",
          f"the Director is never told of a hold already cleared ({dropped} dropped): {rig.held_notices()}")
    check(len(rig.holds()) == 2, f"the refusal taken during the hold, still on screen after its clear, holds nothing: {rig.holds()}")
    rig.panes["main"].screen = c594.screens()["recovered"]
    rig.store.write(TARGETS["main"], "idle", source="codex.Stop")
    mark = time.monotonic()
    rig.passes(2.0)
    check(any("PGU-2" in m for m in rig.to("main", mark)), f"cleared: main's waiting work goes: {rig.to('main', mark)}")

    # Audit round 2: a refusal, a clear, then the pane respawned -- a new process, its history gone -- and Codex
    # refuses the same prompt in the same words, drawing exactly what it drew before. A new refusal: a new hold.
    drawing = codex_refusal_screen()
    before = len(rig.holds())
    rig.turn("main")
    rig.panes["main"].screen = drawing
    rig.passes(1.5)
    check(len(rig.holds()) == before + 1, f"the pane's refusal holds the account: {rig.holds()[-1:]}")
    rig.op("clear_provider_account_hold", caller="director", runtime="codex", reason="before the respawn")
    rig.register("main", s644.Provider(rig.root))
    rig.turn("main")
    rig.panes["main"].screen = json.loads(json.dumps(drawing))
    rig.passes(1.5)
    check(len(rig.holds()) == before + 2 and rig.holds()[-1]["cleared_at"] is None
          and rig.holds()[-1]["evidence"].startswith(f"codex:{rig.providers['main'].pid}:"),
          f"after a respawn, the same prompt refused in the same words holds the account again: {rig.holds()[-2:]}")
    rig.op("clear_provider_account_hold", caller="director", runtime="codex", reason="respawn test clear")
    rig.panes["main"].screen = c594.screens()["recovered"]
    rig.store.write(TARGETS["main"], "idle", source="codex.Stop")


def scenario_claude_refusal_holds_the_directors_own_account(rig: Rig) -> None:
    rig.start("PGU-3", "ops")
    rig.passes(1.5)
    notices, events, holds = len(rig.held_notices()), len(rig.events()), len(rig.holds()) + 1
    mark = time.monotonic()
    rig.claude_refusal("app", uuid="refusal-app-1")
    rig.passes(1.5)
    hold = rig.holds()[-1]
    check(hold["runtime"] == "claude" and hold["kind"] == "refusal" and hold["role"] == "app" and hold["category"] == "cyber"
          and hold["evidence"] == "claude:refusal-app-1" and hold["source"] == "claude.Stop" and hold["cleared_at"] is None,
          f"the refused turn's transcript entry, recorded by app's own process, holds the claude account: {hold}")
    roles = next(h for h in rig.status()["accounts"]["held"] if h["runtime"] == "claude")["roles"]
    check(roles == ["app", "audit", "designer", "director", "inspector", "ops"],
          f"every role on it, running or only declared, the Director among them: {roles}")
    check(len(rig.held_notices()) == notices and hold["notified_director"] is False and rig.to("director", mark) == []
          and len(rig.events()) == events + 1,
          f"the Director's own account is held: it is told nothing, and the operator's event is the notice: {rig.events()}")
    record = (rig.state_dir / (TARGETS["app"].replace(":", "_") + ".json")).read_text()
    check(SECRET not in record and SECRET not in json.dumps(rig.holds()) and '"last_refusal"' in record,
          "the pane's record and the hold keep the refusal's identity, never the refused prompt or response")
    rig.act("PGU-3", "add_comment", text="Director: ops, any news?")
    rig.sql("SELECT ticket_board.enqueue_notification('PGU-3', 'escalation', 'director', 'ops may be stuck on PGU-3.', "
            "jsonb_build_object('kind', 'escalation', 'id', 'PGU-3', 'assignee', 'ops', 'target_role', 'director'), 'syrd672-esc');")
    rig.passes(2.0)
    check(rig.to("ops", mark) == [] and rig.to("app", mark) == [] and rig.to("director", mark) == [],
          f"nothing reaches any role on the account, the Director included: {[(r, m[:60]) for _t, r, m in rig.sends if _t >= mark]}")
    # The Director cannot be asked; the operator clears it.
    cleared = rig.may("clear_provider_account_hold", "user", runtime="claude",
                      reason="operator reviewed the refused request and the account")
    check(cleared.get("cleared_by") == "user", f"the User's clear is recorded as the User's: {cleared}")
    rig.store.write(TARGETS["ops"], "idle", source="claude.Stop")
    rig.passes(2.0)
    check(any("PGU-3" in m for m in rig.to("ops", mark)), f"cleared: ops hears about its comment: {rig.to('ops', mark)}")
    # The same transcript's refusal, seen again by a later turn end (a Stop that carries it forward): held nothing more.
    rig.store.write(TARGETS["app"], "idle", source="claude.Stop")
    rig.passes(1.0)
    check(len(rig.holds()) == holds and all(h["cleared_at"] for h in rig.holds()),
          f"a refusal already taken is stale: {rig.holds()}")


def scenario_other_failures_hold_no_account(rig: Rig) -> None:
    before = len(rig.holds())
    rig.claude_failure("ops", "rate_limit", reset_in=60.0)          # a quota, with its own hold
    rig.claude_failure("app", "overloaded", quota=False)
    rig.claude_failure("director", "authentication_failed", quota=False)
    rig.turn("main")
    rig.panes["main"].screen = c594.screens()["capacity"]          # Codex's capacity error
    rig.passes(2.0)
    check(len(rig.holds()) == before, f"quota, overload, authentication and capacity are not safety refusals: {rig.holds()}")
    check(rig.hold("ops").get("state") == "held", f"the quota keeps its own policy: {rig.hold('ops')}")


def scenario_held_by_hand(rig: Rig) -> None:
    rig.op("hold_provider_account", caller="main", expect=403, runtime="codex", reason="looks odd")
    held = rig.may("hold_provider_account", "user", runtime="codex", reason="the reply looked like a refusal")
    again = rig.op("hold_provider_account", caller="director", runtime="codex", reason="same")["hold"]
    current = rig.holds()[-1]
    check(held.get("began") is True and again["began"] is False and again["hold"] == held["hold"]
          and current["kind"] == "suspected" and current["began_by"] == "user" and current["reason"].startswith("the reply"),
          f"a person holds an account on a suspicion, and once: {held} {again} {current}")
    rig.op("clear_provider_account_hold", caller="director", runtime="codex", reason="checked by hand")


def test_on_a_production_board() -> None:
    with temporary_cluster(prefix="s672", shutdown="immediate") as cluster, tempfile.TemporaryDirectory(prefix="syrd672.") as tmp:
        state_dir, session_dir = Path(tmp) / "state", Path(tmp) / "sessions"
        state_dir.mkdir(), session_dir.mkdir()
        rig = Rig(cluster, state_dir, session_dir)
        try:
            scenario_codex_refusal_holds_its_account(rig)
            scenario_claude_refusal_holds_the_directors_own_account(rig)
            scenario_other_failures_hold_no_account(rig)
            scenario_held_by_hand(rig)
        finally:
            rig.close()


# -- the parts, alone ---------------------------------------------------------------------------------------------------
class Conn:
    """A board connection's stand-in: whether the account is held, or which question fails."""

    def __init__(self, *, held: bool = False, fail: bool = False, fail_held: bool = False, rows: dict | None = None) -> None:
        self.held, self.fail, self.fail_held, self.rows, self.statements = held, fail, fail_held, rows or {}, []

    def execute(self, statement: str, params: tuple = ()):
        self.statements.append(statement)
        if self.fail or (self.fail_held and "role_account_hold" in statement):
            raise RuntimeError("the board is unreachable")
        if "to_regprocedure" in statement:
            return SimpleNamespace(fetchone=lambda: (True,))
        if "role_account_hold" in statement:
            return SimpleNamespace(fetchone=lambda: (7 if self.held else None,))
        found = next((rows for key, rows in self.rows.items() if key in statement), [(None,)])
        return SimpleNamespace(fetchone=lambda: found[0], fetchall=lambda: list(found))


def listener() -> SimpleNamespace:
    requeued: list = []
    return SimpleNamespace(logger=SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
                           ledger=SimpleNamespace(requeue=lambda conn, nid, attempts, why, delay_seconds: requeued.append(
                               (nid, why, delay_seconds))), requeued=requeued)


def test_delivery_and_typing_fail_closed() -> None:
    for conn, want in ((Conn(), False), (Conn(held=True), True), (Conn(fail=True), True), (Conn(fail_held=True), True)):
        seen = listener()
        held = provider_refusal.hold_delivery(seen, conn, 5, 1, "assignment", "main", "{}", "claim")
        check(held is want and (seen.requeued[0][0] == 5 and seen.requeued[0][1].startswith("provider_account_hold")
                                if want else seen.requeued == []),
              f"a held account, or a check that cannot be made, holds the notice -- requeued, not dropped: {seen.requeued}")
        check(provider_refusal.typing_held(listener(), conn, "main") is want, "and nothing is typed into the pane either")


def test_only_the_registered_process_is_evidence() -> None:
    registration = {"runtime": "claude", "generation": 3, "pid": 4242, "start_time": 222}
    refusal = {"evidence": "r-9", "category": "cyber", "source": "claude.Stop", "pane_pid": 4242, "pane_start_time": 222}
    state = SimpleNamespace(last_refusal=refusal, state="idle", source="claude.Stop", turn_started_at=None)
    own = provider_refusal.evidence_for(None, "app", TARGETS["app"], state, registration)
    other = provider_refusal.evidence_for(None, "app", TARGETS["app"], state, dict(registration, start_time=111))
    check(own == {"runtime": "claude", "pid": 4242, "start_time": 222, "category": "cyber", "source": "claude.Stop",
                  "evidence": "claude:r-9"} and other is None,
          f"the refusal the registered process recorded is evidence; a reused pid's is not: {own} {other}")
    def gate_on(screen: dict) -> SimpleNamespace:
        return SimpleNamespace(
            cursor_position_runner=lambda *_a, **_k: SimpleNamespace(stdout=f"2 {screen['cursor'][1]} 45"),
            capture_pane_runner=lambda *_a, **_k: SimpleNamespace(stdout="\n".join(screen["rows"])))

    gate = gate_on(codex_refusal_screen())
    codex = dict(registration, runtime="codex")
    turn = {"pane_pid": 4242, "pane_start_time": 222}
    unended = SimpleNamespace(last_refusal=None, state="busy", source="codex.UserPromptSubmit", turn_started_at=1.5,
                              turn_process=turn)
    ended = SimpleNamespace(**{**vars(unended), "state": "idle", "source": "codex.Stop"})
    foreign = SimpleNamespace(**{**vars(unended), "turn_process": {"pane_pid": 4242, "pane_start_time": 111}})
    first_seen = {"at": 2.0}
    sighted = lambda signature: first_seen["at"] if signature else None  # noqa: E731 -- when the board first saw it
    taken = provider_refusal.evidence_for(gate, "main", TARGETS["main"], unended, codex, sighted)
    check(taken and taken["evidence"] == "codex:4242:222:1.5" and taken["category"] == "cyber"
          and provider_refusal.evidence_for(gate, "main", TARGETS["main"], ended, codex, sighted) is None
          and provider_refusal.evidence_for(gate, "main", TARGETS["main"], foreign, codex, sighted) is None
          and provider_refusal.evidence_for(gate, "main", TARGETS["main"], unended, codex) is None,
          f"Codex's line counts after a turn its registered process took and has not ended, first seen in that turn; "
          f"not after a normal end, nor another process's turn, nor with nothing to say when it was first seen: {taken}")
    later = SimpleNamespace(**{**vars(unended), "turn_started_at": 200.0})
    check(provider_refusal.evidence_for(gate, "main", TARGETS["main"], later, codex, sighted) is None,
          "a refusal already showing when a new turn began is an older turn's: it is not this turn's evidence (Audit)")
    first_seen["at"] = 201.0
    check(provider_refusal.evidence_for(gate, "main", TARGETS["main"], later, codex, sighted)["evidence"]
          == "codex:4242:222:200.0", "the same drawing first seen after the new turn began is that turn's refusal")
    plain = gate_on(c594.screens()["capacity"])
    check(provider_refusal.evidence_for(plain, "main", TARGETS["main"], unended, codex, sighted) is None,
          "a capacity error is not a refusal")


def test_only_codexs_own_refusal_ending_the_turn_counts() -> None:
    def seen(screen: dict, history: list[str] | None = None):
        found = provider_refusal.codex_refusal(screen["rows"], screen["cursor"][1],
                                               screen["rows"] if history is None else history)
        return found and found["signature"]

    phrase = provider_refusal.CODEX_REFUSAL_LINES[0]
    one = seen(codex_screen_of([("say hi", "refused")]))
    check(one, f"Codex's own event line, just above the prompt, is a refusal: {one}")
    check(seen(codex_screen_of([("say hi", "refused"), ("carry on", "working")])) is None
          and seen(codex_screen_of([("say hi", "refused"), ("carry on", "ok")])) is None,
          "a refusal with a later turn's prompt echo or reply below it is not the live turn's")
    tight = codex_screen_of([("say hi", "refused")])
    tight["rows"][8] = "› carry on"  # a new turn's echo drawn straight under the old refusal, nothing between
    check(seen(tight) is None, "a later prompt echo ends the live turn's block: the refusal above it is an older turn's")
    quoted_prompt = codex_screen_of([(f"what does '{phrase}' mean?", "ok")])
    quoted_reply = codex_screen_of([("say hi", "ok")])
    quoted_reply["rows"][7] = f"• Codex prints: {phrase}"
    check(seen(quoted_prompt) is None and seen(quoted_reply) is None,
          "the words quoted in a prompt or a reply are not Codex's own event")
    unreadable = codex_screen_of([("say hi", "refused")])
    unreadable["cursor"] = [0, 3]
    check(provider_refusal.codex_refusal(unreadable["rows"], 3, unreadable["rows"]) is None
          and provider_refusal.codex_refusal(quoted_reply["rows"], 41, quoted_reply["rows"]) == {"signature": None},
          "a screen with no prompt at the cursor says nothing; one that reads plainly says: no refusal")
    again = seen(codex_screen_of([("say hi", "refused"), ("say hi", "refused")]))
    check(again and again != one, "the same prompt refused again, beneath the first, is drawn differently")
    check(seen(codex_screen_of([("say hi", "refused")]), history=[]) == seen(codex_screen_of([("say hi", "refused")]), []),
          "and the same drawing is the same signature: freshness is when it was first seen, not what it looks like")


def test_claude_never_re_sends_a_refusal_itself() -> None:
    from scripts import team_launcher as tl

    def env(cli: str) -> dict:
        with tempfile.TemporaryDirectory(prefix="syrd672-cmd.") as raw:
            path = Path(raw) / "project.json"
            path.write_text(json.dumps({"project": "pgu", "board_url": "http://127.0.0.1:1/", "roles": [
                {"role": "app", "slot": 0, "workdir": raw, "cli": [cli]}]}), encoding="utf-8")
            command = tl.cli_command_for_role(tl.load_project_config("pgu", path).roles[0], session_dir=Path(raw) / "s")
        program = next(i for i, word in enumerate(command) if Path(word).name == cli)
        return dict(word.split("=", 1) for word in command[:program] if "=" in word and not word.startswith("-"))

    from scripts import role_command
    check(env("claude").get(role_command.CLAUDE_REFUSAL_FALLBACK_ENV) == "1"
          and role_command.CLAUDE_REFUSAL_FALLBACK_ENV not in env("codex"),
          "a Claude pane starts with Claude Code's refusal fallback off, so a refused request is never sent to another "
          "model on the same account; other CLIs are untouched")


def test_the_hook_keeps_a_refusals_identity_and_never_its_words() -> None:
    import subprocess
    with tempfile.TemporaryDirectory(prefix="syrd672-hook.") as raw:
        root = Path(raw)
        transcript = root / "t.jsonl"

        def turn_end(source: str, records: list[dict]) -> dict:
            transcript.write_text("".join(json.dumps(r) + "\n" for r in records))
            payload = {"session_id": "s-1", "transcript_path": str(transcript), "last_assistant_message": SECRET,
                       **({"error": "invalid_request"} if source.endswith("Failure") else {})}
            subprocess.run([sys.executable, str(HOOK), "idle" if source == "claude.Stop" else "busy", "--source", source,
                            "--target", "pgu-app:0.0", "--state-dir", str(root / "state"), "--session-dir", str(root / "s")],
                           input=json.dumps(payload), text=True, check=True, capture_output=True,
                           env={"PATH": os.environ["PATH"], "HOME": raw})
            return json.loads((root / "state" / "pgu-app_0.0.json").read_text())

        asked = {"type": "user", "uuid": "u-1", "message": {"role": "user", "content": SECRET}}
        refused = {"type": "assistant", "uuid": "r-7", "timestamp": "2026-11-07T12:00:00Z",
                   "message": {"stop_reason": "refusal", "stop_details": {"category": "cyber"}, "content": [{"text": SECRET}]}}
        answered = {"type": "assistant", "uuid": "a-8", "message": {"stop_reason": "end_turn", "content": [{"text": "ok"}]}}
        for source in ("claude.Stop", "claude.StopFailure"):
            (root / "state").mkdir(exist_ok=True)
            for old in (root / "state").iterdir():
                old.unlink()
            record = turn_end(source, [asked, refused])
            check(record.get("last_refusal", {}).get("evidence") == "r-7" and record["last_refusal"]["category"] == "cyber"
                  and record["last_refusal"]["source"] == source and SECRET not in json.dumps(record),
                  f"{source}: the refusal's identity and category are recorded, and no word of the exchange: {record}")
        later = turn_end("claude.Stop", [asked, refused, {**asked, "uuid": "u-2"}, answered])
        check((later.get("last_refusal") or {}).get("evidence") == "r-7", "a later normal turn neither forgets the refusal nor makes it new")
        fresh = root / "fresh"
        transcript.write_text(json.dumps(asked) + "\n" + json.dumps(answered) + "\n")
        subprocess.run([sys.executable, str(HOOK), "idle", "--source", "claude.Stop", "--target", "pgu-app:0.0",
                        "--state-dir", str(fresh), "--session-dir", str(root / "s")],
                       input=json.dumps({"session_id": "s-2", "transcript_path": str(transcript)}), text=True, check=True,
                       capture_output=True, env={"PATH": os.environ["PATH"], "HOME": raw})
        check("last_refusal" not in json.loads((fresh / "pgu-app_0.0.json").read_text()),
              "a turn that was not refused records none")


def test_pickup_and_admission_leave_out_every_held_role() -> None:
    from pull_scheduling_policy_test import pull_document
    from scripts import worker_admission
    from scripts.ticket_board import pull_pickup
    from scripts.ticket_board.workflow_config import pull_claimant_pool

    class Rows:
        def execute(self, statement: str, params: tuple = ()):
            rows = [("ops",)] if "provider_quota_holds" in statement else [("main",), ("app",)]
            return SimpleNamespace(fetchall=lambda: list(rows))

    seen = SimpleNamespace(_provider_quota_holds_present=True, _provider_account_holds_present=True)
    reasons = pull_pickup._held(seen, Rows())
    check(set(reasons) == {"ops", "main", "app"} and reasons["main"].startswith("its provider account is held")
          and reasons["ops"] == "its provider is held for quota",
          f"pull pickup claims for no role on a held account, beside the quota-held, and says which: {reasons}")
    document = pull_document()
    pool = sorted(pull_claimant_pool(document))
    answers = {"/api/workflow": {"revision": 1, "document": document}, "/api/reservations": {"pull_queue": {}},
               "/api/provider-holds": {"holds": [], "accounts": {"held": [{"runtime": "claude", "roles": pool[:1]}]}}}
    getter = lambda url: answers.get("/" + url.split("/", 3)[3], {})  # noqa: E731
    roles = {name: SimpleNamespace(model=None, effort=None) for name in pool}
    _claimants, _ready, held, _holders, _cohorts = worker_admission._board_facts(
        SimpleNamespace(board_url="http://127.0.0.1:1/"), roles, getter, ask_holders=False)
    check(pool and held == set(pool[:1]), f"no worker on a held account is started: {held} of {pool}")


def test_no_context_restore_or_retry_is_typed_while_held() -> None:
    from unittest.mock import patch

    from scripts.ticket_board import director_capacity, session_context

    def board(held: bool) -> Conn:
        context = {"role": "app", "ticket_id": "PGU-1", "session_id": "old", "recorded_at": 1000.0, "state": "bound",
                   "runtime": "claude"}
        return Conn(held=held, rows={"held_ticket_contexts": [{"c": json.dumps(context)}],
                                     "open_ticket_context_restore": [({"attempt": {"id": 1}, "opened": True},)],
                                     "ticket_context_status": [({},)]})

    def parts():
        sent: list = []
        seen = SimpleNamespace(role_runtimes={"app": "claude"}, sender=lambda target, text: sent.append((target, text)),
                               logger=SimpleNamespace(info=lambda *a: None, warning=lambda *a: None))
        gate = SimpleNamespace(role_targets={"app": "pgu-app:0.0", "director": "pgu-director:0.0"},
                               state_store=SimpleNamespace(read=lambda target: SimpleNamespace(
                                   last_session_start={"session_id": "new", "at": time.time()})),
                               eligibility_busy=lambda target: False, background_work_roles=lambda roles: [])
        return seen, gate, sent

    for held in (False, True):
        seen, gate, sent = parts()
        session_context.repair_pass(seen, board(held), gate, time.time())
        session_context._tell(seen, board(held), gate, "app", "a context note")
        check((sent == []) if held else (sent == [("pgu-app:0.0", "/resume old"), ("pgu-app:0.0", "a context note")]),
              f"{'held: no resume and no note is typed' if held else 'unheld, the same pass resumes and notes'}: {sent}")
    seen, gate, sent = parts()
    conn = board(True)
    answer = session_context.before_delivery(seen, conn, gate, ticket_id="PGU-1", role="app", now=time.time(), handoff=True)
    check(answer == "wait" and sent == [] and not any("ticket_context_status" in s for s in conn.statements),
          f"a notice's restore waits, typing nothing and parking nothing: {answer} {sent}")
    for held in (False, True):
        seen, gate, sent = parts()
        seen._director_capacity_present = True
        conn = board(held)
        with patch.object(director_capacity, "reading", lambda _gate: ("pgu-director:0.0", SimpleNamespace(), {})):
            director_capacity.run(seen, conn, gate)
        claimed = any("note_director_capacity" in s for s in conn.statements)
        check(claimed is not held and sent == [],
              f"{'held: no capacity retry is claimed' if held else 'unheld, the retry is asked for'}: {conn.statements}")


def test_schema_sql_ends_with_the_migrations_copies() -> None:
    """A fresh board (schema.sql) and an upgraded one (pgu1030 last) run the same functions."""
    import re
    migration = (ROOT / "scripts/ticket_board/migrations/pgu1030_syrd672_provider_account_holds.sql").read_text()
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()

    def last(text: str, name: str) -> str:
        start = text.rindex(f"CREATE OR REPLACE FUNCTION ticket_board.{name}(")
        return text[start:text.index("$$;", start) + 3]

    names = re.findall(r"CREATE OR REPLACE FUNCTION ticket_board\.(\w+)\(", migration)
    drifted = [name for name in names if last(schema, name) != last(migration, name)]
    check(len(names) >= 12 and drifted == [], f"schema.sql's last copy of each of pgu1030's {len(names)} functions is its: {drifted}")


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"provider_account_hold_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
