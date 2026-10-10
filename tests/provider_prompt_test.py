#!/usr/bin/env python3
"""SYRD-566: a pane stopped at its provider's own question is not a worker ready for work.

Otto (release 2951c5c9) found three ways a fresh Codex worker can be up and
unable to take anything: a model-retirement notice that waits for an answer, a
pane that fires no hook until its first prompt -- so the board's only witness is
the launcher's own "idle" seed -- and `/new`, which asks whether to run in the
current checkout or a new worktree. Typing a ticket notice into any of those
answers the question: the notice ends in Enter.

Recorded screens (tests/fixtures/syrd566_provider_screens.json: Codex 0.162.0
and Claude Code 2.1.294, offline, throwaway homes) drive the classifier, the
real activity gate and a production-built board's real listener. Worker starts
go through the real `switchyard worker-pool` command. Where Codex is installed,
the real CLI is run again -- contained, with no network -- so the classifier is
checked against the current version, not only the recording. The before-runs
are main's code, from a git archive.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "774d365fd99998359a55c81136b6b12b9b17c2b3"  # main before SYRD-566
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
FIXTURE = ROOT / "tests" / "fixtures" / "syrd566_provider_screens.json"
PROMPTS = {
    "codex_model_retirement": "model_retirement",
    "codex_folder_trust": "folder_trust",
    "codex_sign_in": "sign_in",
    "codex_new_location_menu": "new_conversation_location",
    "claude_folder_trust": "folder_trust",
    "claude_folder_trust_yes_selected": "folder_trust",
}
READY = ("codex_ready", "codex_after_clear", "claude_ready")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:  # the label again, last, so a truncated tail still says which check failed
        raise AssertionError(f"{detail}\nFAILED CHECK: {re.split(r': [\[{(]', detail, maxsplit=1)[0]}")
    CHECKS += 1


def captures() -> dict:
    return json.loads(FIXTURE.read_text())["captures"]


def fake_tmux(screens: dict[str, dict], typed: list | None = None):
    """A tmux whose panes show recorded screens: capture-pane and the cursor, and nothing else."""

    def tmux(argv, **_kwargs):
        argv = [str(part) for part in argv]
        target = argv[argv.index("-t") + 1] if "-t" in argv else ""
        screen = screens.get(target)
        if screen is None:
            raise AssertionError(f"no fake pane for {target!r}: {argv}")
        if argv[:2] == ["tmux", "capture-pane"]:
            return subprocess.CompletedProcess(argv, 0, "\n".join(screen["rows"]) + "\n", "")
        if argv[:2] == ["tmux", "display-message"]:
            if "cursor_x" in argv[-1]:
                x, y = screen["cursor"]
                return subprocess.CompletedProcess(argv, 0, f"{x} {y} {len(screen['rows'])}\n", "")
            if argv[-1] == "#{pane_pid}":
                return subprocess.CompletedProcess(argv, 0, "100\n", "")
            return subprocess.CompletedProcess(argv, 0, "0\n", "")
        if typed is not None:
            typed.append(argv)
        raise AssertionError(f"unexpected tmux call {argv}")

    return tmux


# --------------------------------------------------------------------------
# The gate, before and after: a child process runs it in main's tree
# --------------------------------------------------------------------------


def gate_verdicts(root: Path) -> dict:
    """The activity gate's verdict on each recorded screen, for a pane only the launcher has said is idle."""
    sys.path[:0] = [str(root), str(root / "tests")]
    from scripts.ticket_board import notify_listener as nl

    seen = {}
    for name, screen in captures().items():
        for source in ("team_launcher.start", "codex.Stop"):
            with tempfile.TemporaryDirectory(prefix="syrd566-gate.") as tmp:
                store = nl.PaneHookStateStore(Path(tmp))
                store.write("pgu-app:0.0", "idle", source=source, now=time.time() - 60)
                tmux = fake_tmux({"pgu-app:0.0": screen})
                gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                           capture_pane_runner=tmux, pane_pid_runner=tmux, process_table_reader=lambda: (),
                                           sleeper=lambda _s: None, role_runtimes={"app": "codex"})
                busy = gate.is_working("pgu-app:0.0")
                seen[f"{name}/{source}"] = [busy, gate.last_trace("pgu-app:0.0").reason]
    # The gate's other two entry points, at the /new menu: the anti-clobber check
    # under a busy hook, and a Codex busy hook old enough to be recovered as idle.
    for label, state, source, age, ask in (("anti_clobber_busy_hook", "busy", "codex.UserPromptSubmit", 5, "anti_clobber_busy"),
                                           ("stale_codex_busy", "busy", "codex.UserPromptSubmit", 600, "is_working")):
        with tempfile.TemporaryDirectory(prefix="syrd566-gate.") as tmp:
            store = nl.PaneHookStateStore(Path(tmp))
            store.write("pgu-app:0.0", state, source=source, now=time.time() - age)
            tmux = fake_tmux({"pgu-app:0.0": captures()["codex_new_location_menu"]})
            gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                       capture_pane_runner=tmux, pane_pid_runner=tmux, process_table_reader=lambda: (),
                                       sleeper=lambda _s: None, role_runtimes={"app": "codex"})
            seen[label] = [getattr(gate, ask)("pgu-app:0.0"), gate.last_trace("pgu-app:0.0").reason]
    # A pane at a question that then cannot be read: what it showed before is not what it shows now.
    with tempfile.TemporaryDirectory(prefix="syrd566-gate.") as tmp:
        store = nl.PaneHookStateStore(Path(tmp))
        store.write("pgu-app:0.0", "idle", source="codex.Stop", now=time.time() - 60)
        shown = {"pgu-app:0.0": captures()["codex_model_retirement"]}
        readable = fake_tmux(shown)

        def tmux(argv, **kwargs):
            if not shown:
                return subprocess.CompletedProcess(argv, 1, "", "no pane")
            return readable(argv, **kwargs)

        gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                   capture_pane_runner=tmux, pane_pid_runner=tmux, process_table_reader=lambda: (),
                                   sleeper=lambda _s: None, role_runtimes={"app": "codex"})
        first = [gate.is_working("pgu-app:0.0"), gate.last_trace("pgu-app:0.0").reason]
        shown.clear()
        seen["unreadable_after_question"] = first + [gate.is_working("pgu-app:0.0"), gate.last_trace("pgu-app:0.0").reason]
    # A pane the launcher seeded two seconds ago: its CLI is still drawing its first screens.
    with tempfile.TemporaryDirectory(prefix="syrd566-gate.") as tmp:
        store = nl.PaneHookStateStore(Path(tmp))
        store.write("pgu-app:0.0", "idle", source="team_launcher.start", now=time.time() - 2)
        tmux = fake_tmux({"pgu-app:0.0": captures()["codex_ready"]})
        gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                   capture_pane_runner=tmux, pane_pid_runner=tmux, process_table_reader=lambda: (),
                                   sleeper=lambda _s: None, role_runtimes={"app": "codex"})
        seen["just_seeded"] = [gate.is_working("pgu-app:0.0"), gate.last_trace("pgu-app:0.0").reason]
    return seen


