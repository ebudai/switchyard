#!/usr/bin/env python3
"""SYRD-738 on a production-built board: the Director is refused first, then replaced on another runtime.

SYRD-672's rig: the real listener, gate and pane hook, stand-in provider processes registered as the roles, and the
board built from this tree's migrations. The rebind runs through `director_rebind` itself -- plan, then apply --
reading the board over its own HTTP routes (the recovery packet, the workflow, the runtime assignments) and calling
the board's root-only functions as the database owner, as `pkexec switchyard rebind-director` does through psql.
tmux is a stand-in whose kill-session ends the old Director's provider process for real; the start registers a fresh
codex provider as the Director, as the replacement pane's registrar would.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import provider_account_hold_test as s672  # noqa: E402
import provider_quota_hold_test as s644  # noqa: E402
from role_runtime_test import RuntimeRunner, _write_config  # noqa: E402
from scripts import director_rebind as dr, runtime_handover, team_launcher  # noqa: E402
from scripts.ticket_board import recovery_packet as rp  # noqa: E402
from scripts.ticket_board.peer_identity import read_process  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
REASON = "the Director's claude account refused on safety grounds; moving coordination to codex"
ELIGIBLE = {"codex": [], "agy": ["agy is not installed"], "hermes": ["hermes is not installed"],
            "copilot": ["copilot is not signed in"]}


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


class Rig(s672.Rig):
    def get(self, path: str) -> Any:
        try:
            with urllib.request.urlopen(self.base + path, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ValueError(f"board answered {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from None

    def sender(self, target: str, message: str) -> dict:
        """A Director on codex takes a turn as main does: the real hook stamps it, and Codex's own Stop ends it."""
        if target == s644.TARGETS["director"] and s644.RUNTIMES["director"] == "codex":
            self.sends.append((time.monotonic(), "director", message))
            self.turn("director")
            self.panes["director"].screen = s644.c594.screens()["recovered"]
            self.store.write(target, "idle", source="codex.Stop")
            return {}
        return super().sender(target, message)

    def rebinds(self) -> list[dict]:
        return self.json("SELECT coalesce(json_agg(r ORDER BY r.id), '[]')::text FROM ticket_board.director_rebinds r;")

    def rebind_events(self) -> list[dict]:
        events = self.json("SELECT ticket_board.user_events_after(0, 50)::text;")["events"]
        return [e for e in events if e["kind"] == "director_rebind"]

    def pause(self) -> dict | None:
        return self.json("SELECT row_to_json(p)::text FROM ticket_board.work_pauses p WHERE ended_at IS NULL;")

    def account(self, role: str) -> str:
        return self.sql(f"SELECT coalesce(ticket_board.role_account_runtime('{role}'), '');")

    def declared(self, role: str) -> str:
        return self.sql("SELECT r->>'runtime' FROM ticket_board.workflow_configuration c, "
                        f"jsonb_array_elements(c.document->'roles') r WHERE c.singleton AND r->>'name' = '{role}';")

    def registered(self, role: str) -> dict:
        return self.json(f"SELECT row_to_json(a)::text FROM ticket_board.role_runtime_assignments a WHERE role = '{role}';") or {}


class RigAdmin:
    """The board's root-only functions, called as the database owner: what psql does for the command as root."""

    def __init__(self, rig: Rig) -> None:
        self.rig, self.calls = rig, []

    def call(self, function: str, *args: tuple[Any, str]) -> Any:
        literals = []
        for value, sql_type in args:
            if value is None:
                literals.append(f"NULL::{sql_type}")
                continue
            text = value if isinstance(value, str) else json.dumps(value, sort_keys=True)
            assert "$v$" not in text
            literals.append(f"$v${text}$v$::{sql_type}")
        self.calls.append(function)
        try:
            out = self.rig.sql(f"SELECT {function}({', '.join(literals)})::text;")
        except AssertionError as exc:  # the fixture's psql raises the database's refusal
            raise RuntimeError(str(exc).strip().splitlines()[0]) from None
        return json.loads(out) if out else None


class Tmux(RuntimeRunner):
    """tmux for the Director's session: its kill ends the registered provider process, as hanging the pane up does."""

    def __init__(self, rig: Rig) -> None:
        super().__init__(live={"pgu-director"})
        self.rig = rig
        self.current["pgu-director"] = "claude"

    def __call__(self, args: list[str], **kwargs: Any):
        command = [str(part) for part in args]
        if command[:2] == ["tmux", "kill-session"] and "pgu-director" in self.live:
            self.rig.providers["director"].stop()
        return super().__call__(args, **kwargs)


