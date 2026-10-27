#!/usr/bin/env python3
"""SYRD-695: a turn's tool calls are the turn moving, to the busy-pane escalation.

On SYRD-691 Ops took the ticket at 09:42:16 UTC and worked one continuous turn,
a tool call every few seconds, until 10:03:55. At 10:00:54 the Director was sent
"PRIORITY SYRD-691 ... its pane has said busy since, with nothing moving on
screen". Claude Code's and Codex's board hooks write only a turn's start and
end, so nothing the pane did in between reached the board; no call lived long
enough to earn an execution lease; a 1.2 s screen sample saw nothing change.

Here hooks are installed by the real installer into a disposable home, and a
real provider process -- a pane under a process named `tmux: server` -- runs
the hook commands it installed, as Claude Code and Codex run them. The real
notify listener passes over a production-built board, its pane screens static.
The hand-over and the turn are dated 2400 s back, past the 600 s grace and the
board's 1800 s stall threshold:

* `control`: a busy turn, no tool call: one escalation, delivered;
* `tool-calls`: the same with a run of short tool calls just now (Claude): no
  escalation, the pane's state untouched by them;
* `codex`: the same for Codex, whose busy hook the listener rewrites to idle
  after 120 s of a still screen (`stale_codex_busy_recovery`) and then types
  notices into: a turn calling tools stays busy, and nothing is typed into it;
  `codex-silence`: a silent Codex turn is recovered to idle as before;
* `silence`: tool calls, the last 1900 s ago: one escalation, bound to that
  call, and only one; `quiet`: the last 900 s ago, past the grace but not the
  threshold from it: no escalation yet;
* `queued-then-call`: escalated while the Director's pane is busy, then a tool
  call: delivery drops it (`hook_activity`), and the episode ends;
  `queued-then-call-pass`: the same caught by the next pass;
* calls that say nothing about this turn still alert: run by a `claude -p` the
  pane's provider started (`nested`), left by a replaced provider
  (`replaced`), before the turn started (`earlier-turn`), from a reused pid
  (`reused-pid`), or dated in the future (`future`);
* a tool hook after the turn's Stop leaves the pane idle, and the hook's word
  its own.

The before-run is main's tree, from a git archive, with the same steps.
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
BEFORE = "df9516244a9ab56283d76a0f1a409f9c09f377bb"  # main before SYRD-695
TARGETS = {"director": "pgu-director:0.0", "ops": "pgu-ops:0.0", "app": "pgu-app:0.0", "main": "pgu-main:0.0"}
RUNTIMES = {"director": "claude", "ops": "claude", "app": "claude", "main": "codex"}
GRACE = 600
THRESHOLD = 1800
BACK = 2400  # how long before each case's first pass its ticket was handed over
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"{detail}\nFAILED CHECK: {detail.split(': ', 1)[0]}")
    CHECKS += 1


def scenario(root: Path) -> dict:
    sys.path[:0] = [str(root), str(root / "tests")]
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import codex_capacity_retry_test as c594
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from provider_quota_hold_test import Provider
    from temporary_cluster import temporary_cluster

    from scripts.ticket_board import notify_listener as nl

    for key in [k for k in os.environ if k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL"))]:
        os.environ.pop(key)
    os.environ["TICKET_BOARD_BOARD_STALL_SECONDS"] = str(THRESHOLD)
    out: dict = {}
    with temporary_cluster(prefix="s695", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="s695.", dir="/tmp") as tmp:
        home, state_dir, session_dir = Path(tmp) / "home", Path(tmp) / "state", Path(tmp) / "sessions"
        for d in (home, state_dir, session_dir):
            d.mkdir()
        hook_env = {"PATH": os.environ["PATH"], "HOME": str(home), "TICKET_BOARD_PROJECT": "pgu",
                    "TICKET_BOARD_PANE_STATE_DIR": str(state_dir), "TICKET_BOARD_PANE_SESSION_DIR": str(session_dir)}
        # The hooks as the installer writes them for an owner: the commands a provider runs are read from its config.
        installed = subprocess.run(
            [sys.executable, str(root / "scripts/ticket-board-install-pane-hooks"), "install", "--home", str(home),
             "--hook-source", str(root / "scripts/ticket-board-pane-idle-hook"),
             "--bin-path", str(home / ".local/bin/ticket-board-pane-idle-hook"),
             "--claude-permission-hook", str(root / "scripts/ticket-board-claude-permission-hook")],
            env=hook_env, capture_output=True, text=True, timeout=60)
        assert installed.returncode == 0, installed.stderr
        configs = {"claude": json.loads((home / ".claude/settings.json").read_text()),
                   "codex": json.loads((home / ".codex/hooks.json").read_text())}

        def commands(cli: str, event: str) -> list[str]:
            return [h["command"] for entry in configs[cli].get("hooks", {}).get(event, []) for h in entry.get("hooks", [])
                    if "ticket-board-pane-idle-hook" in h.get("command", "")]

        out["installed"] = {cli: {event: [c.split(" --state-dir")[0].split("ticket-board-pane-idle-hook ")[-1]
                                          for c in commands(cli, event)] for event in ("PreToolUse", "PostToolUse")}
                            for cli in configs}

        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board",
                                                                   fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        api = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        def replica(statement: str) -> None:
            # Arranged history, dated past the board's triggers: not a write.
            sql(f"SET session_replication_role = replica; {statement} SET session_replication_role = origin;")

        def iso(epoch: float) -> str:
            return datetime.fromtimestamp(epoch, timezone.utc).isoformat()

        store = nl.PaneHookStateStore(state_dir)
        shown = c594.screens()
        providers = {role: Provider(state_dir.parent) for role in TARGETS}
        spawned = list(providers.values())
        class ClaudePane(c594.Pane):
            def table(self) -> list[tuple]:
                return [(self.pid, 1, 50, self.pid, self.started)]  # no daemon of its own: c594's is Codex's

        # Every pane's screen is still: a sample sees nothing move, as it saw nothing on SYRD-691.
        panes = {role: (c594.Pane if RUNTIMES[role] == "codex" else ClaudePane)(shown["director"], p.pid, p.started)
                 for role, p in providers.items()}
        tmux, table = c594.fake_tmux({TARGETS[r]: pane for r, pane in panes.items()})

        def register(role: str, provider) -> None:
            previous = sql(f"SELECT coalesce((SELECT generation FROM ticket_board.role_runtime_assignments "
                           f"WHERE role = '{role}'), 0);")
            sql(f"SELECT ticket_board.register_role_runtime('{role}', '{RUNTIMES[role]}', '{TARGETS[role]}', '/tmp', "
                f"'/tmp', {provider.pid}, {provider.ticks}, {os.getuid()}, {previous});")
            panes[role].pid, panes[role].started = provider.pid, provider.started

        for role, provider in providers.items():
            register(role, provider)
            store.write(TARGETS[role], "idle", source=f"{RUNTIMES[role]}.Stop", now=time.time() - 3600)
        # Registered long before: a runtime registering is progress on the board's clock (SYRD-675).
        replica(f"UPDATE ticket_board.role_runtime_assignments SET registered_at = '{iso(time.time() - 3 * BACK)}';")
        gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                   pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                   director_startup_hold_seconds=0, role_runtimes=RUNTIMES)
        sent: list[tuple[str, str]] = []
        director_held = {"busy": False}

        def sender(target: str, message: str) -> dict:
            sent.append((target, message))
            if target == TARGETS["director"]:
                # The Director's turn takes the notice, and ends.
                store.write(target, "busy", source="claude.UserPromptSubmit")
                store.write(target, "idle", source="claude.Stop")
            return {}

        def listener():
            held = nl.TicketBoardNotifyListener(
                conninfo=listener_url, project="pgu", sender=sender, activity_gate=gate.is_working,
                target_exists=lambda target: target in TARGETS.values(), poll_seconds=0,
                submission_confirm_seconds=0.2, submission_poll_seconds=0.05)
            held.unresolved_turn_grace_seconds = GRACE
            held.delivery_submitter = lambda _target: None
            return held

        def director(busy: bool) -> None:
            director_held["busy"] = busy
            store.write(TARGETS["director"], "busy" if busy else "idle",
                        source="claude.UserPromptSubmit" if busy else "claude.Stop")

        def run_pass(held=None):
            # A notice requeued behind a pane's own write waits a backoff; here the next pass is the next attempt.
            sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                "WHERE next_attempt_at > clock_timestamp();")
            held = held or listener()
            held.listen_once(max_notifications=held.delivered_count + 20)
            # The Director's pane settles after each notice it takes: what is queued for it goes on a later pass.
            for _ in range(8):
                if director_held["busy"] or sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                                                "WHERE target_role = 'director';") == "0":
                    break
                time.sleep(0.3)
                sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                    "WHERE next_attempt_at > clock_timestamp();")
                held.listen_once(max_notifications=held.delivered_count + 20)
            return held

        def fire(role: str, event: str, *, provider=None, nested: bool = False) -> int:
            """The provider runs the hook commands installed for `event`, as Claude Code and Codex run them."""
            cmds = commands(RUNTIMES[role], event)
            for cmd in cmds:
                argv = ["sh", "-c", cmd]
                if nested:  # a `claude -p` the pane's provider started from a tool shell runs its own hooks
                    argv = [sys.executable, "-c", "import subprocess, sys; sys.exit(subprocess.run(sys.argv[1:]).returncode)",
                            *argv]
                (provider or providers[role]).run(argv, json.dumps({"session_id": f"{role}-s1", "hook_event_name": event,
                                                                    "tool_name": "Bash"}),
                                                  {**hook_env, "TICKET_BOARD_PANE_TARGET": TARGETS[role]})
            return len(cmds)

        def tool_calls(role: str, count: int = 6, **kw) -> int:
            fired = 0
            for _ in range(count):
                fired += fire(role, "PreToolUse", **kw) + fire(role, "PostToolUse", **kw)
            return fired

        def state_file(role: str) -> Path:
            return state_dir / (TARGETS[role].replace(":", "_") + ".json")

        def activity_file(role: str) -> Path:
            return state_dir / (TARGETS[role].replace(":", "_") + ".activity.json")

        def activity(role: str) -> dict | None:
            try:
                return json.loads(activity_file(role).read_text())
            except (OSError, ValueError):
                return None

        def redate(path: Path, **fields) -> None:
            record = json.loads(path.read_text())
            record.update(fields)
            path.write_text(json.dumps(record))

        tickets = iter(range(1, 100))
        current: dict = {}

        def begin(role: str, title: str) -> tuple[str, float]:
            """A ticket handed to `role` BACK seconds ago, and the turn the hand-over started, through the real hook."""
            if role in current:
                fixture.post_json(api, f"/api/tickets/{current[role]}/actions/force_move",
                                  {"state": "done", "assignee": "unassigned"}, caller="director")
            ticket = f"PGU-{next(tickets)}"
            fixture.seed_postgres_ticket(admin, ticket, title=title, state="analysis", assignee="director",
                                         commit_exempt=True)
            fixture.post_json(api, f"/api/tickets/{ticket}/actions/route", {"state": "in_progress", "assignee": role},
                              caller="director")
            t0 = time.time() - BACK
            sql(f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';")
            sql(f"SELECT ticket_board.record_notification_trace('{ticket}', {9000 + int(ticket[4:])}, '{role}', "
                "'transition', 'send');")
            replica(f"UPDATE ticket_board.ticket_notification_state SET current_assignment_at = '{iso(t0)}', "
                    f"entered_current_state_at = '{iso(t0)}' WHERE ticket_id = '{ticket}'; "
                    f"UPDATE ticket_board.notification_trace SET ts = '{iso(t0)}' WHERE ticket_id = '{ticket}'; "
                    f"UPDATE ticket_board.ticket_comments SET ts = '{iso(t0)}' WHERE ticket_id = '{ticket}'; "
                    f"UPDATE ticket_board.board_progress_events SET at = '{iso(t0)}' WHERE ticket_id = '{ticket}';")
            current[role] = ticket
            activity_file(role).unlink(missing_ok=True)
            fire(role, "UserPromptSubmit")
            redate(state_file(role), turn_started_at=t0 + 1, updated_at=t0 + 1)
            return ticket, t0

        def story(ticket: str) -> dict:
            """What the board did about the ticket's stall: escalations queued, sent, dropped or withdrawn, and why."""
            rows = json.loads(sql(
                "SELECT coalesce(jsonb_agg(jsonb_build_object('event', tr.event, 'reason', coalesce(tr.busy_reason, ''), "
                "'premise', tr.detail ->> 'stall_premise', 'phase', tr.detail ->> 'phase', 'progress_at', "
                "e.detail -> 'payload' ->> 'progress_at') ORDER BY tr.id), '[]')::text "
                "FROM ticket_board.notification_trace tr JOIN ticket_board.notification_trace e "
                "ON e.notification_id = tr.notification_id AND e.event = 'enqueue' "
                f"WHERE tr.ticket_id = '{ticket}' AND e.detail -> 'payload' ->> 'kind' = 'stall_escalation';"))
            return {
                "queued": sum(1 for r in rows if r["event"] == "enqueue"),
                "sent": sum(1 for _t, m in sent if m.startswith(f"PRIORITY {ticket} ")),
                "dropped": [r["premise"] for r in rows if r["event"] == "drop"],
                "withdrawn": [r["reason"] for r in rows if r["event"] == "discard"],
                "progress_at": [r["progress_at"] for r in rows if r["event"] == "enqueue"],
                "episodes": sql("SELECT coalesce(string_agg(phase || '/' || coalesce(close_reason, 'open'), ',' "
                                f"ORDER BY id), '') FROM ticket_board.role_stall_episodes WHERE ticket_id = '{ticket}';"),
                "still_queued": sql("SELECT count(*) FROM ticket_board.ticket_notification_queue "
                                    f"WHERE ticket_id = '{ticket}' AND payload ->> 'kind' = 'stall_escalation';"),
            }

        try:
            # -- the control: busy, no tool call --------------------------------------------------------------------
            ticket, _t0 = begin("ops", "Busy, nothing moving")
            run_pass(), run_pass()
            out["control"] = story(ticket)
            out["control_message"] = next((m for _t, m in sent if m.startswith(f"PRIORITY {ticket} ")), "")

            # -- SYRD-691: a run of short tool calls, none long enough for a lease ----------------------------------
            ticket, _t0 = begin("ops", "Short tool calls")
            before = state_file("ops").read_text()
            out["tool_calls_fired"] = tool_calls("ops")
            out["state_untouched"] = state_file("ops").read_text() == before
            recorded = activity("ops")
            out["activity"] = None if recorded is None else {
                "source": recorded.get("source"), "by_provider": recorded.get("pane_pid") == providers["ops"].pid,
                "start_time": recorded.get("pane_start_time") == providers["ops"].ticks}
            run_pass(), run_pass()
            out["tool-calls"] = story(ticket)

            def codex_pane(ticket: str) -> dict:
                hook = json.loads(state_file("main").read_text())
                return {**story(ticket), "hook": f"{hook['state']}/{hook['source']}",
                        "typed_into": sum(1 for t, _m in sent if t == TARGETS["main"])}

            ticket, _t0 = begin("main", "Codex short tool calls")
            out["codex_fired"] = tool_calls("main")
            out["codex_activity_source"] = (activity("main") or {}).get("source")
            run_pass(), run_pass()
            out["codex"] = codex_pane(ticket)
            sent[:] = [(t, m) for t, m in sent if t != TARGETS["main"]]
            ticket, _t0 = begin("main", "Codex, silent")
            run_pass(), run_pass()
            out["codex-silence"] = codex_pane(ticket)
            # The listener's idle keeps the turn's process; a call after it is not the hook's word for an idle pane.
            fire("main", "PostToolUse")
            from scripts.ticket_board import stall_episode
            out["recovered_word_own"] = stall_episode.owner_hook(gate, "main") == {
                "state": "idle", "updated_at": iso(json.loads(state_file("main").read_text())["updated_at"]),
                "turn_started_at": iso(json.loads(state_file("main").read_text())["turn_started_at"])}

            # -- tool calls, then real silence past the threshold ------------------------------------------------------
            ticket, _t0 = begin("app", "Silence after the calls")
            tool_calls("app", 2)
            last = time.time() - 1900
            if activity("app") is not None:
                redate(activity_file("app"), at=last)
            run_pass(), run_pass(), run_pass()
            out["silence"] = story(ticket)
            out["silence_last_call"] = iso(last)

            ticket, _t0 = begin("ops", "Quiet a while after the calls")
            tool_calls("ops", 2)
            if activity("ops") is not None:
                redate(activity_file("ops"), at=time.time() - 900)
            run_pass(), run_pass()
            out["quiet"] = story(ticket)

            # -- escalated while the Director is busy, then a tool call: delivery drops it --------------------------
            ticket, _t0 = begin("ops", "Queued, then a call before delivery")
            director(True)
            held = run_pass()
            out["queued_before_call"] = story(ticket)
            tool_calls("ops", 1)
            director(False)
            sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                f"WHERE ticket_id = '{ticket}';")
            with psycopg.connect(listener_url, autocommit=True) as conn:
                held.refresh_workflow(conn)
                held.process_due_notifications(conn)
            out["queued-then-call"] = story(ticket)
            run_pass()
            out["queued_then_call_after"] = story(ticket)

            # -- the same, caught by the next pass ---------------------------------------------------------------------
            ticket, _t0 = begin("app", "Queued, then a call before the next pass")
            director(True)
            run_pass()
            tool_calls("app", 1)
            run_pass()
            director(False)
            run_pass()
            out["queued-then-call-pass"] = story(ticket)

            # -- calls that say nothing about this turn still alert ------------------------------------------------------
            ticket, _t0 = begin("ops", "A claude -p run from the tool shell")
            tool_calls("ops", 2, nested=True)
            out["nested_activity"] = activity("ops")
            run_pass(), run_pass()
            out["nested"] = story(ticket)

            ticket, _t0 = begin("app", "Calls left by a replaced provider")
            tool_calls("app", 2)  # the old provider's calls, just now
            replacement = Provider(state_dir.parent)
            spawned.append(replacement)
            register("app", replacement)
            providers["app"] = replacement
            replica(f"UPDATE ticket_board.role_runtime_assignments SET registered_at = '{iso(time.time() - BACK)}' "
                    "WHERE role = 'app';")
            fire("app", "UserPromptSubmit")  # the replacement takes the turn
            redate(state_file("app"), turn_started_at=time.time() - BACK + 1, updated_at=time.time() - BACK + 1)
            run_pass(), run_pass()
            out["replaced"] = story(ticket)

            for name, change in (("earlier-turn", lambda t0: {"at": t0 - 100}),
                                 ("reused-pid", lambda t0: {"pane_start_time": providers["ops"].ticks + 1}),
                                 ("future", lambda t0: {"at": time.time() + 3600})):
                ticket, t0 = begin("ops", f"Calls: {name}")
                tool_calls("ops", 1)
                if activity("ops") is not None:
                    redate(activity_file("ops"), **change(t0))
                run_pass(), run_pass()
                out[name] = story(ticket)
                # Nor is it shown as this turn's: delivery's evidence of the owner's hook names no call.
                from scripts.ticket_board import stall_episode
                out[f"{name}_claimed"] = "tool_activity_at" in (stall_episode.owner_hook(gate, "ops") or {})

            # -- a tool hook after the turn's Stop leaves the pane idle ------------------------------------------------
            fire("ops", "Stop")
            fire("ops", "PostToolUse")
            stopped = json.loads(state_file("ops").read_text())
            out["after_stop"] = stopped.get("state")
            from scripts.ticket_board import stall_episode
            out["after_stop_word_own"] = (stall_episode.owner_hook(gate, "ops") or {}).get("updated_at") \
                == iso(stopped["updated_at"])
        finally:
            for provider in spawned:
                provider.stop()
            server.shutdown()
            server.server_close()
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL",
                                                                     "TMUX"))}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree)],
                           capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=500,
                           stdin=subprocess.DEVNULL, cwd=str(tree))
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def one_alert(seen: dict) -> bool:
    return seen["queued"] == 1 and seen["sent"] == 1 and seen["dropped"] == [] and seen["withdrawn"] == []