# --------------------------------------------------------------------------
# A production board and its real listener
# --------------------------------------------------------------------------


def listener_scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests"), str(ROOT / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board, workflow
    from scripts.ticket_board import notify_listener as nl
    from temporary_cluster import temporary_cluster

    screens = captures()
    seen: dict = {}
    with temporary_cluster(prefix="s566", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="syrd566-state.") as state_dir:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        app.apply_workflow(workflow(root), expected_revision=0, dry_run=False, caller_role="director")

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        roles = ("main", "app", "ops", "director")
        targets = {role: f"pgu-{role}:0.0" for role in roles}
        panes = {target: screens["codex_ready"] for target in targets.values()}
        panes[targets["director"]] = screens["claude_ready"]
        store = nl.PaneHookStateStore(Path(state_dir))
        tmux = fake_tmux(panes)
        gate = nl.PaneActivityGate(state_store=store, client_activity_runner=tmux, cursor_position_runner=tmux,
                                   capture_pane_runner=tmux, pane_pid_runner=tmux, process_table_reader=lambda: (),
                                   sleeper=lambda _s: None,
                                   role_runtimes={**{role: "codex" for role in roles}, "director": "claude"})
        # Fresh Codex panes: only the launcher has said anything. The Director
        # has ended a turn since this listener started.
        for role in ("main", "app", "ops"):
            store.write(targets[role], "idle", source="team_launcher.start", now=time.time() - 60)
        store.write(targets["director"], "idle", source="claude.Stop")
        sends: list[tuple[str, str, str]] = []

        def sender(target: str, message: str) -> dict:
            sends.append((target, message, _shown(panes[target])))
            return {}

        def one_pass() -> None:
            sql("UPDATE ticket_board.ticket_notification_queue SET next_attempt_at = clock_timestamp() "
                "WHERE dead_lettered_at IS NULL;")
            listener = nl.TicketBoardNotifyListener(conninfo=listener_url, project="pgu", sender=sender,
                                                    activity_gate=gate.is_working, target_exists=lambda _t: True,
                                                    submission_witness=lambda *_a: True, poll_seconds=0)
            listener._wait_for_notification = lambda _conn: False  # one iteration, without waiting for the next due row
            listener.listen_once(max_notifications=10)

        def route(ticket: str, role: str) -> None:
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
            fixture.post_json(base, f"/api/tickets/{ticket}/actions/route", {"state": "in_progress", "assignee": role},
                              caller="director")

        def held(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(json_build_array(kind, target_role, coalesce(last_error, '')) "
                                  "ORDER BY id), '[]')::text FROM ticket_board.ticket_notification_queue "
                                  f"WHERE ticket_id = '{ticket}' AND target_role <> 'director';"))

        def deferred(ticket: str) -> list:
            return json.loads(sql("SELECT coalesce(json_agg(DISTINCT busy_reason), '[]')::text FROM ticket_board.notification_trace "
                                  f"WHERE ticket_id = '{ticket}' AND event = 'gate_defer' AND target_role <> 'director';"))

        def episode(role: str):
            try:
                return json.loads(sql("SELECT coalesce(json_agg(json_build_array(reason, alerted_at IS NOT NULL, "
                                      "coalesce(clear_reason, '')) ORDER BY id), '[]')::text "
                                      f"FROM ticket_board.background_hold_episodes WHERE target_role = '{role}';"))
            except AssertionError:
                return None  # main before SYRD-557 has no such table

        def backdate(role: str, minutes: int) -> None:
            try:
                sql(f"UPDATE ticket_board.background_hold_episodes SET held_since = held_since - interval '{minutes} minutes' "
                    f"WHERE target_role = '{role}' AND cleared_at IS NULL;")
            except AssertionError:
                pass

        def director_heard(ticket: str) -> list:
            waiting = json.loads(sql("SELECT coalesce(json_agg('waiting: ' || message ORDER BY id), '[]')::text "
                                     "FROM ticket_board.ticket_notification_queue "
                                     f"WHERE ticket_id = '{ticket}' AND target_role = 'director';"))
            return [m for t, m, _s in sends if t == targets["director"] and ticket in m] + waiting

        def typed_into(target: str, ticket: str) -> list:
            """What each send of this ticket's own notices found on the pane it was typed into."""
            return [shown for t, m, shown in sends if t == target and m.startswith(f"{ticket} -- ")]

        try:
            # 1. A fresh Codex worker stopped at the model-retirement notice.
            panes[targets["app"]] = screens["codex_model_retirement"]
            route("PGU-1", "app")
            one_pass()
            one_pass()
            seen["retirement_held"] = [held("PGU-1"), deferred("PGU-1"), typed_into(targets["app"], "PGU-1"),
                                       episode("app"), director_heard("PGU-1")]
            backdate("app", 1)
            one_pass()
            seen["retirement_under_threshold"] = [episode("app"), director_heard("PGU-1")]
            backdate("app", 2)
            for _ in range(3):
                one_pass()
            seen["retirement_alerted"] = [held("PGU-1"), episode("app"), director_heard("PGU-1"),
                                          typed_into(targets["app"], "PGU-1")]
            # Somebody answers it; the pane is at Codex's ordinary prompt.
            panes[targets["app"]] = screens["codex_ready"]
            one_pass()
            seen["retirement_answered"] = [held("PGU-1"), episode("app"), director_heard("PGU-1"),
                                           typed_into(targets["app"], "PGU-1")]

            # 2. Somebody typed /new in ops's pane, which had ended a turn.
            store.write(targets["ops"], "idle", source="codex.Stop")
            panes[targets["ops"]] = screens["codex_new_location_menu"]
            route("PGU-2", "ops")
            one_pass()
            backdate("ops", 3)
            one_pass()
            seen["new_menu"] = [held("PGU-2"), deferred("PGU-2"), episode("ops"), director_heard("PGU-2"),
                                typed_into(targets["ops"], "PGU-2")]
        finally:
            server.shutdown()
            server.server_close()
    return seen


