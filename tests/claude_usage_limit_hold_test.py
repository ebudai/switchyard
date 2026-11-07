#!/usr/bin/env python3
"""SYRD-735: Claude's weekly limit holds the role, whoever shares its account, and nobody types through the menu.

Live (2026-11-05/06): App's Claude account hit its weekly limit and App was held (SYRD-644). The Director reassigned
SYRD-734 to Main, idle and not held. Main's turn met the same limit, and Claude Code 2.1.318 drew its menu ("What do
you want to do? 1. Stop and wait for limit to reset ..."). No hold was recorded for Main. Main's pane read busy, a
priority alert said it was busy with nothing moving, and the board stall alert counted App's ticket as runnable.

Measured offline (tests/fixtures/syrd735_claude_usage_limit.json, kit in tests/fixtures/syrd735_probe):
- **StopFailure does run under the menu.** It runs before Claude has written the turn's API-error record: when the
  hook starts, the transcript holds 468 bytes and no API error, and the record lands about 0.1 s later. Main's hook
  read the transcript at once and found no rejected quota. The listener classes such a failure as a throttle, and the
  hook leaves the pane busy. That is the whole failure, with or without the menu.
- **Esc leaves the menu for the prompt.** It chooses nothing, sends no request and fires no hook.
- **Each session's own transcript names its account** (session-owner, at its start). App, Main and Ops share one.

A production-built board runs here, with the real listener, gate and hook program. SYRD-644's rig supplies the panes:
processes under a process named `tmux: server`, with the hook run by the pane's own provider. App and Ops are Claude
roles, Main is Codex. Claude's writing is played from the measured records, at the measured order and delay: the turn
starts through the hook, StopFailure runs, and the API-error record lands 0.1 s after it starts.

Main runs first, from a git archive. These cases run on both:
- **The live race:** main records no hold, leaves App's pane busy, and delivers a comment into the refusing provider;
  the board reads App's turn unresolved and the stall evaluator counts App's ticket as runnable. Here App is held to
  the record's reset, its pane is idle, the comment waits, its turn reads resolved, and the stall evaluator says why
  the ticket is not runnable.
- **An earlier turn's record in the transcript:** main takes it as this turn's. Here it is never taken: with no new
  record the quota stays unknown (after a bounded wait), and with one it is the new one.
- **One account, two roles:** Ops on a different account is not held. Ops on App's account, whose own turn ended
  after App's failure, is not held either, since its account answered since then. Ops on App's account, last heard
  before App's latest failure, is held with App without being woken, and the hold joins App's notice. The Director's
  pane, also on that account, and Main (Codex) are never held this way.
- **The menu:** for held App, the listener presses Esc once, which dismisses it. For held Ops, whose menu ignores
  Esc, it presses twice and then leaves it to a person. It never answers another question on a held pane, and for
  the Director, not held, it presses nothing.
- **A clear sticks:** the Director clears Ops's shared hold; it is not shared again while App's stands, and Ops's
  hand-over goes.
- **The reset:** at reset plus grace App's hold releases, and the work waiting for it is delivered.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "b2524ef123f1410999711ba5052783beea9b6183"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
FIXTURE = json.loads((ROOT / "tests/fixtures/syrd735_claude_usage_limit.json").read_text(encoding="utf-8"))
ACCOUNT, OTHER = ("00000000-0000-4000-8000-000000000001", "00000000-0000-4000-8000-000000000002"), \
    ("00000000-0000-4000-8000-0000000000aa", "00000000-0000-4000-8000-0000000000bb")
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def transcript(path: Path, session: str, account: tuple[str, str], *, older_reset: int | None = None) -> None:
    """A session transcript as Claude Code 2.1.318 starts one: its head records (session-owner names the account), then
    the user's prompt; with `older_reset`, an earlier turn's API-error record, an hour old."""
    head = [dict(r, sessionId=session, **({"accountUuid": account[0], "organizationUuid": account[1]}
                                           if r.get("type") == "session-owner" else {})) for r in FIXTURE["transcript_head"]]
    lines = [json.dumps(r) for r in head]
    if older_reset is not None:
        old = dict(FIXTURE["api_error_record"], timestamp=iso(time.time() - 3600))
        old["quotaLimits"] = dict(old["quotaLimits"], resetsAt=older_reset)
        lines.append(json.dumps(old))
    lines.append(json.dumps({"type": "user", "sessionId": session, "timestamp": iso(time.time()),
                             "message": {"role": "user", "content": "PRIVATE PROMPT"}}))
    path.write_text("".join(line + "\n" for line in lines))


def late_record(path: Path, resets_at: int, delay: float = 0.1) -> threading.Thread:
    """Claude Code writing the turn's API-error record `delay` after its StopFailure hook starts, as measured."""
    def write() -> None:
        time.sleep(delay)
        record = dict(FIXTURE["api_error_record"], timestamp=iso(time.time()))
        record["quotaLimits"] = dict(record["quotaLimits"], resetsAt=resets_at)
        with path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")
    thread = threading.Thread(target=write, daemon=True)
    thread.start()
    return thread