def before(got: dict) -> None:
    check(got["installed"] == {"claude": {"PreToolUse": [], "PostToolUse": []},
                               "codex": {"PreToolUse": [], "PostToolUse": []}},
          f"before: no hook runs on a tool call: {got['installed']}")
    for name in ("control", "tool-calls", "silence", "quiet", "nested", "replaced", "earlier-turn", "reused-pid", "future"):
        check(one_alert(got[name]), f"before ({name}): one escalation, delivered: {got[name]}")
    check("has said busy since, with nothing moving on screen" in got["control_message"],
          f"before: the SYRD-691 message: {got['control_message']}")
    for name in ("codex", "codex-silence"):
        check(got[name]["hook"] == "idle/listener.stale_codex_busy_recovery" and got[name]["typed_into"] > 0,
              f"before ({name}): a Codex turn is called over after 120 s of a still screen, and typed into: {got[name]}")
    check(got["queued-then-call"]["sent"] == 1 and got["queued-then-call"]["dropped"] == [],
          f"before: a queued escalation is delivered whatever the pane did since: {got['queued-then-call']}")


def candidate(got: dict) -> None:
    hook = "{} --source {}.{}"
    check(got["installed"] == {cli: {event: [hook.format("activity", cli, event)] for event in ("PreToolUse", "PostToolUse")}
                               for cli in ("claude", "codex")},
          f"Claude and Codex run the hook on every tool call, before and after it: {got['installed']}")
    check(one_alert(got["control"]) and "has said busy since, with nothing moving on screen" in got["control_message"],
          f"control: a busy turn with no tool call still escalates, once: {got['control']}")
    check(got["tool_calls_fired"] == 12 and got["state_untouched"] is True
          and got["activity"] == {"source": "claude.PostToolUse", "by_provider": True, "start_time": True},
          f"each call is recorded beside the pane's state, by the pane's provider, the state untouched: "
          f"{got['tool_calls_fired']} {got['state_untouched']} {got['activity']}")
    for name in ("tool-calls", "codex"):
        seen = got[name]
        check(seen["queued"] == 0 and seen["sent"] == 0 and seen["episodes"] == "",
              f"{name}: a turn of short tool calls is moving: no escalation, no episode: {seen}")
    check(got["codex"]["hook"] == "busy/codex.UserPromptSubmit" and got["codex"]["typed_into"] == 0,
          f"codex: a turn calling tools stays busy, and nothing is typed into it: {got['codex']}")
    check(got["codex-silence"]["hook"] == "idle/listener.stale_codex_busy_recovery" and got["codex-silence"]["typed_into"] > 0,
          f"codex-silence: a silent Codex turn is recovered to idle as before: {got['codex-silence']}")
    check(got["codex_fired"] == 12 and got["codex_activity_source"] == "codex.PostToolUse",
          f"codex: its calls recorded too: {got['codex_fired']} {got['codex_activity_source']}")
    seen = got["silence"]
    check(one_alert(seen) and seen["progress_at"] and
          abs(datetime.fromisoformat(seen["progress_at"][0]).timestamp()
              - datetime.fromisoformat(got["silence_last_call"]).timestamp()) < 1,
          f"silence: one escalation, bound to the last tool call: {seen} {got['silence_last_call']}")
    check(got["quiet"]["queued"] == 0 and got["quiet"]["sent"] == 0,
          f"quiet: past the grace since the last call, but not the threshold: no escalation yet: {got['quiet']}")
    seen = got["queued-then-call"]
    check(got["queued_before_call"]["queued"] == 1 and got["queued_before_call"]["sent"] == 0
          and seen["sent"] == 0 and seen["dropped"] == ["hook_activity"] and seen["still_queued"] == "0",
          f"queued-then-call: a tool call before delivery drops the escalation, saying why: "
          f"{got['queued_before_call']} {seen}")
    check(got["queued_then_call_after"]["episodes"] == "director/progress" and got["queued_then_call_after"]["queued"] == 1,
          f"queued-then-call: the next pass ends the episode, and alerts no more: {got['queued_then_call_after']}")
    seen = got["queued-then-call-pass"]
    check(seen["queued"] == 1 and seen["sent"] == 0 and seen["withdrawn"] == ["stall_premise_hook_activity"]
          and seen["episodes"] == "director/progress",
          f"queued-then-call-pass: the next pass withdraws it, saying why, and ends the episode: {seen}")
    check(got["nested_activity"] is None and one_alert(got["nested"]),
          f"nested: a hook a `claude -p` ran records nothing, and the alert stands: {got['nested_activity']} {got['nested']}")
    for name in ("replaced", "earlier-turn", "reused-pid", "future"):
        check(one_alert(got[name]), f"{name}: a call that is not this turn's holds nothing off: {got[name]}")
    for name in ("earlier-turn", "reused-pid", "future"):
        check(got[f"{name}_claimed"] is False, f"{name}: nor is it the owner's hook evidence: {got[f'{name}_claimed']}")
    check(got["recovered_word_own"] is True,
          f"a call after the listener called a Codex turn over is not the idle pane's word: {got['recovered_word_own']}")
    check(got["after_stop"] == "idle" and got["after_stop_word_own"] is True,
          f"a tool hook after the turn's Stop leaves the pane idle, and the hook's word its own: "
          f"{got['after_stop']} {got['after_stop_word_own']}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2])), default=str))
        return 0
    if os.environ.get("SYRD695_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd695-before.", dir="/tmp") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(run(ROOT))
    print(f"tool_activity_evidence_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