def _shown(screen: dict) -> str:
    """The question a recorded screen shows, by its first option, or "ready"."""
    for row in screen["rows"]:
        if re.match(r"^\s*[›❯>]\s*1\.\s", row) or "No, exit" in row:
            return row.strip()
    return "ready"


# --------------------------------------------------------------------------
# Worker starts, through the real `switchyard worker-pool` command
# --------------------------------------------------------------------------


def worker_starts() -> dict:
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    import worker_pool_start_status_test as start_suite
    from scripts import worker_screen

    screens = captures()

    class Host(start_suite.Host):
        """The start suite's sandbox tenant, whose worker pane shows a recorded screen."""

        screen: dict | None = None
        #: Captures that show the ready prompt before `screen` is drawn over it, as Codex does 4 s in.
        ready_first = 0

        def runner(self, args, **kwargs):
            command = [str(part) for part in args]
            if "capture-pane" in command:
                if self.screen is None:
                    return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
                shown = self.screen
                if self.ready_first > 0:
                    self.ready_first -= 1
                    shown = screens["codex_ready"]
                return subprocess.CompletedProcess(command, 0, stdout="\n".join(shown["rows"]) + "\n")
            return super().runner(args, **kwargs)

    seen: dict = {}
    saved = (worker_screen.SETTLE_SECONDS, worker_screen.POLL_SECONDS)
    worker_screen.SETTLE_SECONDS, worker_screen.POLL_SECONDS = 0.2, 0.05
    try:
        for label, screen, ready_first in (("retirement", screens["codex_model_retirement"], 0),
                                           ("new_menu", screens["codex_new_location_menu"], 0),
                                           ("late_retirement", screens["codex_model_retirement"], 3),
                                           ("ready", screens["codex_ready"], 0), ("unreadable", None, 0)):
            with tempfile.TemporaryDirectory(prefix="syrd566-start.") as tmp:
                host = Host(Path(tmp))
                host.screen, host.ready_first = screen, ready_first
                host.prepare("impl-1")
                code, said = host.run("start", "impl-1")
                status_code, status = host.run("status")
                again_code, again = host.run("start", "impl-1")
                seen[label] = {"start": [code, said], "status": [status_code, status], "again": [again_code, again],
                               "starts": len(host.started)}
    finally:
        worker_screen.SETTLE_SECONDS, worker_screen.POLL_SECONDS = saved
    return seen