def hook_cases(root: Path) -> dict:
    """The real hook program alone, on an earlier turn's record: no record of this turn; then one, 0.1 s late."""
    hook = root / "scripts/ticket-board-pane-idle-hook"
    seen = {}
    with tempfile.TemporaryDirectory(prefix="syrd735-hook.") as tmp:
        state, sessions = Path(tmp) / "state", Path(tmp) / "sessions"
        state.mkdir(), sessions.mkdir()
        for case, fresh in (("no_record_yet", None), ("late_record", int(time.time()) + 7200)):
            log = Path(tmp) / f"{case}.jsonl"
            transcript(log, "s-hook", ACCOUNT, older_reset=int(time.time()) - 1800)
            target = f"pgu-{case.replace('_', '-')}:0.0"
            (state / (target.replace(":", "_") + ".json")).write_text(json.dumps(
                {"target": target, "state": "busy", "source": "claude.UserPromptSubmit", "updated_at": time.time(),
                 "turn_started_at": time.time()}))
            payload = dict(FIXTURE["stopfailure_payload"], session_id="s-hook", transcript_path=str(log))
            started = time.monotonic()
            writer = late_record(log, fresh) if fresh else None
            subprocess.run([sys.executable, str(hook), "busy", "--source", "claude.StopFailure", "--target", target,
                            "--state-dir", str(state), "--session-dir", str(sessions)], input=json.dumps(payload),
                           text=True, capture_output=True, check=True, timeout=30,
                           env={"PATH": os.environ["PATH"], "HOME": tmp})
            took = time.monotonic() - started
            if writer:
                writer.join()
            record = json.loads((state / (target.replace(":", "_") + ".json")).read_text())
            seen[case] = {"quota": record["provider_error"]["quota"], "state": record["state"], "took": round(took, 2),
                          "fresh": fresh}
        # The turn-start hook stamping its turn after a fast refusal was already recorded: still this turn's record.
        log, target = Path(tmp) / "late-stamp.jsonl", "pgu-late-stamp:0.0"
        transcript(log, "s-hook", ACCOUNT)
        record = dict(FIXTURE["api_error_record"], timestamp=iso(time.time()))
        record["quotaLimits"] = dict(record["quotaLimits"], resetsAt=int(time.time()) + 3600)
        with log.open("a") as handle:
            handle.write(json.dumps(record) + "\n")
        (state / (target.replace(":", "_") + ".json")).write_text(json.dumps(
            {"target": target, "state": "busy", "source": "claude.UserPromptSubmit", "updated_at": time.time(),
             "turn_started_at": time.time() + 1}))
        started = time.monotonic()
        subprocess.run([sys.executable, str(hook), "busy", "--source", "claude.StopFailure", "--target", target,
                        "--state-dir", str(state), "--session-dir", str(sessions)], input=json.dumps(
                            dict(FIXTURE["stopfailure_payload"], session_id="s-hook", transcript_path=str(log))),
                       text=True, capture_output=True, check=True, timeout=30, env={"PATH": os.environ["PATH"], "HOME": tmp})
        seen["start_stamped_late"] = {"took": round(time.monotonic() - started, 2), "quota": json.loads(
            (state / (target.replace(":", "_") + ".json")).read_text())["provider_error"]["quota"]}
        # A long session: its session-owner record is at the head, far behind the tail a turn end reads.
        log, target = Path(tmp) / "long.jsonl", "pgu-long:0.0"
        transcript(log, "s-long", ACCOUNT)
        with log.open("a") as handle:
            for n in range(400):
                handle.write(json.dumps({"type": "attachment", "n": n, "pad": "x" * 1000}) + "\n")
        subprocess.run([sys.executable, str(hook), "idle", "--source", "claude.Stop", "--target", target, "--state-dir",
                        str(state), "--session-dir", str(sessions)], input=json.dumps(
                            {"session_id": "s-long", "transcript_path": str(log), "hook_event_name": "Stop"}),
                       text=True, capture_output=True, check=True, timeout=30, env={"PATH": os.environ["PATH"], "HOME": tmp})
        checkout = json.loads((state / (target.replace(":", "_") + ".json")).read_text()).get("last_checkout") or {}
        seen["long_session_account"] = (checkout.get("account") or {}).get("account", "")
    return seen


def scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    os.environ["TICKET_BOARD_QUOTA_HOLD_GRACE_SECONDS"] = "1"
    import codex_capacity_retry_test as c594
    import provider_quota_hold_test as rigs
    from temporary_cluster import temporary_cluster

    seen: dict = {"hook": hook_cases(root)}
    with temporary_cluster(prefix="s735", shutdown="immediate") as cluster, tempfile.TemporaryDirectory(prefix="syrd735.") as tmp:
        state_dir, session_dir = Path(tmp) / "state", Path(tmp) / "sessions"
        state_dir.mkdir(), session_dir.mkdir()
        rig = rigs.Rig(cluster, state_dir, session_dir)
        keys: list = []

        def key_runner(argv, **_kwargs):
            keys.append(argv)
            target = argv[argv.index("-t") + 1]
            role = next(r for r, t in rigs.TARGETS.items() if t == target)
            if argv[-1] == "Escape" and role != "ops":  # Ops's menu ignores it: the listener must not keep pressing
                rig.panes[role].screen = screen("after_escape")
            return subprocess.CompletedProcess(argv, 0, "", "")

        rig.gate.key_runner = key_runner
        try:
            def hook(role: str, source: str, state: str, payload: dict) -> None:
                rig.providers[role].run([sys.executable, str(rigs.HOOK), state, "--source", source, "--target",
                                         rigs.TARGETS[role], "--state-dir", str(state_dir), "--session-dir", str(session_dir)],
                                        json.dumps(payload), {"PATH": os.environ["PATH"], "HOME": tmp})

            def turn_end(role: str, account: tuple[str, str]) -> None:
                """A normal turn end, its session's transcript naming `account`."""
                log = Path(tmp) / f"{role}-{time.time_ns()}.jsonl"
                transcript(log, f"s-{role}", account)
                hook(role, "claude.Stop", "idle", {"session_id": f"s-{role}", "transcript_path": str(log),
                                                    "hook_event_name": "Stop", "stop_hook_active": False})

            def refused_turn(role: str, account: tuple[str, str], resets_at: int) -> None:
                """A turn the provider refuses for its weekly limit, in 2.1.318's order: the turn starts, StopFailure
                runs, and the API-error record lands 0.1 s after it starts."""
                log = Path(tmp) / f"{role}-{time.time_ns()}.jsonl"
                transcript(log, f"s-{role}", account)
                hook(role, "claude.UserPromptSubmit", "busy", {"session_id": f"s-{role}", "hook_event_name": "UserPromptSubmit"})
                writer = late_record(log, resets_at)
                hook(role, "claude.StopFailure", "busy", dict(FIXTURE["stopfailure_payload"], session_id=f"s-{role}",
                                                              transcript_path=str(log)))
                writer.join()

            def screen(name: str) -> dict:
                rows = list(FIXTURE["screens"][name])
                y = max(i for i, row in enumerate(rows) if row.lstrip().startswith(("❯", "Enter to confirm")))
                return {"rows": rows, "joined": rows, "cursor": [4, y]}

            def hold(role: str) -> dict:
                found = rig.hold(role)
                return {"state": found.get("state"), "source": (found.get("evidence") or {}).get("source"),
                        "limit": (found.get("evidence") or {}).get("limit"), "reset_at": found.get("reset_at"),
                        "joined": found.get("joined_hold") is not None, "end": found.get("end_reason")} if found else {}

            def as_listener(statement: str) -> str:
                return rig.sql(f"SET ROLE ticket_board_listener;\n{statement}").splitlines()[-1]

            reset = int(time.time()) + 40
            rig.start("PGU-1", "app")
            rig.passes(1.0)  # the hand-over goes first, as the live one had
            turn_end("ops", OTHER)
            turn_end("director", ACCOUNT)  # the Director's pane is on the same account

            # 1. The live race: App's turn refused, the record late. Then a comment for App.
            t0 = time.monotonic()
            refused_turn("app", ACCOUNT, reset)
            rig.passes(1.0)
            app_state = rig.store.read(rigs.TARGETS["app"])
            mark = time.monotonic()
            rig.act("PGU-1", "add_comment", text="Director: any news?")
            rig.passes(1.5)
            seen["race"] = {
                "hold": hold("app"), "pane": f"{app_state.state}/{app_state.source}", "comment_to_app": len(rig.to("app", mark)),
                "resolved": rig.sql("SELECT ticket_board.ticket_turn_is_resolved('PGU-1', clock_timestamp());"),
                # The live priority alert's path: a role stall episode about a pane that reads busy (SYRD-595).
                "stall_episodes": rig.sql("SELECT count(*) FROM ticket_board.role_stall_episodes WHERE ticket_id = 'PGU-1';"),
                "ops_other_account": hold("ops"),
            }

            # 2. Ops on App's account, but its own turn ended after App's failure: its account answered since.
            turn_end("ops", ACCOUNT)
            rig.passes(1.0)
            seen["answered_since"] = hold("ops")

            # 3. Ops's process replaced: the account its predecessor reported is not the replacement's to show.
            rig.register("ops", rigs.Provider(rig.root))
            refused_turn("app", ACCOUNT, reset)
            rig.passes(1.0)
            seen["replaced"] = hold("ops")
            turn_end("ops", ACCOUNT)  # the replacement's own turn end, before App's next failure

            # 4. App's next turn is refused too: Ops, last heard before it, is held with App, never woken.
            mark = time.monotonic()
            refused_turn("app", ACCOUNT, reset)
            rig.passes(1.5)
            seen["shared"] = {"ops": hold("ops"), "app": hold("app"),
                              "director_told": [m for at, r, m in rig.sends if r == "director" and at >= t0 and "out of quota" in m],
                              "director_held": hold("director"), "main_held": hold("main")}
            # The live move: the Director hands Ops a ticket, Ops looking free.
            mark = time.monotonic()
            rig.start("PGU-2", "ops")
            rig.passes(1.5)
            seen["shared"]["to_ops"] = rig.to("ops", mark)

            # 5. The menu: on held App's pane; on held Ops's, where Esc does nothing; on the Director's, not held.
            rig.panes["app"].screen = rig.panes["ops"].screen = rig.panes["director"].screen = screen("usage_limit_menu")
            rig.passes(2.0)
            seen["menu"] = {"keys": sorted([a for a in argv if a not in ("tmux", "send-keys", "-t")] for argv in keys),
                            "app_screen_menu": "Stop and wait for limit to reset" in "\n".join(rig.panes["app"].screen["rows"])}
            # Another question on held App's pane: never answered.
            trust = json.loads((ROOT / "tests/fixtures/syrd566_provider_screens.json").read_text())["captures"]["claude_folder_trust"]
            rig.panes["app"].screen = {"rows": trust["rows"], "joined": trust["rows"], "cursor": trust["cursor"]}
            before_trust = len(keys)
            rig.passes(1.0)
            seen["menu"]["keys_on_other_question"] = keys[before_trust:]
            rig.panes["app"].screen = screen("after_escape")
            rig.panes["ops"].screen = screen("after_escape")
            rig.panes["director"].screen = c594.screens()["director"]

            # The Director clears Ops's hold: it stays cleared while App's hold stands, and Ops's hand-over goes.
            mark = time.monotonic()
            if hold("ops").get("state") == "held":  # main's tree holds nothing to clear
                rig.op("clear_provider_hold", role="ops", reason="Ops is on a topped-up account")
            rig.passes(1.5)
            seen["cleared"] = {"ops": hold("ops"), "to_ops": len(rig.to("ops", mark)), "app": hold("app").get("state")}

            # The stall evaluator on a held owner's ticket, nothing else in its way (the Director's live case): Main,
            # held on the board's own say, with an idle pane and a delivered hand-over.
            rig.start("PGU-3", "main")
            rig.passes(1.0)
            rig.note("main", observed_at=datetime.now(timezone.utc).isoformat(), turn="stopfailure:direct",
                     reset_at=datetime.fromtimestamp(reset, timezone.utc).isoformat(), limit="seven_day")
            # Only the hold may answer: an unresolved-turn stall of Main's own (main's tree prompts it) is closed first.
            rig.sql("UPDATE ticket_board.role_stall_episodes SET closed_at = clock_timestamp() WHERE ticket_id = 'PGU-3' "
                    "AND closed_at IS NULL;")
            seen["stall_why"] = as_listener("SELECT coalesce(why_not, '<runnable>') FROM ticket_board.board_stall_runnable("
                                            "clock_timestamp()) WHERE ticket_id = 'PGU-3';")

            # 6. Reset plus grace: both released, and what waited for them is delivered.
            rig.mode["app"] = rig.mode["ops"] = "ok"
            mark = time.monotonic()
            rig.passes(max(reset + 1 + 3 - time.time(), 1.0))
            seen["reset"] = {"to_app": len(rig.to("app", mark)), "to_ops": len(rig.to("ops", mark)),
                             "app": hold("app"), "ops": hold("ops")}
            # The menu on App's pane once its hold has ended: old text, and no hold to vouch for it. Never pressed.
            before_stale = len(keys)
            rig.panes["app"].screen = screen("usage_limit_menu")
            rig.passes(1.0)
            seen["reset"]["keys_after_hold"] = keys[before_stale:]
        finally:
            rig.close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    # The measured records are this ticket's: main's tree is given them, nothing else of this one.
    (into / "tests/fixtures").mkdir(parents=True, exist_ok=True)
    (into / "tests/fixtures/syrd735_claude_usage_limit.json").write_text(json.dumps(FIXTURE))
    return into