def starter(rig: Rig, *, fail: bool = False):
    def start(role: Any, *, config: Any, pane_state_dir: Path, runner: Any) -> int:
        runtime = team_launcher._role_cli_name(role)
        runner.starts.append((role.tmux_session, runtime))
        if fail:
            return 3
        s644.RUNTIMES["director"] = runtime  # the replacement pane registers what its configuration names
        rig.register("director", s644.Provider(rig.root))
        # Codex's idle composer, and the app-server daemon it starts (the rig's measured process table) already
        # running before the pane's first idle hook, as it is once a real Codex session has settled.
        rig.panes["director"].screen = s644.c594.screens()["recovered"]
        rig.panes["director"].started = time.time() - 600
        rig.store.write(s644.TARGETS["director"], "idle", source="codex.Stop")
        runner.live.add(role.tmux_session)
        runner.current[role.tmux_session] = runtime
        return 0
    return start


def rebind(rig: Rig, tmp: Path, hold: int, *, fail_start: bool = False) -> tuple[int, dr.Rebind, Tmux, list[str]]:
    config_path = _write_config(tmp, project="pgu")
    config = team_launcher.load_project_config("pgu", config_path)
    tmux = Tmux(rig)
    plan = dr.plan_rebind(config, config_path=config_path, get=rig.get, hold=hold, runtime="codex", model="",
                          runner=tmux, eligible_for=lambda *a, **k: dict(ELIGIBLE))
    printed: list[str] = []
    dr.describe(plan, printed.append)
    check(not plan.blockers and plan.registration.get("runtime") == "claude"
          and plan.registration.get("process_pid") == rig.providers["director"].pid,
          f"the plan reads the held Director's registration from the board: {plan.registration} {plan.blockers}")
    state = tmp / "pane-state"
    state.mkdir(exist_ok=True)
    handover = runtime_handover.RuntimeHandover(
        read_assignments=lambda _c: ((rig.get("/api/runtime-assignments") or {}).get("assignments") or {}, ""),
        read_process=read_process, settle_seconds=0.2, exit_timeout_seconds=10.0)
    work = dr.Rebind(plan=plan, config=config, config_path=config_path, admin=RigAdmin(rig), get=rig.get,
                     runner=tmux, tmux_runner=tmux, start=starter(rig, fail=fail_start), handover=handover,
                     pane_state_dir=state, print_func=printed.append)
    return dr.apply(work, operator="eric", reason=REASON), work, tmux, printed


def refuse(rig: Rig, statement: str) -> str:
    try:
        rig.sql(statement)
    except AssertionError as exc:
        return str(exc)
    return ""


def director_first_refusal(rig: Rig) -> dict:
    rig.start("PGU-1", "main")
    rig.start("PGU-2", "ops")
    rig.passes(1.5)
    rig.claude_refusal("director", uuid="refusal-director-1")
    rig.passes(1.5)
    hold = rig.holds()[-1]
    check(hold["runtime"] == "claude" and hold["role"] == "director" and hold["cleared_at"] is None
          and rig.account("director") == "claude", f"the Director's own refusal holds the claude account: {hold}")
    return hold