def slow_draw() -> list:
    """The start watch over a CLI that stays blank longer than the settle time, then asks (clock injected)."""
    sys.path[:0] = [str(ROOT)]
    from scripts import worker_screen

    now = [0.0]
    blank = "\n" * 45  # a real pane captures its rows, blank or not
    frames = [blank] * int(worker_screen.SETTLE_SECONDS / worker_screen.POLL_SECONDS + 4)
    frames.append("\n".join(captures()["codex_model_retirement"]["rows"]) + "\n")

    def runner(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, frames.pop(0) if len(frames) > 1 else frames[0], "")

    def sleep(seconds: float) -> None:
        now[0] += seconds

    prompt = worker_screen.startup_prompt("pgu-app:0.0", runner, clock=lambda: now[0], sleep=sleep)
    return [prompt.kind if prompt else None, now[0] > worker_screen.SETTLE_SECONDS]


def worker_problem_reads() -> dict:
    """runtime_handover.worker_problem, once liveness and identity are fine: what the pane shows decides."""
    sys.path[:0] = [str(ROOT), str(ROOT / "tests")]
    from types import SimpleNamespace

    from scripts import runtime_handover

    screens = captures()
    saved = (runtime_handover._liveness_problem, runtime_handover._identity_problem)
    runtime_handover._liveness_problem = lambda role, runner: ""
    runtime_handover._identity_problem = lambda config, role, runner, handover: ""
    role = SimpleNamespace(role="app", target="pgu-app:0.0", cli=["codex"], live_commands=[])
    try:
        return {name: runtime_handover.worker_problem(None, role, runner=fake_tmux({"pgu-app:0.0": screens[name]}),
                                                      handover=runtime_handover.RuntimeHandover())
                for name in ("codex_model_retirement", "codex_ready")}
    finally:
        runtime_handover._liveness_problem, runtime_handover._identity_problem = saved


def fresh_context() -> dict:
    sys.path[:0] = [str(ROOT)]
    from scripts.ticket_board.notification_session_clear import SESSION_CLEAR_COMMANDS

    return dict(SESSION_CLEAR_COMMANDS)


