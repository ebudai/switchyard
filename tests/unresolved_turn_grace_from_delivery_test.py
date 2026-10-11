#!/usr/bin/env python3
"""SYRD-584: the unresolved-turn escalation fired while the owner was ten minutes into a turn on the ticket.

Live on Otto (release f92a3d4d), OTTO-139 with main (Codex):
1. main ended a turn unresolved, and Codex then showed its own menu.
2. The board's repair prompt waited behind the menu until the Director
   answered it.
3. Once delivered, the prompt started a turn on OTTO-139.
4. Ten minutes into that turn the Director was told "main ended a turn without
   resolving OTTO-139 ... and the grace period has passed after being prompted".

The escalation timed its grace from the prompt's enqueue, and never asked
whether the owner had worked since.

This suite runs on a production-built board (companion roles, schema.sql, the
real migrate runner, rbac.sql, a declared workflow). It uses the real notify
listener, driven one loop iteration at a time, and the real PaneActivityGate
reading real hook-state files written by the real PaneHookStateStore. Only the
gate's live-pane probes (tmux, /proc) are under the scenario's control. The
grace is a few seconds, so time passes for real. The cases:

- **A, Otto's sequence:**
  - a turn ends unresolved, and the prompt is held behind a menu;
  - once it is delivered, the woken turn works past the grace;
  - that turn ends, and a later prompt wakes a turn that ends unresolved too.
- **B, an owner who never answers:** the prompt is held, then delivered, and the
  owner stays idle.
- **C, a stale background child (SYRD-583):** the prompt is delivered, the hook
  stays idle, and the pane only looks busy.

It runs on main, from a git archive in a child process, and on this tree.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "2b7fb5c4bcbb55ce8818ccaf49f4f2c2bca9c5f2"  # main before SYRD-584
GRACE = 4.0
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def scenario(root: Path, prefix: str) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from tmux_bus_isolation import isolate_tmux_bus

    isolate_tmux_bus()
    import psycopg
    import ticket_board_write_api_test as fixture
    from scripts.ticket_board.notify_listener import ActivityTrace, PaneActivityGate, PaneHookStateStore, TicketBoardNotifyListener
    from temporary_cluster import temporary_cluster

    class Gate(PaneActivityGate):
        """The real gate over real hook files; only what it would read from tmux and /proc is the scenario's."""

        def __init__(self, store) -> None:
            super().__init__(state_store=store)
            self.busy: dict[str, str] = {}  # target -> why the pane looks busy

        def _full_activity_trace(self, target, check_trusted_working=False):
            reason = self.busy.get(target)
            return ActivityTrace(reason is not None, reason or "scenario_idle")

        def background_work_roles(self, roles=None):
            return set()

        def composer_snapshot(self, target):
            return None

    seen: dict = {}
    with temporary_cluster(prefix=prefix, shutdown="immediate") as cluster, tempfile.TemporaryDirectory() as panes:
        dbname = "board"
        admin = fixture.conninfo(cluster.socket_dir, cluster.port, dbname)
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
        fixture.psql(admin, (root / "scripts/ticket_board/schema.sql").read_text())
        fixture.create_roles(admin)
        fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        result = subprocess.run(["bash", str(root / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
        assert result.returncode == 0, result.stderr[-1500:]
        fixture.psql(admin, (root / "scripts/ticket_board/rbac.sql").read_text())
        app = fixture.TicketBoardApp(cluster.root / "frames", cluster.root / "assets", project="pgu", ticket_prefix="PGU",
                                     database_url=fixture.conninfo(cluster.socket_dir, cluster.port, dbname, fixture.SERVICE_ROLE))
        server = fixture.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=fixture.QuietNotifier())
        fixture.TEST_WRITE_TOKEN = server.write_token
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{server.server_port}"
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, dbname, "ticket_board_listener")

        def act(ticket: str, operation: str, actor: str, **payload):
            return fixture.post_json(base, f"/api/tickets/{ticket}/actions/{operation}", payload, caller=actor)

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        cfg = json.loads((root / "examples/workflows/inspection.json").read_text())
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

        store = PaneHookStateStore(panes)
        gate = Gate(store)
        sent: list[tuple[str, str]] = []
        listener = TicketBoardNotifyListener(conninfo=listener_url, sender=lambda target, message: sent.append((target, message)),
                                             activity_gate=gate.is_working, target_exists=lambda _target: True,
                                             submission_witness=lambda _target, _since: True, poll_seconds=0, project="pgu")
        listener.unresolved_turn_grace_seconds = GRACE

        def target(role: str) -> str:
            return f"pgu-{role}:0.0"

        def hook(role: str, state: str, event: str) -> None:
            store.write(target(role), state, source=f"codex.{event}")

        def start_turn(role: str) -> None:
            hook(role, "busy", "UserPromptSubmit")

        def end_turn(role: str) -> None:
            hook(role, "idle", "Stop")

        def one_pass(menu_after: str | None = None) -> list[str]:
            """One iteration of the listener's loop, as listen_once runs it, without its wait.

            `menu_after` puts that role's CLI on a menu once the turn-end pass
            has run: the menu came up after the turn ended, as on Otto.
            """
            before = len(sent)
            with psycopg.connect(listener_url, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                gate.role_targets = dict(listener.role_targets)
                for step in ("process_reminder_snooze_due", "process_idle_turn_end_nudges", "process_idle_stall_nudges",
                             "process_serial_focus_queue_wakeups"):
                    if hasattr(listener, step):
                        getattr(listener, step)(conn)
                if menu_after:
                    gate.busy[target(menu_after)] = "provider_menu"
                listener.process_due_notifications(conn)
            return [message for _target, message in sent[before:]]

        def escalations(ticket: str) -> list[str]:
            return [m for t, m in sent if t == target("director") and f"without resolving {ticket} " in m]

        def prompts(ticket: str, role: str) -> list[str]:
            return [m for t, m in sent if t == target(role) and m.startswith(f"{ticket} is still yours")]

        def epoch(statement: str) -> float | None:
            value = sql(f"SELECT extract(epoch FROM ({statement}))::text;")
            return float(value) if value else None

        def escalated_at(ticket: str) -> float | None:
            return epoch("SELECT min(ts) FROM ticket_board.notification_trace WHERE ticket_id = "
                         f"'{ticket}' AND target_role = 'director' AND kind = 'unresolved_turn' AND event = 'enqueue'")

        def watch(ticket: str, seconds: float) -> list[float]:
            """Passes, once a second, for `seconds`; when (since start) the Director had heard about `ticket`."""
            start, heard = time.time(), []
            while time.time() - start < seconds:
                one_pass()
                if escalations(ticket):
                    heard.append(round(time.time() - start, 1))
                time.sleep(1.0)
            return heard

        authors = {"PGU-139": "main", "PGU-140": "app", "PGU-141": "ops"}
        for ticket in authors:
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="analysis", assignee="director", commit_exempt=True)
        for role in ("director", *authors.values()):
            end_turn(role)
        one_pass()

        def begin(ticket: str) -> None:
            """Routed and handed over; the owner works a turn on it and ends without resolving it."""
            role = authors[ticket]
            act(ticket, "route", "director", state="in_progress", assignee=role)
            one_pass()
            start_turn(role)
            time.sleep(0.05)
            end_turn(role)

        # A: Otto's sequence. The prompt waits behind a menu for half the grace.
        T, role = "PGU-139", "main"
        begin(T)
        one_pass(menu_after=role)
        seen["a_held"] = prompts(T, role)
        time.sleep(GRACE / 2)
        one_pass()
        gate.busy.pop(target(role))  # the Director answers the menu
        seen["a_delivered"] = [m for m in one_pass() if m.startswith(f"{T} is still yours")]
        start_turn(role)  # the prompt wakes a turn on the ticket
        gate.busy[target(role)] = "hook_busy"
        seen["a_mid_turn"] = watch(T, 2.5 * GRACE)
        seen["a_mid_turn_escalated_at"] = escalated_at(T)
        # The woken turn ends unresolved. The next prompt wakes a short turn that
        # ends unresolved too: that turn's own grace, and only then, escalates.
        gate.busy.pop(target(role))
        end_turn(role)
        one_pass()
        seen["a_second_prompt"] = len(prompts(T, role))
        start_turn(role)
        time.sleep(0.05)
        end_turn(role)
        last_end = time.time()
        seen["a_after_end"] = watch(T, GRACE + 3)
        seen["a_end_to_escalation"] = (escalated_at(T) or 0) - last_end
        seen["a_escalations"] = escalations(T)
        seen["a_prompts"] = len(prompts(T, role))

        # B: an owner who never answers. The prompt is held for most of the
        # grace, then delivered; the owner stays idle.
        T, role = "PGU-140", "app"
        begin(T)
        one_pass(menu_after=role)
        held_from = time.time()
        time.sleep(GRACE * 0.75)
        one_pass()
        gate.busy.pop(target(role))
        one_pass()
        delivered = time.time()
        seen["b_delivered_after"] = round(delivered - held_from, 1)
        # A hook write that is not a turn -- Claude's idle notification -- is
        # not the owner working, and does not move the grace.
        time.sleep(GRACE / 2)
        store.write(target(role), "idle", source="claude.Notification")
        seen["b_watch"] = watch(T, GRACE / 2 + 3)
        seen["b_delivery_to_escalation"] = (escalated_at(T) or 0) - delivered
        seen["b_escalations"] = escalations(T)

        # C: the prompt is delivered at once, the owner's hook stays idle, and a
        # background child makes the pane look busy (SYRD-583).
        T, role = "PGU-141", "ops"
        begin(T)
        one_pass()
        delivered = time.time()
        gate.busy[target(role)] = "pane_child_work"
        seen["c_watch"] = watch(T, GRACE + 3)
        seen["c_delivery_to_escalation"] = (escalated_at(T) or 0) - delivered
        seen["c_escalations"] = escalations(T)

        # An older listener's call, three arguments, still reaches the guard;
        # and a fresh install has the same guard, owner and grants as this
        # migrated board, with no three-argument overload left behind.
        seen["old_call"] = fixture.psql(listener_url, "SELECT ticket_board.notify_unresolved_turn_end("
                                                      "'{}'::jsonb, clock_timestamp(), interval '600 seconds');").strip()
        fixture.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "fresh"])
        fresh = fixture.conninfo(cluster.socket_dir, cluster.port, "fresh")
        fixture.psql(fresh, (root / "scripts/ticket_board/schema.sql").read_text())
        fixture.psql(fresh, (root / "scripts/ticket_board/rbac.sql").read_text())
        shape = ("SELECT coalesce(json_agg(json_build_array(p.oid::regprocedure::text, md5(pg_get_functiondef(p.oid)), "
                 "pg_get_userbyid(p.proowner), coalesce(p.proacl::text, '')) ORDER BY p.oid::regprocedure::text), '[]')::text "
                 "FROM pg_proc p WHERE p.proname = 'notify_unresolved_turn_end' AND p.pronamespace = 'ticket_board'::regnamespace "
                 "AND p.pronargs >= 3;")
        seen["fresh_shape"] = json.loads(fixture.psql(fresh, shape))
        seen["migrated_shape"] = json.loads(sql(shape))
        server.shutdown()
        server.server_close()
    return seen


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests", "examples"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd584b-")))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd584.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    check(before["a_held"] == [] and len(before["a_delivered"]) == 1 and before["a_mid_turn"],
          f"main: the prompt waited behind the menu, was delivered and woke a turn, and the Director was told "
          f"while that turn was running: {before['a_mid_turn']}")
    check(before["b_watch"] and before["b_delivery_to_escalation"] < GRACE - 1,
          f"main: after a held prompt the grace ran from its enqueue, so the owner had less than the grace to "
          f"answer it: {before['b_delivery_to_escalation']:.1f}s")

    a = scenario(ROOT, "syrd584a-")
    check(a["a_held"] == [] and a["a_delivered"] and a["a_delivered"][0].startswith("PGU-139 is still yours"),
          f"A: the prompt waited behind the menu, then reached the owner: {a['a_delivered']}")
    check(a["a_mid_turn"] == [] and a["a_mid_turn_escalated_at"] is None,
          f"A: through the woken turn, two and a half graces long, the Director is told nothing: {a['a_mid_turn']}")
    check(a["a_second_prompt"] == 2,
          f"A: the woken turn ended unresolved past the first prompt's grace, so the owner is prompted again: {a['a_second_prompt']}")
    check(len(a["a_escalations"]) == 1 and GRACE - 0.5 <= a["a_end_to_escalation"] <= GRACE + 2.5,
          f"A: the later unresolved turn escalates once, its own grace after it ended: "
          f"{a['a_end_to_escalation']:.1f}s {a['a_escalations']}")
    check(a["a_prompts"] == 2,
          f"A: the short turn that ended within the second prompt's grace is not prompted again: {a['a_prompts']}")
    check(len(a["b_escalations"]) == 1 and GRACE - 0.5 <= a["b_delivery_to_escalation"] <= GRACE + 1.4,
          f"B: an owner who never answers is reported once, a grace after the prompt reached them, however long it "
          f"was held first ({a['b_delivered_after']}s), and an idle notification since is not a turn: "
          f"{a['b_delivery_to_escalation']:.1f}s")
    check(len(a["c_escalations"]) == 1 and a["c_delivery_to_escalation"] <= GRACE + 2.5,
          f"C: a pane that only looks busy, its hook idle, is not a turn: reported a grace after delivery: "
          f"{a['c_delivery_to_escalation']:.1f}s")
    check(a["old_call"].lstrip("-").isdigit(), f"an older listener's three-argument call still reaches the guard: {a['old_call']}")
    signatures = [shape[0] for shape in a["migrated_shape"]]
    check(a["migrated_shape"] == a["fresh_shape"]
          and signatures == ["ticket_board.notify_unresolved_turn_end(jsonb,timestamp with time zone,interval,jsonb)"]
          and "ticket_board_listener=X" in a["migrated_shape"][0][3],
          f"schema.sql and rbac.sql install what the migrations do -- one guard, its owner and its grants: "
          f"{a['migrated_shape']} {a['fresh_shape']}")
    print(f"unresolved_turn_grace_from_delivery_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