def scenario_the_board_admits_only_root(rig: Rig, hold: int) -> None:
    packet = json.dumps({"hold": hold})
    as_service = refuse(rig, "SET ROLE ticket_board_service;\nSELECT ticket_board.begin_director_rebind("
                             f"{hold}, 'eric', 'x', 'codex', '', '{{}}', '{packet}');")
    as_listener = refuse(rig, "SET ROLE ticket_board_listener;\nSELECT ticket_board.finish_director_rebind(1, 'applied', '{}', '/x');")
    reads = rig.sql("SET ROLE ticket_board_service;\nSELECT count(*) FROM ticket_board.director_rebinds;").splitlines()[-1]
    check("permission denied" in as_service and "permission denied" in as_listener and reads == "0",
          f"no role may begin or finish a rebind; the service may read the record: {as_service[:120]} {as_listener[:120]}")
    begin = ("SELECT ticket_board.begin_director_rebind({hold}, 'eric', 'x', '{to}', '', '{{}}', '{packet}'::jsonb);")
    held = refuse(rig, begin.format(hold=hold, to="claude", packet=packet))
    other = refuse(rig, begin.format(hold=hold, to="codex", packet=json.dumps({"hold": hold + 100})))
    gone = refuse(rig, begin.format(hold=hold + 100, to="codex", packet=json.dumps({"hold": hold + 100})))
    check("the one held" in held and "packet is for hold" in other and "not a current provider account hold" in gone,
          f"the database itself refuses the held target, another hold's packet and a hold that is not current: "
          f"{held[:120]} | {other[:120]} | {gone[:120]}")
    rig.op("hold_provider_account", caller="user", runtime="codex", reason="suspected, by hand")
    codex = rig.holds()[-1]["id"]
    not_director = refuse(rig, begin.format(hold=codex, to="agy", packet=json.dumps({"hold": codex})))
    held_too = refuse(rig, begin.format(hold=hold, to="codex", packet=packet))
    rig.op("clear_provider_account_hold", caller="user", runtime="codex", reason="checked by hand")
    check("which is not the Director's" in not_director and "codex account is held too" in held_too,
          f"a hold on another account, or a target held itself, is refused: {not_director[:140]} | {held_too[:140]}")
    check(rig.rebinds() == [], "nothing refused left a record")


def scenario_no_eligible_alternate(rig: Rig, tmp: Path, hold: int) -> None:
    config_path = _write_config(tmp, project="pgu")
    config = team_launcher.load_project_config("pgu", config_path)
    none = {runtime: ["not installed"] for runtime in ELIGIBLE}
    plan = dr.plan_rebind(config, config_path=config_path, get=rig.get, hold=hold, runtime="codex", model="",
                          runner=Tmux(rig), eligible_for=lambda *a, **k: dict(none))
    sends = len(rig.sends)
    printed: list[str] = []
    first = dr.unavailable(plan, admin=RigAdmin(rig), operator="eric", reason=REASON, print_func=printed.append)
    second = dr.unavailable(plan, admin=RigAdmin(rig), operator="eric", reason=REASON, print_func=printed.append)
    pause, rows, events = rig.pause(), rig.rebinds(), rig.rebind_events()
    check(first == 1 and second == 1 and len(rows) == 1 and rows[0]["state"] == "unavailable"
          and rows[0]["result"]["alternates"] == none and len(events) == 1 and events[0]["detail"] == "unavailable"
          and events[0]["standing"] is True,
          f"no eligible alternate is recorded once, with what each runtime lacks, and the operator told once: "
          f"{rows} {events}")
    check(pause is not None and pause["mode"] == "drain" and pause["began_by"] == "operator:eric"
          and "is told" in printed[0] and "already told" in printed[1] and len(rig.sends) == sends,
          f"work is paused (drain) on the operator's behalf, and nothing is sent: {pause} {printed}")
    rig.op("resume_work", caller="user", reason="the operator is making codex ready")
    check(rig.pause() is None, "the operator ends the pause the usual way")


