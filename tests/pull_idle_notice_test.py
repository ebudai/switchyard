#!/usr/bin/env python3
"""SYRD-748: the pull idle notice names only workers that are idle without claiming, never one still working.

Live on MEFP (2026-11-07): MEFP-990 had waited in `pull_ready_source_first` since 04:19 UTC. Audit signed off Luna-2's
MEFP-988 at 18:31:10, freeing its slot (`release_after: audit`). The notice then said "MEFP-990 has been ready to claim
since 2026-11-07 04:19 UTC, and these implementers are idle without claiming it: luna-2 (working)". The Director routed
MEFP-990 by hand 70 s later. That was inside the gate's deliberate 120 s recovery of a Codex turn that ended without
its Stop hook, after which the pickup claims. The notice named a worker still finishing its turn as idle capacity that
had not claimed.

A production-built board runs here with the example workflow opted into pull scheduling. The real HTTP server and
notify listener run, with the real PaneActivityGate and its real pane probes over Codex's measured screens
(tests/fixtures/syrd594_codex_capacity_screens.json). Main is the Codex claimant, registered with a live stand-in
provider process (SYRD-644's rig); app and ops are not running. The Director's pane records what it is told. The
stale-turn window is scaled to 30 s.

Main runs first, from a git archive. These cases run on both:
- **Freed, still finishing its turn:** main, Audit having signed off its ticket, its turn's hook still fresh. Main's
  tree calls it "main (working)" in the idle notice; this tree names nobody, and claims nothing yet.
- **Its turn ends:** it claims the ready ticket.
- **A stale turn, the screen at the empty prompt:** past the window, recovered and claimed, as before.
- **Its turn ended on background work still running:** main's tree names it "(background work running)"; this tree
  does not.
- **Genuinely idle without claiming:** stopped on a prompt with its slot free, it is still named.
"""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "ff29d7761566f481c3a1032a38a13cdc0bc6e667"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
STALE_SECONDS = 30  # production's 120 s, scaled
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(root: Path) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import codex_capacity_retry_test as c594
    import provider_quota_hold_test as rigs
    import ticket_board_write_api_test as fixture
    from assigned_implementation_queue_test import build_board
    from pull_scheduling_policy_test import pull_document
    from scripts.ticket_board import notify_listener as nl
    from temporary_cluster import temporary_cluster

    seen: dict = {}
    providers: dict = {}
    with temporary_cluster(prefix="s748", shutdown="immediate") as cluster, tempfile.TemporaryDirectory(prefix="syrd748.") as tmp:
        admin = build_board(fixture, cluster, root, "board")
        app = fixture.TicketBoardApp(cluster.root / "f", cluster.root / "a", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, "board", fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "board", "ticket_board_listener")
        try:
            def sql(statement: str) -> str:
                return fixture.psql(admin, statement).strip()

            def act(ticket: str, op: str, who: str, **body):
                return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{op}", body, caller=who)

            def where(ticket: str) -> str:
                return sql(f"SELECT state || '/' || assignee FROM ticket_board.tickets WHERE id = '{ticket}';")

            cfg = pull_document()
            cfg["project"] = "pgu"
            for role in cfg["roles"]:
                if role.get("target"):
                    role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
            app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")
            runtimes = {r["name"]: r.get("runtime") for r in cfg["roles"]}
            targets = {r["name"]: r["target"] for r in cfg["roles"] if r.get("target")}
            assert runtimes["main"] == "codex", runtimes  # the example's main is a Codex implementer
            shown = c594.screens()
            panes = {}
            for role in ("main", "director"):
                providers[role] = p = rigs.Provider(Path(tmp))
                sql(f"SELECT ticket_board.register_role_runtime('{role}', '{runtimes[role]}', '{targets[role]}', '/tmp', "
                    f"'/tmp', {p.pid}, {p.ticks}, {os.getuid()}, 0);")
                panes[targets[role]] = c594.Pane(shown["recovered"] if role == "main" else shown["director"], p.pid, p.started)
            tmux, table = c594.fake_tmux(panes)
            state_dir = Path(tmp) / "state"
            store = nl.PaneHookStateStore(state_dir)
            store.write(targets["main"], "idle", source="codex.Stop", now=time.time() - 600)
            store.write(targets["director"], "idle", source="claude.Stop", now=time.time() - 600)
            gate = nl.PaneActivityGate(state_store=store, cursor_position_runner=tmux, capture_pane_runner=tmux,
                                       pane_pid_runner=tmux, process_table_reader=table, sleeper=lambda _s: None,
                                       director_startup_hold_seconds=0, stale_codex_busy_hook_seconds=STALE_SECONDS,
                                       idle_working_timer_sample_delay_seconds=0,
                                       role_runtimes={"main": "codex", "director": "claude"})
            sends: list = []

            def sender(target: str, message: str) -> dict:
                role = next(r for r, t in targets.items() if t == target)
                sends.append((role, message))
                if role == "main":  # Codex starts the turn; it ends without its Stop hook, as an errored turn does
                    store.write(target, "busy", source="codex.UserPromptSubmit")
                return {}

            def passes(count: int = 3) -> None:
                nl.TicketBoardNotifyListener._wait_for_notification = lambda _self, _conn: False
                for _ in range(count):
                    listener = nl.TicketBoardNotifyListener(
                        conninfo=listener_url, project="pgu", sender=sender, activity_gate=gate.is_working,
                        target_exists=lambda _t: True, poll_seconds=0, submission_confirm_seconds=0.2,
                        submission_poll_seconds=0.05, logger=logging.getLogger("syrd748"))
                    listener.delivery_submitter = lambda _t: None
                    listener.unresolved_turn_grace_seconds = 3600
                    listener.listen_once(max_notifications=50)
                    time.sleep(0.2)

            def stale(seconds: float = 30) -> None:
                """The turn-start hook record of main's last turn, `seconds` old: no Stop hook came after it."""
                path = state_dir / (targets["main"].replace(":", "_") + ".json")
                record = json.loads(path.read_text())
                record.update(state="busy", source="codex.UserPromptSubmit", updated_at=time.time() - seconds,
                              turn_started_at=time.time() - seconds)
                path.write_text(json.dumps(record))

            def admitted(ticket: str, *, ready_minutes_ago: int = 0) -> None:
                fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director",
                                             needs_inspection=True, commit_exempt=True)
                act(ticket, "admit", "director")
                if ready_minutes_ago:
                    sql(f"UPDATE ticket_board.ticket_notification_state SET entered_current_state_at = clock_timestamp() "
                        f"- interval '{ready_minutes_ago} minutes' WHERE ticket_id = '{ticket}';")

            def released(ticket: str) -> None:
                """main submits and Audit signs off: release_after audit frees its slot."""
                act(ticket, "submit_to_inspection", "main")
                if where(ticket).startswith("inspection/"):
                    act(ticket, "inspector_sign_off", "inspector")
                act(ticket, "audit_sign_off", "audit", text="Audited.")

            def told(ticket: str) -> list:
                return [m for r, m in sends if r == "director" and ticket in m and "idle without claiming" in m]

            def hook_source() -> str:
                return json.loads((state_dir / (targets["main"].replace(":", "_") + ".json")).read_text()).get("source", "")

            # main claims PGU-1 and works it. Audit signs it off: main is free, its turn still finishing.
            admitted("PGU-1")
            passes(2)
            seen["first"] = where("PGU-1")
            released("PGU-1")
            stale(2)  # its turn's hook, two seconds old: well inside the window
            admitted("PGU-2", ready_minutes_ago=20)  # ready well past the idle-alert interval
            passes(3)
            seen["finishing"] = {"where": where("PGU-2"), "told": told("PGU-2"),
                                 "reserved": sql("SELECT coalesce(ticket_board.ticket_current_reserved_ticket('main'), '-');")}
            # Its turn ends: it claims.
            store.write(targets["main"], "idle", source="codex.Stop")
            passes(2)
            seen["turn_ended"] = {"where": where("PGU-2"), "told": told("PGU-2")}
            # A turn that ended without its Stop hook, past the window, the screen at the empty prompt: recovered, claimed.
            released("PGU-2")
            stale(60)
            admitted("PGU-3")
            passes(3)
            seen["stale"] = {"where": where("PGU-3"), "hook": hook_source()}
            # Its turn ended on background work still running (SYRD-538): working too, not named.
            released("PGU-3")
            path = state_dir / (targets["main"].replace(":", "_") + ".json")
            path.write_text(json.dumps({
                "target": targets["main"], "state": "idle", "source": "codex.Stop", "updated_at": time.time(),
                "background_work": {"pane_pid": providers["main"].pid, "pane_start_time": providers["main"].ticks,
                                    "session_id": "c-1", "tasks": [{"id": "b1", "type": "subagent", "status": "running"}]}}))
            admitted("PGU-4", ready_minutes_ago=20)
            passes(3)
            seen["background"] = {"where": where("PGU-4"), "told": told("PGU-4")}
            # Genuinely idle without claiming: stopped on a prompt, its slot free. Still named. (The notice considers the
            # oldest waiting ticket, so PGU-4 is withdrawn first.)
            act("PGU-4", "withdraw", "director")
            store.write(targets["main"], "blocked", source="codex.PermissionRequest")
            admitted("PGU-5", ready_minutes_ago=20)
            passes(3)
            seen["prompt"] = {"where": where("PGU-5"), "told": told("PGU-5")}
        finally:
            server.shutdown()
            server.server_close()
            for provider in providers.values():
                try:
                    provider.stop()
                except Exception:  # noqa: BLE001 -- already stopped
                    pass
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root)],
                           capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                           env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


