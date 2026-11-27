#!/usr/bin/env python3
"""SYRD-891: an agy role whose tool calls are all being denied is told to the Director once, whatever the cause.

On ocstab (2026-11-27) a Researcher on agy 1.5.17 had every tool call refused, "tool call denied by pre-tool hook: "
with no reason, for two hours on OCSTAB-4, and no alert reached the Director or the operator.

* **What agy records** -- `tool_denials.streak` on agy 1.5.17's own transcripts (tests/fixtures/syrd891_transcripts,
  recorded offline by SYRD-890's probe): three calls denied with no reason is a run of three; the same three calls
  run is none; one refusal with the guard's reason is a run of one. And on written transcripts: a call that runs ends
  the run; each call of a response counts; a user's new prompt ends an unanswered response; an error that is not a
  refusal ends the run; the first, cut line of a tail is skipped.
* **The likely cause** (`suspects`): a hook's denial with no reason names SYRD-890's fix, first (SYRD-893: on ocstab
  agy's 403 line was there too, and the re-login did not help); agy's own log saying its login lacked an OAuth scope
  (the 403 `fetch_config` line, read from the newest log of the transcript's home) follows it, or comes first without
  it, with the re-login; a hook's reason is pointed at; agy's own permission layer is named; anything else is quoted
  as it is. The quoted refusal and log line are one line with no control characters (SYRD-893).
* **The current pane's calls** (SYRD-894): only outcomes agy recorded since the role's current pane registered count,
  so a run from before a restart is not reported after it (ocstab's false alert), and a run that goes on after it
  counts, and is keyed, from its first call in the new pane.
* **The role's own record** (`observe`): the transcript its SessionStart record names; nothing below `THRESHOLD`; a
  record naming anything but agy's transcript file is not read; an unchanged transcript is answered from the cache.
* **The board**, built fresh from schema.sql and upgraded from the parent tree through its migrations (pgu1063), with
  main and ops on agy and the real listener: the pass (`tool_denials.run`, as the listener runs it) queues one notice
  for main, on main's ticket, naming the role, the count, agy's words and SYRD-890's fix; the listener delivers it to
  the Director's pane; the next pass queues nothing; a run in a new conversation is told again; a role with no ticket
  is told once it has one; a notice whose ticket moved before delivery is dropped; a role whose calls run is never
  told; the Director's own pane is never told about, though it has a ticket; only
  the listener may report; a workflow with no agy role is asked nothing; after a restart (the pane registers again) a
  run from before it is not told, and a new run in the new pane is, once. schema.sql's note_tool_denials is pgu1063's.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import psycopg  # noqa: E402

from documentation_gate_board_test import example  # noqa: E402
from router_role_board_test import Board, fixture, temporary_cluster  # noqa: E402
from schema_function_drift import assert_no_drift, owning_migration  # noqa: E402
from scripts.ticket_board import tool_denials as td  # noqa: E402

CHECKS = 0
FIXTURES = ROOT / "tests" / "fixtures" / "syrd891_transcripts"
MIGRATION = "scripts/ticket_board/migrations/pgu1063_syrd891_tool_denial_alerts.sql"
SCOPE = ("W1127 08:27:03.1 1 aicode_fallback.go:102] AICode request for fetch_config lacked OAuth scope (failed to make "
         "AI Code FetchConfig request: PERMISSION_DENIED (code 403): Request had insufficient authentication scopes.); "
         "falling back to CCPA")


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def lines(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


def call(i: int, n: int = 1) -> str:
    return json.dumps({"step_index": i, "type": "PLANNER_RESPONSE", "status": "DONE",
                       "tool_calls": [{"name": "run_command", "args": {"CommandLine": "pwd"}}] * n})


def outcome(i: int, error: str = "") -> str:
    step = {"step_index": i, "type": "GENERIC", "status": "ERROR" if error else "DONE", "created_at": f"t{i}"}
    return json.dumps({**step, "error": error} if error else step)


DENY = "tool call denied by pre-tool hook: "


def denied(steps: list[str]) -> int:
    """How long the trailing run of refusals is: 0 for none, so a wrong answer fails a check rather than crashing it."""
    return (td.streak(steps) or {}).get("denied", 0)


def test_what_agy_records() -> None:
    seen = td.streak(lines("denied_no_reason.jsonl"))
    check(seen and seen["denied"] == 3 and seen["ran_before"] == 0 and seen["last_error"] == DENY
          and seen["first_step"] == 2, f"agy's three calls denied with no reason are a run of three: {seen}")
    check(td.streak(lines("ran.jsonl")) is None, "the same three calls run are none")
    seen = td.streak(lines("refused_with_reason.jsonl"))
    check(seen and seen["denied"] == 1 and "Switchyard refused this command" in seen["last_error"],
          f"a refusal with the guard's reason is a run of one: {seen}")
    check(denied([call(1), outcome(2, DENY), call(3), outcome(4), call(5), outcome(6, DENY)]) == 1,
          "a call that runs ends the run")
    check(td.streak([call(1), outcome(2), call(3, 3), outcome(4, DENY), outcome(5, DENY), outcome(6, DENY)])
          == {"denied": 3, "ran_before": 1, "first_step": 4, "first_at": "t4", "last_at": "t6", "last_error": DENY},
          "each call of one response counts")
    user = json.dumps({"step_index": 3, "type": "USER_INPUT", "status": "DONE"})
    check(denied([call(1, 2), outcome(2, DENY), user, json.dumps({"step_index": 4, "type": "GENERIC",
                                                                   "status": "DONE"})]) == 1,
          "a new prompt ends an unanswered response: what follows it is no call's outcome")
    check(td.streak([call(1), outcome(2, DENY), call(3), outcome(4, "invalid arguments: missing toolSummary")]) is None,
          "an error that is not a refusal ends the run")
    check(denied(['ep_index": 9, "status": "ERROR"}', call(1), outcome(2, DENY)]) == 1,
          "a tail's cut first line is skipped")


def test_only_the_current_panes_calls_count() -> None:
    """SYRD-894: a run counts only calls agy recorded since the role's current pane started."""
    start = 1_800_000_000.25
    old = retimed("denied_no_reason.jsonl", start - 60)
    check(td.streak(old, since=start) is None, "denials all from before the pane's start: no run")
    check((td.streak(old) or {}).get("denied") == 3, "with no start known, as before: the run of three")
    spanning = retimed("denied_no_reason.jsonl", start - 20) + retimed("denied_no_reason.jsonl", start + 10, 20)[1:]
    seen = td.streak(spanning, since=start) or {}
    check(seen.get("denied") == 3 and seen.get("first_step") == 22 and seen.get("ran_before") == 0,
          f"a run that goes on after the restart counts from its first call in this pane, keyed by it: {seen}")
    untimed = [call(1), json.dumps({"step_index": 2, "type": "GENERIC", "status": "ERROR", "error": DENY})]
    check(td.streak(untimed, since=start) is None, "an outcome with no time cannot be placed in this pane")
    same_second = [call(1), json.dumps({"step_index": 2, "type": "GENERIC", "status": "ERROR", "error": DENY,
                                        "created_at": stamp(start)})]
    check((td.streak(same_second, since=start) or {}).get("denied") == 1,
          "agy's whole second of the start counts: it cannot say which side of the start it fell")