def scenario_apply(rig: Rig, tmp: Path, hold: int) -> None:
    # Work that waited on the held Director: an escalation about unheld work, requeued while the hold stands.
    rig.sql("SELECT ticket_board.enqueue_notification('PGU-1', 'escalation', 'director', 'main may be stuck on PGU-1.', "
            "jsonb_build_object('kind', 'escalation', 'id', 'PGU-1', 'assignee', 'main', 'target_role', 'director'), "
            "'syrd738-waiting');")
    rig.passes(1.0)
    old = dict(rig.providers["director"].__dict__)
    mark = time.monotonic()
    status, work, tmux, printed = rebind(rig, tmp, hold)
    text = "\n".join(printed)
    check(status == 0, f"the rebind is applied: {text[-800:]}")
    row = rig.rebinds()[-1]
    check(row["state"] == "applied" and row["operator"] == "eric" and row["from_runtime"] == "claude"
          and row["to_runtime"] == "codex" and row["hold_id"] == hold and row["reason"] == REASON
          and row["old_identity"]["registration"]["process_pid"] == old["pid"]
          and row["old_identity"]["declared"]["runtime"] == "claude"
          and [s["step"] for s in row["steps"]] == ["old_stopped", "session_set_aside", "workflow", "projection",
                                                   "packet_written", "started"]
          and row["packet"]["state_digest"] == work.plan.packet["provenance"]["state_digest"]
          and row["result"]["registration"]["runtime"] == "codex",
          f"the board journals the old and new identities, the operator, the reason, the hold, the packet's "
          f"provenance and each change: {json.dumps(row)[:900]}")
    new = rig.registered("director")
    check(rig.declared("director") == "codex" and rig.account("director") == "codex" and new["runtime"] == "codex"
          and new["process_pid"] == rig.providers["director"].pid and new["process_pid"] != old["pid"]
          and read_process(old["pid"]) is None
          and team_launcher._role_cli_name(dr._director(team_launcher.load_project_config("pgu", work.config_path)))
          == "codex",
          f"one authoritative Director: declared, configured and registered codex, a new process; the old one gone: "
          f"{new} {rig.declared('director')}")
    current = rig.holds()[0]
    check(current["id"] == hold and current["cleared_at"] is None
          and rig.sql("SELECT ticket_board.role_account_hold('app') IS NOT NULL;") == "t",
          "the hold stands, and still holds every role left on the claude account")
    check(tmux.starts == [("pgu-director", "codex")] and not any("send-keys" in " ".join(c) for c in tmux.calls),
          f"only codex was started, and nothing was typed into the held pane: {tmux.starts}")
    written = json.loads(Path(row["packet_file"]).read_text())
    check(rp.validate(written) == work.plan.packet and written["packet_digest"] == row["packet"]["packet_digest"],
          "the packet file the new session is given is the validated packet the board recorded")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and not any(f"Director rebind {row['id']}" in m for m in rig.to("director", mark)):
        rig.passes(1.0)
    told = rig.to("director", mark)
    notice = [m for m in told if f"Director rebind {row['id']}" in m]
    check(len(notice) == 1 and row["packet_file"] in notice[0] and f"recovery-packet --check {row['packet_file']}" in notice[0]
          and f"safety hold {hold}" in notice[0] and "send nothing to the claude account" in notice[0]
          and s672.SECRET not in notice[0],
          f"the replacement is told once, in fixed words, where its validated packet is: {told}")
    check(any("main may be stuck on PGU-1" in m for m in told),
          f"what waited on the held Director now reaches its replacement: coordination resumes: {told}")
    check(all(r == "director" or r == "main" for _at, r, _m in rig.sends if _at >= mark)
          and not rig.to("app", mark) and not rig.to("ops", mark),
          f"nothing reaches the held claude account: {[(r, m[:50]) for at, r, m in rig.sends if at >= mark]}")
    check(rig.sql(f"SELECT ticket_board.director_rebind_notice_is_current('{{\"rebind\": {row['id']}}}'::jsonb);") == "t"
          and rig.rebind_events()[-1]["detail"] == "applied" and rig.rebind_events()[-1]["standing"] is False
          and rig.rebind_events()[0]["standing"] is False,
          f"the notice stays current while the Director runs on this rebind; the operator's events stand no longer: "
          f"{rig.rebind_events()}")
    # A notice for a rebind that is not the one the Director runs on is dropped, never delivered as an escalation.
    rig.sql("SELECT ticket_board.enqueue_notification('PGU-1', 'escalation', 'director', 'stale rebind notice', "
            f"jsonb_build_object('kind', 'director_rebind', 'id', 'PGU-1', 'rebind', {row['id'] + 100}, 'hold', {hold}, "
            "'target_role', 'director'), 'syrd738-stale');")
    stale_mark = time.monotonic()
    rig.passes(2.0)
    check(not any("stale rebind notice" in m for m in rig.to("director", stale_mark))
          and rig.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE dedupe_key = 'syrd738-stale' "
                      "AND dead_lettered_at IS NULL AND claimed_at IS NULL AND next_attempt_at <= now();") == "0",
          f"a rebind notice that is not current is not delivered: {rig.to('director', stale_mark)}")
    act = rig.act("PGU-1", "add_comment", caller="director", text="Director (codex): picking up from the packet.")
    check(isinstance(act, dict), "the replacement acts as the Director")
    # Once the Director runs on another account again, this rebind's notice is no longer current.
    rig.sql("UPDATE ticket_board.role_runtime_assignments SET runtime = 'agy' WHERE role = 'director';")
    moved = rig.sql(f"SELECT ticket_board.director_rebind_notice_is_current('{{\"rebind\": {row['id']}}}'::jsonb);")
    rig.sql("UPDATE ticket_board.role_runtime_assignments SET runtime = 'codex' WHERE role = 'director';")
    check(moved == "f", f"a rebind's notice is current only while the Director runs on its runtime: {moved}")