def hold_wording() -> dict:
    sys.path[:0] = [str(ROOT)]
    from scripts.ticket_board import background_hold, provider_prompt

    class Conn:
        def __init__(self) -> None:
            self.executed: list = []

        def execute(self, statement, params=None):
            self.executed.append((statement, params))
            return self

    class Ledger:
        logger = None

    older = Ledger()
    older._background_hold_present = "background"  # a board with SYRD-557's function only
    conn = Conn()
    background_hold.note(older, conn, 9, "provider_prompt:model_retirement")
    background_hold.note(older, conn, 9, "pane_child_work")
    return {
        "described": {kind: background_hold.prompt_hold_description(kind) for kind in provider_prompt.KINDS},
        "thresholds": {raw: background_hold.prompt_alert_after_seconds({background_hold.PROMPT_ALERT_AFTER_ENV: raw})
                       for raw in ("", "30", "-1", "soon")},
        "older_board": [statement.split("(")[0] + "(" + str(len(params)) for statement, params in conn.executed],
    }


# --------------------------------------------------------------------------
# The installed Codex, contained and offline
# --------------------------------------------------------------------------


def observe(spec: dict) -> dict:
    """One real Codex session in a private tmux server with no network. Runs as a contained child."""
    sys.path[:0] = [str(ROOT)]
    from scripts import worker_screen
    from scripts.ticket_board import provider_prompt

    env = spec["env"]
    home, work = Path(env["HOME"]), Path(spec["cwd"])
    (home / ".codex").mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    (home / ".codex" / "config.toml").write_text(
        f'model = "{spec["model"]}"\n[projects."{work}"]\ntrust_level = "trusted"\n')
    # A placeholder key gets past sign-in; there is no network to send it to.
    subprocess.run(["codex", "login", "--with-api-key"], input="sk-placeholder-never-sent\n", env=env,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True, timeout=60)
    name = "s566"
    subprocess.run(["tmux", "new-session", "-d", "-s", name, "-x", "160", "-y", "45", "-c", str(work),
                    "unshare --user --map-root-user --net codex"], env=env, check=True)
    socket = subprocess.run(["tmux", "display-message", "-p", "#{socket_path}"], env=env,
                            capture_output=True, text=True).stdout.strip()
    assert socket.startswith(env["TMUX_TMPDIR"]), f"reached a live tmux server: {socket}"

    def runner(argv, **kwargs):
        return subprocess.run(argv, env=env, **kwargs)

    def screen() -> list[str]:
        return worker_screen.capture(name, runner) or []

    try:
        # The real default watch, against the real pane: what start_worker sees.
        first = worker_screen.startup_prompt(name, runner)
        result = {"observed": any("OpenAI Codex (v" in row or "Try new model" in row for row in screen()),
                  "startup": first.kind if first else None,
                  "sign_in": any("Sign in with ChatGPT" in row for row in screen())}
        for step in spec.get("steps", ()):
            if step.startswith("type:"):
                subprocess.run(["tmux", "send-keys", "-t", name, "-l", step[5:]], env=env)
            else:
                subprocess.run(["tmux", "send-keys", "-t", name, step], env=env)
            time.sleep(1)
        # /clear clears the terminal first and draws the new chat once it is up.
        for _ in range(24):
            rows = screen()
            if any(row.strip() for row in rows):
                break
            time.sleep(0.5)
        time.sleep(1)
        rows = screen()
        prompt = provider_prompt.open_prompt(rows)
        result["after"] = prompt.kind if prompt else None
        result["path"] = subprocess.run(["tmux", "display-message", "-p", "-t", name, "#{pane_current_path}"], env=env,
                                        capture_output=True, text=True).stdout.strip()
        result["at_prompt"] = any(row.lstrip().startswith("› ") for row in rows)

        return result
    finally:
        subprocess.run(["tmux", "kill-server"], env=env, capture_output=True)


