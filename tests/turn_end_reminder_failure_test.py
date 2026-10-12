#!/usr/bin/env python3
"""SYRD-482: a failed turn-end reminder query skipped the unresolved-turn and permission-prompt checks.

`process_idle_turn_end_nudges` asks the board for turn-end reminders, then runs
the unresolved-turn guard and the permission-prompt check, "whether or not a
reminder was due, and whether or not one was enqueued" (SYRD-194). But it
returned early when the reminder query raised, gave no row, or gave a value
that is not an integer, before either check ran.

That pass had already marked the turn-end boundary as seen, so the next pass
handed the guard an empty map. The boundary was lost for good:
- the owner was never prompted about that unresolved turn;
- a continuation lease taken for an earlier turn was not retired;
- a pane stopped on a permission prompt waited a pass longer to be reported.

This suite runs on a production-built board (companion roles, schema.sql, the
real migrate runner, rbac.sql, a declared workflow). It uses the real notify
listener, driven one loop iteration at a time, and the real PaneActivityGate
reading real hook-state files written by the real PaneHookStateStore. Only the
gate's live-pane probes (tmux, /proc) are under the scenario's control, and
only the reminder query is: a stand-in connection passes every other statement
to the board. The reminder query answers four ways:
- **ok:** the real query;
- **raise:** a real server error, from `SELECT 1/0`;
- **none:** no row;
- **text:** a real text value, from `SELECT 'not a count'`.

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
BEFORE = "e6fe539b746e4e1aaacffdf40897b7f7729b366a"  # main before SYRD-482
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
MODES = {"ok": "inspector", "raise": "main", "none": "app", "text": "ops"}  # reminder answer -> the owner whose turn ends
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

        def _full_activity_trace(self, target, check_trusted_working=False):
            return ActivityTrace(False, "scenario_idle")

        def background_work_roles(self, roles=None):
            return set()

        def composer_snapshot(self, target):
            return None

    class NoRow:
        def fetchone(self):
            return None

    class Board:
        """The listener's connection, except that the reminder query answers as `mode` says."""

        def __init__(self, conn, mode: str) -> None:
            self.conn, self.mode, self.reminder_queries = conn, mode, 0

        def execute(self, statement, params=None):
            if "notify_idle_turn_end_nudges" in str(statement) and self.mode != "ok":
                self.reminder_queries += 1
                if self.mode == "raise":
                    return self.conn.execute("SELECT 1/0")
                if self.mode == "none":
                    return NoRow()
                return self.conn.execute("SELECT 'not a count'::text")
            return self.conn.execute(statement, params)

        def __getattr__(self, name):
            return getattr(self.conn, name)

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

        def sql(statement: str) -> str:
            return fixture.psql(admin, statement).strip()

        cfg = json.loads((root / "examples/workflows/inspection.json").read_text())
        cfg["project"] = "pgu"
        for role in cfg["roles"]:
            if role.get("target"):
                role["target"] = role["target"].replace("cerulean-", "pgu-", 1)
        app.apply_workflow(cfg, expected_revision=0, dry_run=False, caller_role="director")

        store = PaneHookStateStore(panes)
        gate = Gate(state_store=store)
        sent: list[tuple[str, str]] = []
        listener = TicketBoardNotifyListener(conninfo=listener_url, sender=lambda target, message: sent.append((target, message)),
                                             activity_gate=gate.is_working, target_exists=lambda _target: True,
                                             submission_witness=lambda _target, _since: True, poll_seconds=0, project="pgu")
        listener.unresolved_turn_grace_seconds = 600
        listener.permission_prompt_grace_seconds = 1

        def target(role: str) -> str:
            return f"pgu-{role}:0.0"

        def one_pass(mode: str = "ok") -> dict:
            """One iteration of the listener's loop, as listen_once runs it, without its wait."""
            with psycopg.connect(listener_url, autocommit=True) as conn:
                listener.refresh_workflow(conn)
                gate.role_targets = dict(listener.role_targets)
                board = Board(conn, mode)
                listener.process_reminder_snooze_due(board)
                returned = listener.process_idle_turn_end_nudges(board)
                listener.process_idle_stall_nudges(conn)
                listener.process_serial_focus_queue_wakeups(conn)
                listener.process_due_notifications(conn)
            return {"returned": returned, "reminder_queries": board.reminder_queries}

        def count(statement: str) -> int:
            return int(sql(f"SELECT count(*) FROM ({statement}) AS c;"))

        def prompts(ticket: str) -> int:
            return count(f"SELECT 1 FROM ticket_board.notification_trace WHERE ticket_id = '{ticket}' "
                         "AND kind = 'unresolved_turn_repair' AND event = 'enqueue'")

        def lease_open(ticket: str) -> bool:
            return count(f"SELECT 1 FROM ticket_board.turn_continuation_lease WHERE ticket_id = '{ticket}' "
                         "AND consumed_at IS NULL") > 0

        def permission_reports(since: str) -> int:
            return count("SELECT 1 FROM ticket_board.notification_trace WHERE event = 'enqueue' "
                         f"AND detail ->> 'dedupe_key' = 'permission-prompt:audit:{since}'")

        tickets = {mode: f"PGU-{n}" for n, mode in enumerate(MODES, start=1)}
        for mode, ticket in tickets.items():
            fixture.seed_postgres_ticket(admin, ticket, title=ticket, state="inspection" if mode == "ok" else "in_progress",
                                         assignee=MODES[mode],
                                         commit_exempt=True)
        fixture.seed_postgres_ticket(admin, "PGU-9", title="PGU-9", state="audit", assignee="audit",
                                     commit_exempt=True)
        for role in ("director", "audit", *MODES.values()):
            store.write(target(role), "idle", source="codex.Stop")
        one_pass()
        one_pass()

        for mode, role in MODES.items():
            ticket = tickets[mode]
            # The owner's earlier turn took a lease; this turn starts and ends
            # without resolving the ticket.
            sql("INSERT INTO ticket_board.turn_continuation_lease (ticket_id, owner_role, turn_id, reason, expires_at) "
                f"VALUES ('{ticket}', '{role}', 'an earlier turn', 'continuing', clock_timestamp() + interval '1 hour');")
            store.write(target(role), "busy", source="codex.UserPromptSubmit")
            time.sleep(0.05)
            store.write(target(role), "idle", source="codex.Stop")
            # Meanwhile audit's pane stops on a permission prompt.
            store.write(target("audit"), "blocked", source="codex.PermissionRequest")
            since = gate.permission_prompt_waits()["audit"]
            time.sleep(1.3)
            first = one_pass(mode)
            seen[mode] = {
                **first,
                "boundary": [r for r, _ in listener.idle_nudges._seen_turn_end_idle_since_by_role.items() if r == role],
                "prompted": prompts(ticket),
                "lease_open": lease_open(ticket),
                "permission_reported": permission_reports(since),
            }
            later = one_pass()
            seen[mode]["next_pass"] = {"prompted": prompts(ticket), "lease_open": lease_open(ticket),
                                       "permission_reported": permission_reports(since), "returned": later["returned"]}
            seen[mode]["owner_prompts_sent"] = len([m for t, m in sent if t == target(role)
                                                    and m.startswith(f"{ticket} is still yours")])
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
        print(json.dumps(scenario(Path(sys.argv[2]), "syrd482b-")))
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd482.") as tmp:
        before_root = tree_at(BEFORE, Path(tmp) / "before")
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(before_root)],
                               capture_output=True, text=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        assert child.returncode == 0, child.stderr[-3000:]
        before = json.loads(child.stdout.strip().splitlines()[-1])
    after = scenario(ROOT, "syrd482a-")
    failures = [mode for mode in MODES if mode != "ok"]
    for mode in failures:
        b = before[mode]
        check(b["reminder_queries"] == 1 and b["boundary"] == [MODES[mode]] and b["prompted"] == 0
              and b["next_pass"]["prompted"] == 0 and b["next_pass"]["lease_open"] and b["permission_reported"] == 0,
              f"main, reminder query '{mode}': the turn end was marked seen, the owner was never prompted, not on that "
              f"pass nor the next, the earlier turn's lease stayed open, and the permission prompt waited a pass: {b}")
    ok = after["ok"]
    check(ok["boundary"] == [MODES["ok"]] and ok["prompted"] == 1 and not ok["lease_open"] and ok["permission_reported"] == 1,
          f"when the reminder query works, the turn end prompts its owner, retires the earlier lease and reports the "
          f"permission prompt, all on that pass: {ok}")
    for mode in failures:
        a = after[mode]
        check(a["reminder_queries"] == 1 and a["returned"] == 0,
              f"reminder query '{mode}': it was asked once, and the pass still enqueued no reminder: {a}")
        check(a["prompted"] == 1 and not a["lease_open"] and a["permission_reported"] == 1,
              f"reminder query '{mode}': on the same pass the owner is prompted about the unresolved turn, the earlier "
              f"turn's lease is retired, and the permission prompt is reported: {a}")
        check(a["boundary"] == [MODES[mode]] and a["next_pass"]["prompted"] == 1 and a["owner_prompts_sent"] == 1
              and a["next_pass"]["permission_reported"] == 1,
              f"reminder query '{mode}': the boundary is still acted on once -- the next pass repeats nothing, and the "
              f"owner gets one prompt: {a}")
    print(f"turn_end_reminder_failure_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