def run(root: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root)],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def before(got: dict) -> None:
    race = got["race"]
    check(race["hold"] == {} and race["pane"] == "busy/claude.UserPromptSubmit" and race["comment_to_app"] == 0,
          f"main: the late record leaves App unheld and its pane busy, so a comment waits on a busy pane: {race}")
    check(race["resolved"] == "f" and race["stall_episodes"] != "0",
          f"main: App's turn reads unresolved, and a role stall opens about a busy pane -- the live priority alert's "
          f"path: {race}")
    check(got["stall_why"] == "<runnable>", f"main: the stall evaluator counts a held owner's ticket as runnable: "
                                            f"{got['stall_why']}")
    stale = got["hook"]
    check(stale["no_record_yet"]["quota"].get("status") == "rejected" and stale["late_record"]["quota"].get("resetsAt")
          != stale["late_record"]["fresh"],
          f"main: an earlier turn's record is taken as this turn's: {stale}")
    check(got["shared"]["ops"] == {} and len(got["shared"]["to_ops"]) >= 1 and got["menu"]["keys"] == [],
          f"main: Ops on the exhausted account is not held, and a hand-over wakes it; nothing is pressed on the menu: "
          f"{got['shared']} {got['menu']}")


def candidate(got: dict) -> None:
    race = got["race"]
    check(race["hold"].get("state") == "held" and race["hold"].get("source") == "claude.StopFailure"
          and race["hold"].get("limit") == "seven_day" and race["pane"] == "idle/claude.StopFailure",
          f"the late record is waited for: App is held on its weekly limit, its pane idle: {race}")
    check(race["comment_to_app"] == 0 and race["resolved"] == "t" and race["stall_episodes"] == "0",
          f"the comment waits for the hold, App's turn reads resolved, and no role stall opens: {race}")
    check(got["stall_why"] == "main's provider is out of quota, and its work waits for the reset",
          f"the stall evaluator does not count a held owner's ticket, and says why: {got['stall_why']}")
    hook = got["hook"]
    check(hook["no_record_yet"]["quota"] == {} and 1.9 <= hook["no_record_yet"]["took"] < 5,
          f"an earlier turn's record is never this turn's: with none of its own, after a bounded wait, no quota: {hook}")
    check(hook["late_record"]["quota"].get("resetsAt") == hook["late_record"]["fresh"]
          and hook["late_record"].get("state") == "idle" and hook["late_record"]["took"] < 1.5,
          f"and the record that lands late is this turn's, taken as soon as it lands: {hook}")
    check(race["ops_other_account"] == {}, f"Ops on another account is not held: {race['ops_other_account']}")
    check(got["answered_since"] == {}, f"Ops on App's account, whose own turn ended after App's failure, is not held: "
                                       f"{got['answered_since']}")
    check(got["replaced"] == {}, f"nor is Ops's replacement on its predecessor's account record: {got['replaced']}")
    check(hook["start_stamped_late"]["quota"].get("status") == "rejected" and hook["start_stamped_late"]["took"] < 1.5,
          f"a record from just before the turn-start hook's stamp is still this turn's: {hook['start_stamped_late']}")
    check(len(hook["long_session_account"]) == 32, f"a long session's account is read from its transcript's head: "
                                                     f"{hook['long_session_account']!r}")
    shared = got["shared"]
    check(shared["ops"].get("state") == "held" and shared["ops"].get("source") == "claude.shared_account"
          and shared["ops"].get("reset_at") == shared["app"].get("reset_at") and shared["ops"].get("joined"),
          f"Ops, on App's account and last heard before App's latest failure, is held with it, to the same reset, "
          f"joining App's notice: {shared}")
    check(shared["to_ops"] == [] and len(shared["director_told"]) == 1,
          f"without being woken: a hand-over to Ops waits, and the Director is told once: {shared}")
    check(shared["director_held"] == {} and shared["main_held"] == {},
          f"the Director's pane and Codex Main are never held this way: {shared}")
    menu = got["menu"]
    check(menu["keys"] == [["pgu-app:0.0", "Escape"], ["pgu-ops:0.0", "Escape"], ["pgu-ops:0.0", "Escape"]]
          and not menu["app_screen_menu"],
          f"held App's usage-limit menu gets one Esc, which dismisses it; held Ops's, ignoring Esc, gets two and is then "
          f"left to a person; the Director's, not held, gets nothing; never Enter or a digit: {menu}")
    check(menu["keys_on_other_question"] == [], f"another question on a held pane is never answered: {menu}")
    cleared = got["cleared"]
    check(cleared["ops"].get("end") == "cleared: Ops is on a topped-up account" and cleared["to_ops"] >= 1
          and cleared["app"] == "held",
          f"a Director's clear of a shared hold sticks while its source stands, and Ops's hand-over goes: {cleared}")
    reset = got["reset"]
    check(reset["to_app"] >= 1 and reset["app"].get("state") != "held" and reset["ops"].get("state") != "held",
          f"at reset plus grace App is released, and the work waiting for it is delivered: {reset}")
    check(reset["keys_after_hold"] == [], f"a usage-limit menu on a pane whose hold has ended is never pressed: {reset}")
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    mine = (ROOT / "scripts/ticket_board/migrations/pgu1030_syrd735_held_owner_not_runnable.sql").read_text()
    start = schema.rindex("CREATE OR REPLACE FUNCTION ticket_board.board_stall_runnable(")
    check(schema[start:schema.index("$$;", schema.index("AS $$", start) + 5) + 3] in mine,
          "schema.sql's board_stall_runnable, the one a board built from it runs, is pgu1030's")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]))))
        return 0
    if os.environ.get("SYRD735_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        with tempfile.TemporaryDirectory(prefix="syrd735-before.", dir="/tmp") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(run(ROOT))
    print(f"claude_usage_limit_hold_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