def real_codex() -> dict:
    """Each real observation, run contained (SYRD-524); {} when there is no Codex or no way to cut the network."""
    sys.path[:0] = [str(ROOT / "tests")]
    import contained_cli

    real = {name: shutil.which(name, path="/usr/local/bin:/usr/bin:/bin") for name in ("codex", "tmux", "unshare")}
    if not all(real.values()) or subprocess.run(["unshare", "--user", "--map-root-user", "--net", "true"],
                                                capture_output=True).returncode != 0:
        return {}
    seen = {}
    with contained_cli.ContainedHome("syrd566.") as home:
        for label, model, steps in (("retirement", "gpt-5.5", ()),
                                    ("new", "gpt-6-sol", ("type:/new", "Enter")),
                                    ("clear", "gpt-6-sol", ("type:/clear", "Enter"))):
            base = home.root / label
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": str(base / "home"), "CODEX_HOME": str(base / "home" / ".codex"),
                   "TERM": "xterm-256color", "TMUX_TMPDIR": str(base / "tmux"), "LANG": "C.UTF-8"}
            (base / "tmux").mkdir(parents=True)
            spec = {"env": env, "cwd": str(base / "repo"), "model": model, "steps": list(steps)}
            outcome = home.run([sys.executable, str(Path(__file__).resolve()), "--observe", json.dumps(spec)],
                               env=env, timeout=150, label=label)
            check(outcome.status == "completed" and outcome.returncode == 0 and outcome.clean,
                  f"{label}: the real Codex observation did not finish cleanly: {outcome.reason or outcome.stderr[-800:]}")
            answers = [line for line in outcome.stdout.splitlines() if line.startswith("{")]
            check(answers, f"{label}: the observation reported nothing: {outcome.stdout[-400:]}")
            seen[label] = {**json.loads(answers[-1]), "cwd": spec["cwd"]}
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run_child(*argv: str) -> dict:
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), *argv], capture_output=True, text=True,
                           env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=900)
    assert child.returncode == 0, child.stderr[-3000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def main() -> int:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    if len(sys.argv) == 3 and sys.argv[1] in ("--gate", "--listener", "--observe"):
        work = {"--gate": lambda arg: gate_verdicts(Path(arg)), "--listener": lambda arg: listener_scenario(Path(arg)),
                "--observe": lambda arg: observe(json.loads(arg))}[sys.argv[1]]
        print(json.dumps(work(sys.argv[2]), default=str))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd566.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        before_gate = run_child("--gate", str(before_root))
        before = run_child("--listener", str(before_root))
    after_gate = run_child("--gate", str(ROOT))
    after = run_child("--listener", str(ROOT))

    # The classifier, on every recorded screen.
    sys.path[:0] = [str(ROOT)]
    from scripts.ticket_board.provider_prompt import open_prompt

    shots = captures()
    kinds = {name: (open_prompt(shot["rows"]).kind if open_prompt(shot["rows"]) else None) for name, shot in shots.items()}
    check(kinds == {**PROMPTS, **{name: None for name in READY}},
          f"each recorded question is named, and each ordinary prompt is not a question: {kinds}")
    options = {name: open_prompt(shots[name]["rows"]).options for name in PROMPTS}
    check(options["codex_model_retirement"] == ("Try new model", "Use existing model")
          and options["claude_folder_trust"] == ("No, exit", "Yes, I trust this folder")
          and options["codex_sign_in"] == ("Sign in with ChatGPT", "Sign in with Device Code", "Provide your own API key")
          and [o.split("  ")[0] for o in options["codex_new_location_menu"]] == ["Use current Git worktree", "Create new Git worktree"],
          f"and its options are read as the screen shows them -- unmarked, spaced out or described: {options}")
    syrd550 = json.loads((ROOT / "tests/fixtures/syrd550_claude_captures.json").read_text())["captures"]
    others = {
        **{f"syrd550 {name}": shot["lines"] for name, shot in syrd550.items()},
        "a numbered list in a transcript": ["• Plan:", "  1. read the ticket", "  2. write the test", "", "› ",
                                            "  GPT-6-Sol default · /work/repo", "  ← for agents · ? for shortcuts"],
        "a working turn": ["❯ do it", "", "✻ Thinking… (esc to interrupt)"],
        "a footer with no options": ["  Update available", "", "  Press enter to continue"],
        "options with no cursor": ["  1. one", "  2. two", "  enter select · esc back"],
        # One highlighted line over a key hint is a composer with a send hint, not a choice.
        "a composer with a send hint": ["› Ask Codex to do anything", "", "  enter send · esc clear"],
        # A line that says Enter but gives no key hint is prose, not a footer.
        "Enter in prose under a list": ["› 1. run the suite", "  2. read the diff", "", "  then press Enter when it is done"],
        # A question answered earlier, still on screen above what came after it.
        "an answered question above later output": [
            *captures()["codex_model_retirement"]["rows"][:10],
            "• Switched to gpt-6-sol", "", "› ", "  GPT-6-Sol default · /work/repo", "  ? for shortcuts"],
    }
    wrongly = {label: open_prompt(rows) for label, rows in others.items() if open_prompt(rows) is not None}
    check(wrongly == {}, f"nothing else reads as a question: {wrongly}")

    # The gate. main: the retirement notice and Codex's trust question read idle, so a notice is
    # typed into them; the others hold only because the cursor looks like somebody typing --
    # which is work evidence, so a reminder there is dropped as stale and nobody is told why.
    for name in ("codex_model_retirement", "codex_folder_trust"):
        check([before_gate[f"{name}/{source}"] for source in ("team_launcher.start", "codex.Stop")]
              == [[False, "working_timer_idle"], [False, "hook_idle"]],
              f"before: {name} reads idle, fresh or after a turn: {before_gate}")
    for name in ("codex_sign_in", "codex_new_location_menu", "claude_folder_trust", "claude_folder_trust_yes_selected"):
        check(before_gate[f"{name}/team_launcher.start"] == [True, "human_composing"],
              f"before: {name} reads as somebody typing: {before_gate[f'{name}/team_launcher.start']}")
    for name, kind in PROMPTS.items():
        for source in ("team_launcher.start", "codex.Stop"):
            check(after_gate[f"{name}/{source}"] == [True, f"provider_prompt:{kind}"],
                  f"{name} holds delivery under {source}, named: {after_gate[f'{name}/{source}']}")
    check(before_gate["anti_clobber_busy_hook"] == [True, "human_composing"]
          and after_gate["anti_clobber_busy_hook"] == [True, "provider_prompt:new_conversation_location"],
          f"the anti-clobber check under a busy hook names the question: {after_gate['anti_clobber_busy_hook']}")
    check(before_gate["stale_codex_busy"] == [True, "human_composing"]
          and after_gate["stale_codex_busy"] == [True, "provider_prompt:new_conversation_location"],
          f"so does a stale Codex busy hook, which is never recovered to idle at a question: {after_gate['stale_codex_busy']}")
    check(after_gate["unreadable_after_question"] == [True, "provider_prompt:model_retirement", True, "cursor_state_unavailable"],
          f"a pane that cannot be read is unreadable, not still at the question it showed before: "
          f"{after_gate['unreadable_after_question']}")
    check(before_gate["just_seeded"] == [False, "working_timer_idle"] and after_gate["just_seeded"] == [True, "provider_starting"],
          f"a pane seeded idle two seconds ago is still starting -- Codex draws its retirement notice 4 s in -- where main "
          f"called it idle: before {before_gate['just_seeded']}, after {after_gate['just_seeded']}")
    for name in READY:
        check(after_gate[f"{name}/team_launcher.start"] == before_gate[f"{name}/team_launcher.start"]
              and after_gate[f"{name}/team_launcher.start"][0] is False,
              f"an ordinary prompt is as deliverable as before: {name} {after_gate[f'{name}/team_launcher.start']}")

    # The listener. main: the assignment is typed into the retirement notice.
    check(before["retirement_held"][2] == ["› 1. Try new model"] and before["retirement_held"][0] == [],
          f"before: PGU-1 was typed into the model-retirement notice and acknowledged: {before['retirement_held']}")
    check(before["new_menu"][1] == ["human_composing"] and before["new_menu"][3] == [] and before["new_menu"][4] == [],
          f"before: PGU-2 waited behind the /new menu as if somebody were typing, and nobody was told: {before['new_menu']}")
    held = after["retirement_held"]
    check(held[0] == [["transition", "app", "pane busy"]] and held[1] == ["provider_prompt:model_retirement"]
          and held[2] == [] and held[3] == [["provider_prompt:model_retirement", False, ""]] and held[4] == [],
          f"a fresh worker at the retirement notice: the notice is held, never typed, an episode opened: {held}")
    check(after["retirement_under_threshold"] == [[["provider_prompt:model_retirement", False, ""]], []],
          f"one minute in, under the two-minute threshold: nobody told yet: {after['retirement_under_threshold']}")
    alerted = after["retirement_alerted"]
    check(alerted[0] == held[0] and alerted[1] == [["provider_prompt:model_retirement", True, ""]] and alerted[3] == []
          and len(alerted[2]) == 1 and alerted[2][0].startswith("app's pane has held 1 ticket notice (PGU-1) for 3 minutes: ")
          and "stopped at a model-retirement notice" in alerted[2][0] and "nothing was typed into it" in alerted[2][0]
          and "answer it in the pane" in alerted[2][0] and "background work" not in alerted[2][0],
          f"past it, the Director is told once what the pane is stopped at and what answers it: {alerted}")
    answered = after["retirement_answered"]
    check(answered[0] == [] and answered[1] == [["provider_prompt:model_retirement", True, "delivered"]]
          and answered[3] == ["ready"] and len(answered[2]) == 2 and "has ended" in answered[2][1],
          f"once it is answered, the notice goes to the ready prompt, once, and the hold is cleared: {answered}")
    menu = after["new_menu"]
    check(menu[0] == [["transition", "ops", "pane busy"]] and menu[1] == ["provider_prompt:new_conversation_location"]
          and menu[2] == [["provider_prompt:new_conversation_location", True, ""]] and menu[4] == []
          and len(menu[3]) == 1 and "the `/new` menu" in menu[3][0] and "Use current Git worktree" in menu[3][0]
          and "`/clear`" in menu[3][0],
          f"/new's menu after an ended turn: held, never typed, and the Director told to keep the checkout: {menu}")

    # Worker starts: blocked at a question is not started.
    starts = worker_starts()
    for label, question in (("retirement", "a model-retirement notice (its configured model is being replaced"),
                            ("new_menu", "the `/new` menu")):
        case = starts[label]
        check(case["start"][0] == 1 and "impl-1 failed to start: " in case["start"][1] and "is live and hermes is stopped at" in case["start"][1]
              and question in case["start"][1] and "To unblock it," in case["start"][1] and case["starts"] == 1,
              f"{label}: start reports the worker stopped at the question, not started, and the command fails: {case['start']}")
        check("running, blocked: hermes is stopped at" in case["status"][1] and question in case["status"][1]
              and case["again"][0] == 1 and "impl-1 not started: " in case["again"][1] and question in case["again"][1]
              and case["starts"] == 1,
              f"{label}: status says so, and a second start is refused without starting another: {case}")
    late = starts["late_retirement"]
    check(late["start"][0] == 1 and "impl-1 failed to start: " in late["start"][1] and "is live and hermes is stopped at" in late["start"][1]
          and "a model-retirement notice" in late["start"][1],
          f"a notice drawn over the ready prompt after the start was first observed is still caught: {late['start']}")
    for label in ("ready", "unreadable"):
        case = starts[label]
        check(case["start"][0] == 0 and "impl-1 started" in case["start"][1] and "blocked" not in case["status"][1],
              f"{label}: a ready (or unreadable) screen starts as before: {case['start']} / {case['status']}")
    from scripts.worker_pool import WorkerReadiness

    fit = dict(role="impl-1", target="t", declared=True, routed=("implementation",), onboarding=True, skill="present",
               runtime="authenticated", account=True, worktree=True, session=True, reservation_known=True)
    check(WorkerReadiness(**fit).can_take_work is True
          and WorkerReadiness(**fit, stopped_at="codex is stopped at x").can_take_work is False
          and WorkerReadiness(**fit, stopped_at="codex is stopped at x").ready is True,
          "a worker stopped at a question cannot take work, and is still not refused a restart (it is no blocker)")
    check(slow_draw() == ["model_retirement", True],
          f"a CLI that is still blank past the settle time has not drawn yet: its question is still caught: {slow_draw()}")
    problems = worker_problem_reads()
    check(problems["codex_ready"] == "" and problems["codex_model_retirement"].startswith("codex is stopped at a model-retirement notice")
          and '("Try new model" / "Use existing model")' in problems["codex_model_retirement"],
          f"set-role-runtime's proof: a new worker at the notice is a problem, so the switch is not reported done: {problems}")

    # Fresh context is /clear for every runtime; nothing sends /new.
    commands = fresh_context()
    check(commands.get("codex") == "/clear" and "/new" not in commands.values(),
          f"Switchyard starts a fresh conversation with /clear, never the /new menu: {commands}")
    wording = hold_wording()
    check(all("Nothing answers it by itself, and nothing was typed into it" in text for text in wording["described"].values())
          and wording["thresholds"] == {"": 120.0, "30": 30.0, "-1": 120.0, "soon": 120.0}
          and wording["older_board"] == ["SELECT ticket_board.note_background_hold(3"],
          f"every question is described with what releases it; two minutes unless configured; an older board is "
          f"never told a question is background work: {wording}")

    # The installed Codex, now: the same three screens, and /clear keeps the checkout.
    real = real_codex()
    if real:
        check(all(case["observed"] and not case["sign_in"] for case in real.values()),
              f"every real observation reached Codex itself, past sign-in: {real}")
        check(real["retirement"]["startup"] == "model_retirement",
              f"a real Codex started on gpt-5.5 stops at the notice, and the start watch names it: {real['retirement']}")
        check(real["new"]["startup"] is None and real["new"]["after"] == "new_conversation_location",
              f"real /new asks where the conversation should run: {real['new']}")
        check(real["clear"]["startup"] is None and real["clear"]["after"] is None
              and real["clear"]["path"] == real["clear"]["cwd"] and real["clear"]["at_prompt"],
              f"real /clear asks nothing, and the pane stays in its checkout: {real['clear']}")
    print(f"provider_prompt_test: {CHECKS} checks ok" + ("" if real else " (no installed Codex to check against)"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