def test_the_likely_cause() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd891-home.") as tmp:
        home = Path(tmp) / ".gemini"
        transcript = home / "antigravity-cli" / "brain" / "c1" / ".system_generated" / "logs" / "transcript_full.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("\n".join(lines("denied_no_reason.jsonl")) + "\n")
        log = home / "antigravity-cli" / "log"
        log.mkdir(parents=True)
        (log / "cli-20261127_082700.log").write_text("I1127 started\n" + SCOPE + "\nI1127 loaded 1 named hooks\n")
        check(td.scope_line(transcript) == SCOPE[:td.ERROR_CHARS], "agy's own 403 scope line is found in its newest log")
        newer = log / "cli-20261127_090000.log"
        newer.write_text("I1127 a clean start after the re-login\n")
        check(td.scope_line(transcript) == "", "only the newest log counts: a clean start after a re-login says nothing")
        newer.unlink()  # back to ocstab's case: the 403 line in the newest log, and the refusals with no reason
        sessions = session(Path(tmp), "pgu-main:0.0", "c1", "denied_no_reason.jsonl")
        record = json.loads((sessions / "pgu-main_0.0.json").read_text())
        record["payload"]["transcriptPath"] = str(transcript)  # the transcript in the home whose log has the 403 line
        (sessions / "pgu-main_0.0.json").write_text(json.dumps(record))
        found = (td.observe(str(sessions), "pgu-main:0.0") or {}).get("suspects", [])
    check(len(found) == 2 and "SYRD-890" in found[0] and found[1].startswith("agy's own log says its login lacked an "
          "OAuth scope") and "sign agy in again" in found[1],
          f"SYRD-893: with both, as on ocstab, the hook's empty denial -- evidence about the call -- comes first, and "
          f"the login after it: {found}")
    found = td.suspects({"last_error": DENY + "Switchyard refused this command"}, SCOPE)
    check(found[0].startswith("agy's own log says its login lacked an OAuth scope")
          and found[1] == "a pre-tool hook refused it, for the reason quoted",
          f"without the empty denial, the login still comes first: {found}")
    check(td.suspects({"last_error": DENY + "Switchyard refused this command"}, "")
          == ["a pre-tool hook refused it, for the reason quoted"], "a hook's own reason is pointed at")
    check("--dangerously-skip-permissions" in td.suspects({"last_error": "User denied permission to run command:"}, "")[0],
          "agy's own permission layer is named")
    check(td.suspects({"last_error": "denied-repeat pre-check blocked: x"}, "")[0].startswith("a cause Switchyard does "
          "not recognise"), "anything else is quoted as it is")