def both(got: dict, tree: str) -> None:
    check(got["first"] == "in_progress/main", f"{tree}: main claims its first ticket: {got['first']}")
    check(got["turn_ended"]["where"] == "in_progress/main", f"{tree}: once its turn ends, main claims: {got['turn_ended']}")
    check(got["stale"]["where"] == "in_progress/main" and got["stale"]["hook"] == "codex.UserPromptSubmit",
          f"{tree}: a stale turn at the empty prompt is recovered and claimed for, as before: {got['stale']}")
    prompt = got["prompt"]
    check(prompt["where"] == "ready/unassigned" and len(prompt["told"]) == 1
          and "idle without claiming it: main (stopped on a prompt)" in prompt["told"][0],
          f"{tree}: a worker stopped on a prompt with its slot free is still named, once: {prompt}")


def before(got: dict) -> None:
    both(got, "main")
    background = got["background"]
    check(background["where"] == "ready/unassigned" and len(background["told"]) == 1
          and "main (background work running)" in background["told"][0],
          f"main: with background work still running, main is named idle without claiming: {background}")
    finishing = got["finishing"]
    check(finishing["reserved"] == "-" and finishing["where"] == "ready/unassigned" and len(finishing["told"]) == 1
          and "idle without claiming it: main (working)" in finishing["told"][0],
          f"main: freed but still finishing its turn, main is named idle without claiming -- MEFP's 'luna-2 (working)': "
          f"{finishing}")


def candidate(got: dict) -> None:
    both(got, "this tree")
    finishing = got["finishing"]
    check(finishing["reserved"] == "-" and finishing["where"] == "ready/unassigned" and finishing["told"] == [],
          f"freed but still finishing its turn, main is named nowhere, and claims nothing yet: {finishing}")
    check(got["turn_ended"]["told"] == [], f"and nobody is told anything when it then claims: {got['turn_ended']}")
    background = got["background"]
    check(background["where"] == "ready/unassigned" and background["told"] == [],
          f"with background work still running, main is not named either, and claims nothing yet: {background}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]))))
        return 0
    if os.environ.get("SYRD748_MUTATION_SKIP_BEFORE") != "1":  # a mutant changes this tree only
        with tempfile.TemporaryDirectory(prefix="syrd748-before.", dir="/tmp") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(run(ROOT))
    print(f"pull_idle_notice_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