def scenario_a_failed_start_is_undone(rig: Rig, tmp: Path, hold: int) -> None:
    old = rig.providers["director"].pid
    mark = time.monotonic()
    status, work, tmux, printed = rebind(rig, tmp, hold, fail_start=True)
    row = rig.rebinds()[-1]
    check(status == 1 and row["state"] == "rolled_back" and row["result"]["director_started"] is False
          and row["result"]["undone"] == ["new_session", "workflow", "projection"] and row["result"]["problems"] == [],
          f"the board records the rollback and what it undid: {json.dumps(row)[:600]} {printed[-2:]}")
    reg = rig.registered("director")
    check(rig.declared("director") == "claude" and rig.account("director") == "claude"
          and reg.get("runtime") in (None, "claude") and read_process(old) is None
          and tmux.starts == [("pgu-director", "codex")]
          and team_launcher._role_cli_name(dr._director(team_launcher.load_project_config("pgu", work.config_path)))
          == "claude",
          f"one authoritative identity: claude, declared and configured, stopped, never restarted: {reg} {tmux.starts}")
    pause, events = rig.pause(), rig.rebind_events()
    check(pause is not None and pause["mode"] == "drain" and pause["began_by"] == "operator:eric"
          and events[-1]["detail"] == "rolled_back" and events[-1]["standing"] is True,
          f"work stays paused and the operator is told: {pause} {events}")
    rig.passes(1.5)
    check(not rig.to("director", mark) and not rig.to("app", mark) and not rig.to("ops", mark)
          and rig.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                      "WHERE payload->>'kind' = 'director_rebind';") == "0",
          f"no notice is queued for a rebind that did not happen, and nothing reaches the held account: {rig.sends[-3:]}")
    # The database will not call a rebind applied while the Director is not on its runtime, whoever asks.
    packet = json.dumps({"hold": hold})
    rebind_id = rig.sql(f"SELECT ticket_board.begin_director_rebind({hold}, 'eric', 'by hand', 'codex', '', '{{}}', "
                        f"'{packet}'::jsonb);")
    early = refuse(rig, f"SELECT ticket_board.finish_director_rebind({rebind_id}, 'applied', '{{}}', '/x/packet.json');")
    check("the Director is on claude, not codex; the rebind is not applied" in early,
          f"an applied finish needs the Director on the new runtime: {early[:200]}")
    refuse(rig, f"SELECT ticket_board.finish_director_rebind({rebind_id}, 'rolled_back', '{{}}', NULL);")


def test_a_director_rebind_on_a_production_board() -> None:
    saved = dict(s644.RUNTIMES)
    try:
        with temporary_cluster(prefix="s738", shutdown="immediate") as cluster, \
                tempfile.TemporaryDirectory(prefix="syrd738.") as tmp:
            state_dir, session_dir = Path(tmp) / "state", Path(tmp) / "sessions"
            state_dir.mkdir(), session_dir.mkdir()
            rig = Rig(cluster, state_dir, session_dir)
            try:
                hold = director_first_refusal(rig)["id"]
                scenario_the_board_admits_only_root(rig, hold)
                (Path(tmp) / "unavailable").mkdir()
                scenario_no_eligible_alternate(rig, Path(tmp) / "unavailable", hold)
                (Path(tmp) / "apply").mkdir()
                scenario_apply(rig, Path(tmp) / "apply", hold)
            finally:
                rig.close()
        s644.RUNTIMES.clear()
        s644.RUNTIMES.update(saved)
        with temporary_cluster(prefix="s738b", shutdown="immediate") as cluster, \
                tempfile.TemporaryDirectory(prefix="syrd738b.") as tmp:
            state_dir, session_dir = Path(tmp) / "state", Path(tmp) / "sessions"
            state_dir.mkdir(), session_dir.mkdir()
            rig = Rig(cluster, state_dir, session_dir)
            try:
                hold = director_first_refusal(rig)["id"]
                scenario_a_failed_start_is_undone(rig, Path(tmp), hold)
            finally:
                rig.close()
    finally:
        s644.RUNTIMES.clear()
        s644.RUNTIMES.update(saved)


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"director_rebind_live_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