def stamp(epoch: float) -> str:
    """An epoch as agy writes `created_at`: whole seconds, UTC."""
    return datetime.fromtimestamp(int(epoch), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def retimed(fixture_name: str, at: float, first_index: int = 0) -> list[str]:
    """A recorded transcript's steps as if agy had written them from `at`, one second apart; step indexes from
    `first_index`. Only the time and the index change: agy's words, types and statuses are as recorded."""
    out = []
    for i, line in enumerate(lines(fixture_name)):
        step = json.loads(line)
        step["created_at"], step["step_index"] = stamp(at + i), first_index + i
        out.append(json.dumps(step))
    return out


def session(root: Path, target: str, conversation: str, fixture_name: str, at: float | None = None) -> Path:
    """The role's SessionStart record, as the pane hook writes it, naming agy's transcript in its home. The transcript's
    steps are the recorded fixture's, timed from `at` (now, by default: after the role's current pane registered)."""
    home = root / f"home-{conversation}" / ".gemini"
    transcript = home / "antigravity-cli" / "brain" / conversation / ".system_generated" / "logs" / "transcript_full.jsonl"
    transcript.parent.mkdir(parents=True, exist_ok=True)
    transcript.write_text("\n".join(retimed(fixture_name, time.time() if at is None else at)) + "\n")
    sessions = root / "sessions"
    sessions.mkdir(exist_ok=True)
    safe = "".join(ch if ch.isalnum() or ch in ".-" else "_" for ch in target)
    (sessions / f"{safe}.json").write_text(json.dumps({
        "target": target, "session_id": conversation, "source": "gemini.SessionStart",
        "payload": {"conversationId": conversation, "hookEventName": "SessionStart",
                    "transcriptPath": str(transcript)}}))
    return sessions


def test_the_quoted_refusal_is_one_plain_line() -> None:
    """SYRD-893: agy's refusal, partly shaped by the role's own command, and agy's log line reach the notice without
    control characters, on one line, as report_closure's notices are."""
    nasty = "tool call denied with reason: \x1b[31mrm -rf /\x1b[0m\nnext line\x07\tend\x9b2J"
    with tempfile.TemporaryDirectory(prefix="syrd893-plain.") as tmp:
        root = Path(tmp)
        sessions = session(root, "pgu-main:0.0", "c-9", "denied_no_reason.jsonl")
        transcript = Path(json.loads((sessions / "pgu-main_0.0.json").read_text())["payload"]["transcriptPath"])
        with transcript.open("a") as f:
            f.write(call(8) + "\n" + outcome(9, nasty) + "\n")
        log = transcript.parents[4] / "log"
        log.mkdir(parents=True)
        (log / "cli-20261127_082700.log").write_text("W\x1b[33m " + SCOPE + "\x1b[0m\n")
        seen = td.observe(str(sessions), "pgu-main:0.0") or {}
    error, text = seen.get("last_error", ""), " ".join(seen.get("suspects", []))
    controls = [c for c in error + text if ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f]
    check(seen.get("denied") == 4 and not controls and "\n" not in error,
          f"no escape, BEL, tab, newline or C1 character reaches the notice: {controls} {error!r}")
    check(error == "tool call denied with reason: [31mrm -rf /[0m next line end2J",
          f"and the words stay, on one line: {error!r}")
    check("lacked OAuth scope" in text and "\x1b" not in text, f"agy's own log line too: {text!r}")


def test_the_roles_own_record() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd891-record.") as tmp:
        root = Path(tmp)
        sessions = session(root, "pgu-main:0.0", "c-1", "denied_no_reason.jsonl")
        seen = td.observe(str(sessions), "pgu-main:0.0")
        check(seen and seen["episode"] == "c-1:2" and seen["denied"] == 3 and seen["conversation"] == "c-1",
              f"the transcript the role's SessionStart record names: {seen}")
        check(td.observe(str(session(root, "pgu-ops:0.0", "c-2", "refused_with_reason.jsonl")), "pgu-ops:0.0") is None,
              "a run below the threshold is not reported")
        record = sessions / "pgu-main_0.0.json"
        data = json.loads(record.read_text())
        data["payload"]["transcriptPath"] = str(root / "secrets.txt")
        (root / "secrets.txt").write_text("\n".join(lines("denied_no_reason.jsonl")))
        record.write_text(json.dumps(data))
        check(td.observe(str(sessions), "pgu-main:0.0") is None, "a record naming anything but agy's transcript is not read")
        sessions = session(root, "pgu-main:0.0", "c-3", "denied_no_reason.jsonl")
        cache: dict = {}
        first = td.observe(str(sessions), "pgu-main:0.0", cache)
        check(td.observe(str(sessions), "pgu-main:0.0", cache) is first, "an unchanged transcript is answered from the cache")
        check(first is not None and td.observe(str(sessions), "pgu-main:0.0", cache, since=time.time() + 5) is None,
              "but not across a restart: the same transcript, a pane started after its run, no run (SYRD-894)")
        transcript = Path(json.loads((sessions / "pgu-main_0.0.json").read_text())["payload"]["transcriptPath"])
        with transcript.open("a") as f:
            f.write(call(8) + "\n" + outcome(9) + "\n")
        check(td.observe(str(sessions), "pgu-main:0.0", cache) is None, "and a changed one is read again: a call ran")


def parent_tree(into: Path) -> Path:
    adding = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--", MIGRATION],
                            check=True, capture_output=True, text=True).stdout.split()
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", f"{adding[-1]}^" if adding else "HEAD", "scripts"],
                             check=True, capture_output=True)
    subprocess.run(["tar", "-x", "-C", str(into)], input=archive.stdout, check=True)
    check(not (into / MIGRATION).exists(), "the parent tree has no pgu1063")
    return into


def agy_document() -> dict:
    doc = copy.deepcopy(example())
    for role in doc["roles"]:
        if role["name"] in ("main", "ops"):
            role["runtime"] = "agy"
    return doc


class Untouchable:
    """A connection no statement may reach."""

    def execute(self, *args, **kwargs):
        raise AssertionError(f"FAILED CHECK: a board with no agy role was asked {args[:1]}")


def test_no_agy_role_asks_nothing() -> None:
    import logging
    listener = type("L", (), {"role_runtimes": {"main": "codex", "director": "claude"},
                              "logger": logging.getLogger("syrd891")})()
    check(td.run(listener, Untouchable()) == 0, "a workflow with no agy role: no statement, nothing queued")


def board_scenario(board: Board, name: str, tmp: Path) -> None:
    for role in ("main", "ops"):
        board.sql(f"UPDATE ticket_board.role_runtime_assignments SET session_dir = '{tmp / role / 'sessions'}' "
                  f"WHERE role = '{role}';")
    fixture.seed_postgres_ticket(board.admin, "PGU-971", title="Measure the rail", state="in_progress", assignee="main")
    session(tmp / "main", "pgu-main:0.0", "conv-a", "denied_no_reason.jsonl")
    session(tmp / "ops", "pgu-ops:0.0", "conv-o", "denied_no_reason.jsonl")
    listener = board.listener
    with psycopg.connect(board.listener_url, autocommit=True) as conn:
        listener.refresh_workflow(conn)  # the declared workflow's runtimes, as every pass has them
        queued = td.run(listener, conn)
    notices = board.sql("SELECT coalesce(json_agg(json_build_object('ticket', ticket_id, 'message', message) ORDER BY id), "
                        "'[]')::text FROM ticket_board.ticket_notification_queue WHERE target_role = 'director' "
                        "AND payload->>'kind' = 'tool_denials';")
    notices = json.loads(notices)
    check(queued == 1 and len(notices) == 1 and notices[0]["ticket"] == "PGU-971",
          f"{name}: one notice, for main, on its ticket; ops has no ticket yet: {queued} {notices}")
    message = notices[0]["message"]
    check(message.startswith("main, on agy, has had its last 3 tool calls denied and none run since ")
          and f'agy said "{DENY.strip()}"' in message and "SYRD-890" in message and "PGU-971" in message,
          f"{name}: naming the role, the count, agy's words and SYRD-890's fix: {message}")
    sent = board.one_pass()
    check(any(target == "pgu-director:0.0" and "has had its last 3 tool calls denied" in text for target, text in sent),
          f"{name}: the listener delivers it to the Director's pane: {sent}")
    with psycopg.connect(board.listener_url, autocommit=True) as conn:
        check(td.run(listener, conn) == 0, f"{name}: the next pass queues nothing: told once")
        session(tmp / "main", "pgu-main:0.0", "conv-b", "denied_no_reason.jsonl")
        check(td.run(listener, conn) == 1, f"{name}: a run in a new conversation is told again")
        fixture.seed_postgres_ticket(board.admin, "PGU-972", title="Tune the loop", state="in_progress", assignee="ops")
        check(td.run(listener, conn) == 1, f"{name}: ops is told once it has a ticket")
    moved = board.act("PGU-972", "defer", "director")
    board.busy.clear()  # the Director's pane took the first notice and is free again
    sent = board.one_pass()
    left = board.sql("SELECT count(*) FROM ticket_board.ticket_notification_queue WHERE payload->>'kind' = 'tool_denials' "
                     "AND payload->>'role' = 'ops';")
    check("ok" in moved and any("main, on agy" in text for _target, text in sent)
          and not any("ops, on agy" in text for _target, text in sent) and left == "0",
          f"{name}: in the same pass main's second notice is delivered and ops's, whose ticket moved, is dropped: "
          f"{moved} {sent} left={left}")
    with psycopg.connect(board.listener_url, autocommit=True) as conn:
        session(tmp / "main", "pgu-main:0.0", "conv-c", "ran.jsonl")
        check(td.run(listener, conn) == 0, f"{name}: a role whose calls run is never told")
        # SYRD-894: ocstab's false alert. Denials from before the pane's current start, then a restart (the pane
        # registers again), then a pass before the new pane's first call: the resumed transcript still ends on the
        # old run, and it is not this pane's.
        sessions = session(tmp / "main", "pgu-main:0.0", "conv-r", "denied_no_reason.jsonl", at=time.time() - 120)
        board.sql("UPDATE ticket_board.role_runtime_assignments SET generation = generation + 1, "
                  "registered_at = clock_timestamp() WHERE role = 'main';")
        check(td.run(listener, conn) == 0,
              f"{name}: a run that ended before the pane's current start is not reported after the restart")
        transcript = Path(json.loads((sessions / "pgu-main_0.0.json").read_text())["payload"]["transcriptPath"])
        time.sleep(1.1)  # the new pane's calls land after its registration, in a later second
        with transcript.open("a") as f:
            f.write("\n".join(retimed("denied_no_reason.jsonl", time.time(), first_index=20)[1:]) + "\n")
        check(td.run(listener, conn) == 1, f"{name}: a new run in the new pane's generation is told")
        check(td.run(listener, conn) == 0, f"{name}: once")
        fixture.seed_postgres_ticket(board.admin, "PGU-973", title="Triage", state="analysis", assignee="director")
        director = json.dumps({"director": {"episode": "d:1", "denied": 5, "suspects": []}})
        check(conn.execute("SELECT ticket_board.note_tool_denials(%s::jsonb)", (director,)).fetchone()[0] == 0,
              f"{name}: the Director's own pane is never told about")
    try:
        with psycopg.connect(board.service, autocommit=True) as conn:
            conn.execute("SELECT ticket_board.note_tool_denials('{}'::jsonb)")
        refused = ""
    except psycopg.Error as exc:
        refused = str(exc)
    check(refused, f"{name}: only the listener may report")
    check(board.sql("SELECT count(*) FROM ticket_board.tool_denial_alerts;") == "4",
          f"{name}: one record per role and run")


def test_the_same_function_fresh_and_upgraded() -> None:
    owner = owning_migration("note_tool_denials").name
    check(owner == Path(MIGRATION).name, f"the newest migration defining note_tool_denials is pgu1063: {owner}")
    try:
        assert_no_drift("note_tool_denials")
        drift = ""
    except AssertionError as exc:
        drift = str(exc)
    check(not drift, f"schema.sql's note_tool_denials is pgu1063's: {drift}")


def test_the_listener_runs_it() -> None:
    listener = (ROOT / "scripts/ticket_board/notify_listener.py").read_text()
    check("tool_denials.run(self, conn)" in listener and ", tool_denials, " in listener,
          "every listener pass runs it, beside the stall and capacity passes")


def main() -> int:
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    with temporary_cluster(prefix="syrd891-", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd891-board.") as tmp:
        for name, before in (("fresh", None), ("upgraded", parent_tree(Path(tmp) / "parent"))):
            board = Board(cluster, name, agy_document(), before=before)
            try:
                board_scenario(board, name, Path(tmp) / name)
            finally:
                board.close()
    print(f"tool_denials_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
