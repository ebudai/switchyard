#!/usr/bin/env python3
"""SYRD-724: each project declares how long runnable work may go without durable progress before its board stall
alert, and only the Director changes it.

Production-built boards (router_role_board_test's: schema.sql, roles, rbac.sql, the real ticket-board-migrate,
rbac.sql again), the real HTTP server, the real Unix-socket server under process authority for the Director's pane,
and the real notify listener. Time is the evaluator's `p_now`, given explicitly, as board_stall_test gives it. Main
runs first, from a git archive of its own tree, then this tree.

- Main: a workflow cannot declare a threshold, there is no command, and a project that needs an hour is alerted at
  the listener's 30 minutes.
- Three projects in one cluster -- an hour, nothing declared, 15 minutes -- each alert at their own time, and the
  status says which threshold each uses and why.
- `ticket-board-write set-board-stall-threshold`, from the Director's registered pane over the socket: a preview
  that changes nothing, an apply at the reviewed revision, a moved revision refused, both bounds, every refused value
  leaving the prior setting, and `--default`. Other roles, and any request over HTTP, are refused.
- Changes mid-interval: an alert already sent is not sent again whichever way the threshold moves, and progress
  then starts a clock under the new value; raised before an alert, the alert waits; lowered past the time already
  elapsed, exactly one alert.
- A board restart and a listener restart keep it; a runtime switch and a role-prompt edit, which rebuild the
  document from the live one, keep it; main's board, upgraded, honours it.
- Root's packet racing a Director apply, both orders, through their real paths queued on the workflow lock: the
  Director's value stands; the previous candidate's packet, without its lock statement, loses it.
- Root's provisioning packet, run again from root's record as it was before the Director's change, keeps the
  Director's value and applies root's record for everything else; a record carrying an older value of its own does
  not bring it back, a value the Director cleared stays cleared, and a board running no workflow yet is seeded with
  root's record exactly.
- Only this alert reads it: a busy pane's role stall escalation still uses the listener's own value, and a value
  that is somehow malformed in the stored document reads as the default, never as monitoring off.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree.
BEFORE = "325782664c86fce9f52008034c2f506e816a49ac"
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu1025_syrd724_board_stall_threshold.sql"
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def declaring(base: dict, seconds: int | None) -> dict:
    document = json.loads(json.dumps(base))
    if seconds is not None:
        document["board_stall"] = {"threshold_seconds": seconds}
    return document


class Quiet:
    def notify_ticket_created(self, *a, **k) -> None: ...
    def notify_change(self, *a, **k) -> None: ...


def cli(argv: list[str]) -> list:
    """`ticket-board-write`, as a pane runs it: [exit code, the JSON it printed or {}, what it said on stderr]."""
    from scripts.ticket_board import write_cli

    printed, errors = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(errors):
        try:
            code = write_cli.main(argv)
        except SystemExit as exc:
            code = exc.code
        except Exception as exc:  # an answer, not a crash
            code, _ = f"raised {type(exc).__name__}", errors.write(str(exc))
    try:
        out = json.loads(printed.getvalue() or "{}")
    except json.JSONDecodeError:
        out = {"text": printed.getvalue()[-300:]}
    return [code, out, errors.getvalue().strip()[:3000]]


def main_scenario(cluster) -> dict:
    """Main: no declaration, no command, the listener's 30 minutes for every project."""
    from board_stall_test import Stall, example
    from router_role_board_test import Board
    from scripts.ticket_board.workflow_config import validate

    seen: dict = {}
    try:
        validate(declaring(example(), 3600))
        seen["validate"] = "accepted"
    except ValueError as exc:
        seen["validate"] = str(exc)
    seen["command"] = cli(["--board-url", "http://127.0.0.1:9", "--caller-role", "director",
                           "set-board-stall-threshold", "--seconds", "3600"])
    board = Board(cluster, "mainboard", example())
    try:
        stall = Stall(board)
        stall.ticket("in_progress", "app")
        stall.evaluate(0)
        seen["at_31"] = [stall.evaluate(31)["episode"] is not None, stall.digests()]
        seen["status"] = {k: stall.state().get(k) for k in ("threshold_seconds", "threshold_source")}
    finally:
        board.close()
    return seen


def concurrent(cluster) -> dict:
    """Three projects in one cluster, one listener default of 30 minutes: an hour, nothing declared, 15 minutes."""
    from board_stall_test import Stall, example
    from router_role_board_test import Board

    boards = {name: Board(cluster, name, declaring(example(), seconds))
              for name, seconds in (("alpha", 3600), ("beta", None), ("gamma", 900))}
    seen: dict = {"timeline": {}}
    try:
        stalls = {name: Stall(board) for name, board in boards.items()}
        for stall in stalls.values():
            stall.ticket("in_progress", "app")
            stall.evaluate(0)
        for minute in (14, 16, 29, 31, 59, 61):
            seen["timeline"][str(minute)] = {name: (stall.evaluate(minute), stall.digests())[1]
                                             for name, stall in stalls.items()}
        seen["status"] = {name: {k: stall.state().get(k) for k in
                                 ("threshold_seconds", "threshold_source", "default_threshold_seconds")}
                          for name, stall in stalls.items()}
        seen["fires_after"] = {name: boards[name].sql(
            "SELECT extract(epoch FROM ((s->>'fires_at')::timestamptz - (s->>'clock_started_at')::timestamptz))::int "
            "FROM (SELECT ticket_board.board_stall_status(clock_timestamp()) AS s) x;") for name in boards}
        seen["episodes"] = {name: board.sql("SELECT string_agg(threshold_seconds::text, ',' ORDER BY id) "
                                            "FROM ticket_board.board_stall_episodes;")
                            for name, board in boards.items()}
        seen["read"] = {}
        from scripts.ticket_board import read_client
        for name in ("alpha", "beta"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                read_client.main(["--board-url", boards[name].base, "stall"])
            seen["read"][name] = out.getvalue().splitlines()[0]
        # Changes while gamma's alert is open: raised, then lowered; then the owner's progress.
        gamma, g = boards["gamma"], stalls["gamma"]
        apply = lambda board, seconds: board.app.apply_workflow(  # noqa: E731
            declaring(example(), seconds), expected_revision=board.app.workflow_document()["revision"], dry_run=False,
            caller_role="director")
        apply(gamma, 3600)
        for minute in (62, 70):
            g.evaluate(minute)
        seen["gamma_raised"] = [g.digests(), g.episodes()[-1]["closed"]]
        apply(gamma, 300)
        for minute in (71, 72):
            g.evaluate(minute)
        seen["gamma_lowered"] = [g.digests(), g.episodes()[-1]["closed"]]
        before = g.last_event_id()
        gamma.act(gamma.sql("SELECT id FROM ticket_board.tickets WHERE state = 'in_progress' LIMIT 1;"),
                  "add_comment", "app", text="checkpoint: the long suite is half way")
        g.stamp_progress(before, 73)
        g.evaluate(74)
        seen["gamma_progress"] = [g.digests(), g.episodes()[-1]["reason"]]
        seen["gamma_after"] = [(g.evaluate(77), g.digests())[1], (g.evaluate(78.5), g.digests())[1]]
        # Before any alert: beta is settled and starts again; raised at 20 minutes, the alert waits for the hour.
        b = stalls["beta"]
        b.settle([t for t in b.runnable()])
        b.evaluate(100)
        b.ticket("in_progress", "app")
        b.evaluate(100)
        apply(boards["beta"], 3600)
        seen["beta_fires_after_change"] = boards["beta"].sql(  # read before the next pass evaluates under it
            "SELECT extract(epoch FROM ((s->>'fires_at')::timestamptz - (s->>'clock_started_at')::timestamptz))::int "
            "FROM (SELECT ticket_board.board_stall_status(clock_timestamp()) AS s) x;")
        seen["beta_raised"] = [(b.evaluate(minute), b.digests())[1] for minute in (120, 131, 159, 161)]
        # Alpha's alert is sent; settled, it starts again under the hour and is lowered to 30 minutes at 40.
        a = stalls["alpha"]
        a.settle([t for t in a.runnable()])
        a.evaluate(200)
        a.ticket("in_progress", "app")
        a.evaluate(200)
        seen["alpha_before"] = [(a.evaluate(240), a.digests())[1]]
        apply(boards["alpha"], 1800)
        seen["alpha_lowered"] = [(a.evaluate(minute), a.digests())[1] for minute in (240.5, 241, 250, 260)]
        # A value somehow stored out of bounds or not a number is read as the default, never as monitoring off.
        seen["corrupt"] = {}
        for name, value in (("small", "200"), ("large", "90000"), ("text", '"1h"'), ("digits_as_text", '"3600"'),
                            ("fraction", "3600.5")):
            boards["beta"].sql("UPDATE ticket_board.workflow_configuration SET document = jsonb_set(document, "
                               f"'{{board_stall}}', '{{\"threshold_seconds\": {value}}}') WHERE singleton;")
            try:
                seen["corrupt"][name] = {k: b.state().get(k) for k in ("threshold_seconds", "threshold_source")}
            except Exception as exc:  # an answer, not a crash
                seen["corrupt"][name] = str(exc).splitlines()[0][:200]
        boards["beta"].sql("UPDATE ticket_board.workflow_configuration SET document = document - 'board_stall' "
                           "WHERE singleton;")
        # Nothing declared: the listener's own setting is the default, here configured as 20 minutes.
        b.settle([t for t in b.runnable()])
        b.evaluate(300, threshold=1200)
        b.ticket("in_progress", "app")
        b.evaluate(300, threshold=1200)
        before_listener = b.digests()
        seen["listener_default"] = []
        for minute in (319, 321):
            b.evaluate(minute, threshold=1200)
            seen["listener_default"].append(b.digests() - before_listener)
        seen["listener_status"] = {k: b.state().get(k) for k in ("threshold_seconds", "threshold_source",
                                                                 "default_threshold_seconds")}
    finally:
        for board in boards.values():
            board.close()
    return seen


class Panes:
    """The board's Unix socket under process authority, and this process registered as one role's live pane."""

    def __init__(self, board, cluster, name: str) -> None:
        from scripts.ticket_board.peer_identity import SessionIdentity, read_process
        from scripts.ticket_board.server import ProcessRoleAuthority, TicketBoardUnixServer

        self.board, self.me, self.uid = board, os.getpid(), os.getuid()
        self.start = read_process(self.me).start_time
        authority = ProcessRoleAuthority(board.app, "tenant", resolve_uid=lambda _a: self.uid,
                                         resolve_session=lambda pid: SessionIdentity(pid, read_process(pid).start_time),
                                         session_live=lambda _i: True)
        self.path = cluster.root / f"{name}.sock"
        self.unix = TicketBoardUnixServer(self.path, board.app, events=Quiet(), director_notifier=Quiet(),
                                          role_authority=authority)
        threading.Thread(target=self.unix.serve_forever, daemon=True).start()

    def register(self, role: str) -> None:
        self.board.sql(f"DELETE FROM ticket_board.role_runtime_assignments WHERE role = '{role}' OR process_pid = "
                       f"{self.me}; INSERT INTO ticket_board.role_runtime_assignments (role, runtime, actual_target, "
                       f"worktree, session_dir, process_pid, process_start_time, process_uid) SELECT '{role}', "
                       f"definition->>'runtime', definition->>'target', '/w', '/s', {self.me}, {self.start}, {self.uid} "
                       f"FROM ticket_board.workflow_roles WHERE name = '{role}';")

    def command(self, role: str, *argv: str) -> list:
        return cli(["--socket", str(self.path), "--caller-role", role, "set-board-stall-threshold", *argv])

    def apply(self, *argv: str) -> list:
        """The Director's preview, then its apply at the revision the preview showed."""
        preview = self.command("director", *argv)
        return self.command("director", *argv, "--apply", "--expected-revision", str(preview[1].get("revision")))

    def close(self) -> None:
        self.unix.shutdown()
        self.unix.server_close()


def director_command(cluster) -> dict:
    """The Director's command from its registered pane over the board's socket, and everyone else's attempts."""
    from board_stall_test import Stall, example
    from router_role_board_test import Board
    from scripts.ticket_board import ui_delegation

    board = Board(cluster, "delta", example())
    panes = Panes(board, cluster, "delta")
    register, pane = panes.register, panes.command

    def now() -> list:
        current = board.app.workflow_document()
        declared = current["document"].get("board_stall")
        return [current["revision"], declared.get("threshold_seconds") if isinstance(declared, dict) else declared]

    seen: dict = {}
    try:
        stall = Stall(board)
        register("director")
        seen["start"] = now()
        seen["preview"] = pane("director", "--seconds", "1200")
        seen["after_preview"] = now()
        revision = seen["start"][0]
        seen["apply_without"] = pane("director", "--seconds", "1200", "--apply")
        seen["apply_moved"] = pane("director", "--seconds", "1200", "--apply", "--expected-revision", str(revision - 1))
        seen["apply"] = pane("director", "--seconds", "1200", "--apply", "--expected-revision", str(revision))
        seen["after_apply"] = now()
        seen["same"] = pane("director", "--seconds", "1200")
        seen["refused"] = {}
        for value in ("299", "86401", "0", "-5"):
            current = now()
            seen["refused"][value] = [pane("director", "--seconds", value, "--apply", "--expected-revision",
                                           str(current[0])), now() == current]
        seen["malformed_cli"] = [pane("director", "--seconds", "soon"), pane("director", "--seconds", "1.5"),
                                 pane("director"), pane("director", "--seconds", "600", "--default")]
        seen["malformed_doc"] = {}
        for name, value in (("text", "3600"), ("fraction", 3600.5), ("boolean", True), ("missing", None),
                            ("extra", 600), ("not_an_object", 600)):
            current = now()
            document = declaring(example(), 600)
            document["board_stall"] = {} if value is None else {"threshold_seconds": value}
            if name == "extra":
                document["board_stall"]["also"] = "quiet"
            if name == "not_an_object":
                document["board_stall"] = 600
            try:
                board.app.apply_workflow(document, expected_revision=current[0], dry_run=False, caller_role="director")
                seen["malformed_doc"][name] = ["accepted", now()]
            except Exception as exc:  # the refusal's own words, whatever raised it
                seen["malformed_doc"][name] = [str(exc)[:200], now() == current]
        for bound in ("300", "86400"):
            seen[f"bound_{bound}"] = [pane("director", "--seconds", bound, "--apply", "--expected-revision",
                                           str(now()[0])), now()]
        seen["status_set"] = {k: stall.state().get(k) for k in ("threshold_seconds", "threshold_source")}
        stall.evaluate(0)  # a pass with nothing runnable records the listener's default beside it
        seen["status_idle"] = {k: stall.state().get(k) for k in ("threshold_seconds", "default_threshold_seconds")}
        seen["preview_refused"] = [pane("director", "--seconds", "299"), now()]
        seen["default"] = [pane("director", "--default", "--apply", "--expected-revision", str(now()[0])), now()]
        seen["status_default"] = {k: stall.state().get(k) for k in ("threshold_seconds", "threshold_source")}
        # Everyone else.
        seen["roles"] = {}
        for role in ("app", "ops", "audit"):
            register(role)
            current = now()
            seen["roles"][role] = [pane(role, "--seconds", "600", "--apply", "--expected-revision", str(current[0])),
                                   now() == current]
        seen["service_roles"] = {}
        for role in ("app", "user", "audit"):
            try:
                board.app.apply_workflow(declaring(example(), 600), expected_revision=now()[0], dry_run=False,
                                         caller_role=role)
                seen["service_roles"][role] = "accepted"
            except PermissionError as exc:
                seen["service_roles"][role] = str(exc)
        ui_delegation.TRUST_HEADER_ROLES = False  # as the board runs: over HTTP a request acts as the User
        try:
            seen["http"] = cli(["--board-url", board.base, "--write-token", board.server.write_token,
                                "--caller-role", "director", "set-board-stall-threshold", "--seconds", "600",
                                "--apply", "--expected-revision", str(now()[0])])
        finally:
            ui_delegation.TRUST_HEADER_ROLES = True
        seen["final"] = now()
    finally:
        panes.close()
        board.close()
    return seen


def reprovision(cluster) -> dict:
    """Root's provisioning packet run again, from root's record as it was before the Director's change: the workflow
    phase's own SQL, rendered by provision_workflow_sql and run as the packet runs it."""
    from board_stall_test import Stall, example
    from router_role_board_test import Board, build_board, fixture
    from scripts.ticket_board.project_provision import build_plan, render_workflow_sql
    from scripts.ticket_board.workflow_config import validate

    def packet(admin: str, record: dict, *, as_before: bool = False) -> str:
        sql = render_workflow_sql(build_plan(project="pgu", owner_user="pgu-agent", workflow=record))
        if as_before:  # the previous candidate's packet: root's record applied as it is
            sql = sql.replace("ticket_board.provisioned_workflow(", "(")
        path = cluster.root / "pgu-workflow.sql"
        path.write_text(sql)
        done = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", admin, "-f", str(path)],
                              capture_output=True, text=True)
        return "ok" if done.returncode == 0 else done.stderr.strip()[-300:]

    record = validate(example(), project="pgu")  # root's record, before the Director's change
    seen: dict = {"rendered": "ticket_board.provisioned_workflow('" in render_workflow_sql(
        build_plan(project="pgu", owner_user="pgu-agent", workflow=record))}
    board = Board(cluster, "theta", record)
    panes = Panes(board, cluster, "theta")
    declared = lambda: board.app.workflow_document()["document"].get("board_stall")  # noqa: E731
    rest = lambda: {k: v for k, v in board.app.workflow_document()["document"].items() if k != "board_stall"}  # noqa: E731
    try:
        stall = Stall(board)
        panes.register("director")
        seen["set"] = [panes.apply("--seconds", "5400")[0], declared()]
        seen["before_packet"] = [packet(board.admin, record, as_before=True), declared()]
        seen["set_again"] = [panes.apply("--seconds", "5400")[0], declared()]
        seen["packet"] = [packet(board.admin, record), declared(), rest() == record]
        seen["status"] = {k: stall.state().get(k) for k in ("threshold_seconds", "threshold_source")}
        stall.ticket("in_progress", "app")
        stall.evaluate(0)
        seen["alerts"] = [(stall.evaluate(m), stall.digests())[1] for m in (31, 89, 91)]
        stale = validate(declaring(example(), 900), project="pgu")  # root's record with an older value of its own
        seen["stale_record"] = [packet(board.admin, stale), declared()]
        seen["cleared"] = [panes.apply("--default")[0], declared()]
        seen["cleared_packet"] = [packet(board.admin, stale), declared(),
                                  {k: stall.state().get(k) for k in ("threshold_seconds", "threshold_source")}]
    finally:
        panes.close()
        board.close()
    # A board running no workflow yet: provisioning seeds root's record exactly, threshold and all.
    fresh = build_board(cluster, "iota")
    seen["fresh"] = [packet(fresh, validate(declaring(example(), 3600), project="pgu")),
                     json.loads(fixture.psql(fresh, "SELECT coalesce((ticket_board.declared_workflow()->'board_stall')"
                                                    "::text, 'null');").strip())]
    return seen


def lifecycle(cluster, before: Path | None) -> dict:
    """Restarts, rebuilt documents, the busy-pane escalation, and (with `before`) main's board upgraded."""
    import psycopg
    from board_stall_test import Stall, example
    from router_role_board_test import Board, fixture
    from scripts import role_runtime, workflow_manage
    from scripts.ticket_board import board_stall
    from scripts.ticket_board.notify_listener import TicketBoardNotifyListener
    from scripts.ticket_board.workflow_config import validate

    seen: dict = {}
    board = Board(cluster, "epsilon", declaring(example(), 2400))
    try:
        stall = Stall(board)
        stall.ticket("in_progress", "ops")
        # A board restart: a new app and server over the same database. A listener restart: a new listener.
        app = fixture.TicketBoardApp(cluster.root / "eps2-frames", cluster.root / "eps2-assets", project="pgu",
                                     ticket_prefix="PGU", database_url=board.service)
        seen["restarted_board"] = (app.workflow_configuration() or {}).get("board_stall")
        listener = TicketBoardNotifyListener(conninfo=board.listener_url, sender=lambda *_a: None,
                                             activity_gate=lambda _t: False, target_exists=lambda _t: True,
                                             submission_witness=lambda *_a: False, poll_seconds=0, project="pgu")
        with psycopg.connect(board.listener_url, autocommit=True) as conn:
            listener.refresh_workflow(conn)
            board_stall.run(listener, conn)
        seen["restarted_listener"] = {k: stall.state().get(k) for k in
                                      ("threshold_seconds", "threshold_source", "evaluated_threshold_seconds",
                                       "default_threshold_seconds")}
        # Rebuilt from the live document, as a runtime switch and a role-prompt edit rebuild it.
        live = board.app.workflow_document()
        switched = validate(role_runtime.document_with_runtime(live["document"], role="ops", runtime="codex"))
        board.app.apply_workflow(switched, expected_revision=live["revision"], dry_run=False, caller_role="director")
        live = board.app.workflow_document()
        args = argparse.Namespace(operation="set-role-prompt", role="app", prompt="Keep the suite bounded.",
                                  prompt_file=None)
        prompted = workflow_manage._document_with_role_prompt(live, args, argparse.ArgumentParser())
        board.app.apply_workflow(prompted, expected_revision=live["revision"], dry_run=False, caller_role="director")
        seen["rebuilt"] = (board.app.workflow_document()["document"].get("board_stall"),
                           next(r["runtime"] for r in board.app.workflow_document()["document"]["roles"]
                                if r["name"] == "ops"))
    finally:
        board.close()
    seen["busy"] = busy_pane(cluster)
    if before is not None:
        upgraded = Board(cluster, "zeta", declaring(example(), 3600), before=before)
        try:
            stall = Stall(upgraded)
            stall.ticket("in_progress", "app")
            stall.evaluate(0)
            seen["upgraded"] = [(stall.evaluate(m), stall.digests())[1] for m in (31, 59, 61)]
            seen["upgraded_status"] = {k: stall.state().get(k) for k in ("threshold_seconds", "threshold_source")}
        finally:
            upgraded.close()
    return seen


def busy_pane(cluster) -> dict:
    """The role stall escalation of a busy pane uses the listener's own threshold, whatever the project declares:
    a pane busy 1900 seconds is escalated under the listener's 30 minutes on a project that declares an hour."""
    import logging
    import time
    from datetime import datetime, timedelta
    from types import SimpleNamespace

    import psycopg
    from board_stall_test import Stall, example
    from router_role_board_test import Board
    from scripts.ticket_board import stall_episode

    board = Board(cluster, "eta", declaring(example(), 3600))
    try:
        stall = Stall(board)
        base = datetime.fromisoformat(board.sql("SELECT (date_trunc('second', now()) - interval '5000 seconds')::text;"))
        at = lambda offset: (base + timedelta(seconds=offset)).isoformat()  # noqa: E731
        ticket = stall.ticket("analysis", "director")
        board.act(ticket, "route", "director", state="in_progress", assignee="app")
        board.sql(f"DELETE FROM ticket_board.ticket_notification_queue WHERE ticket_id = '{ticket}';")
        board.sql(f"SELECT ticket_board.record_notification_trace('{ticket}', 9001, 'app', 'transition', 'send');")
        board.sql("SET session_replication_role = replica; "
                  f"UPDATE ticket_board.ticket_notification_state SET current_assignment_at = '{at(3100)}', "
                  f"entered_current_state_at = '{at(3100)}' WHERE ticket_id = '{ticket}'; "
                  f"UPDATE ticket_board.notification_trace SET ts = '{at(3100)}' WHERE ticket_id = '{ticket}'; "
                  f"UPDATE ticket_board.ticket_comments SET ts = '{at(3100)}' WHERE ticket_id = '{ticket}'; "
                  f"UPDATE ticket_board.board_progress_events SET at = '{at(3100)}' WHERE ticket_id = '{ticket}'; "
                  # The pane registered before the hand-over, as a running pane did: a registration is progress.
                  f"UPDATE ticket_board.role_runtime_assignments SET registered_at = '{at(0)}' WHERE role = 'app'; "
                  "SET session_replication_role = origin;")
        stall.evaluate(0)  # the board stall pass records this project's hour
        recorded = board.sql("SELECT threshold_seconds FROM ticket_board.board_stall_state WHERE id;")

        class BusyGate:
            idle_working_timer_sample_delay_seconds = 0

            def __init__(self) -> None:
                self.state_store = self
                self.role_targets = {"app": "pgu-app:0.0"}

            def read(self, _target):
                return SimpleNamespace(state="busy", updated_at=base.timestamp() + 3101,
                                       turn_started_at=base.timestamp() + 3101)

            def gate(self, _target) -> bool:
                return True

            def wall_time(self) -> float:
                return time.time()

            def _working_timer_trace(self, *_a, **_k):
                return None

            def _open_prompt_trace(self, *_a, **_k):
                return None

            def cursor_position_runner(self, *_a, **_k):
                raise OSError("no tmux here")

            capture_pane_runner = cursor_position_runner

        pane = SimpleNamespace(activity_gate=BusyGate().gate, eligibility=SimpleNamespace(),
                               unresolved_turn_grace_seconds=600, logger=logging.getLogger("syrd724"))
        escalated = []
        for _ in range(2):  # past the listener's threshold at once; a second pass adds nothing
            with psycopg.connect(board.listener_url, autocommit=True) as conn:
                escalated.append(stall_episode.run(pane, conn))
        payload = board.sql(f"SELECT payload->>'threshold_seconds' FROM ticket_board.ticket_notification_queue "
                            f"WHERE ticket_id = '{ticket}' AND payload->>'kind' = 'stall_escalation' LIMIT 1;")
        return {"recorded": recorded, "escalated": escalated, "payload_threshold": payload}
    finally:
        board.close()


def scenario(root: Path, before: Path | None) -> dict:
    for extra in (str(root), str(root / "tests")):
        if extra not in sys.path:
            sys.path.insert(0, extra)
    from router_role_board_test import fixture, temporary_cluster  # noqa: F401  (isolates the tmux bus first)

    assert Path(fixture.__file__).resolve().is_relative_to(root.resolve()), fixture.__file__
    with temporary_cluster(prefix="s724", shutdown="immediate") as cluster:
        if not (root / "scripts/ticket_board/migrations" / MIGRATION.name).is_file():
            return {"main": main_scenario(cluster)}
        return {"concurrent": concurrent(cluster), "command": director_command(cluster),
                "lifecycle": lifecycle(cluster, before), "reprovision": reprovision(cluster),
                "race": reprovision_race(cluster)}


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def child(root: Path, before: Path | None) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(PANE_ENV_PREFIXES)}
    done = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), str(before or "")],
                          capture_output=True, text=True, timeout=900, stdin=subprocess.DEVNULL,
                          env={**env, "PYTHONDONTWRITEBYTECODE": "1"})
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-4000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def before_checks(got: dict) -> None:
    m = got["main"]
    check(m["validate"] == "unknown workflow configuration field", f"main: no workflow may declare one: {m['validate']}")
    check(m["command"][0] == 2 and "invalid choice: 'set-board-stall-threshold'" in m["command"][2],
          f"main: there is no command: {m['command']}")
    check(m["at_31"] == [True, 1], f"main: a project that needs an hour is alerted at 31 minutes: {m['at_31']}")
    check(m["status"] == {"threshold_seconds": 1800, "threshold_source": None},
          f"main: one threshold, and nothing says where it comes from: {m['status']}")


def concurrent_checks(got: dict) -> None:
    c = got["concurrent"]
    t = c["timeline"]
    check(t["14"] == {"alpha": 0, "beta": 0, "gamma": 0} and t["16"] == {"alpha": 0, "beta": 0, "gamma": 1},
          f"15 minutes declared: silent at 14, alerted at 16: {t['14']} {t['16']}")
    check(t["29"]["beta"] == 0 and t["31"] == {"alpha": 0, "beta": 1, "gamma": 1},
          f"nothing declared: the listener's 30 minutes: {t['29']} {t['31']}")
    check(t["59"]["alpha"] == 0 and t["61"] == {"alpha": 1, "beta": 1, "gamma": 1},
          f"an hour declared: silent at 59, alerted at 61, and one alert each: {t['59']} {t['61']}")
    check(c["status"] == {"alpha": {"threshold_seconds": 3600, "threshold_source": "workflow",
                                    "default_threshold_seconds": 1800},
                          "beta": {"threshold_seconds": 1800, "threshold_source": "default",
                                   "default_threshold_seconds": 1800},
                          "gamma": {"threshold_seconds": 900, "threshold_source": "workflow",
                                    "default_threshold_seconds": 1800}},
          f"the status says each project's threshold and where it comes from: {c['status']}")
    check(c["episodes"] == {"alpha": "3600", "beta": "1800", "gamma": "900"},
          f"each alert records the threshold it fired under: {c['episodes']}")
    check(c["read"]["alpha"].endswith("threshold 3600s (this project's workflow; the default is 1800s)")
          and c["read"]["beta"].endswith("threshold 1800s (the default)"),
          f"ticket-board-read stall says so: {c['read']}")
    check(c["gamma_raised"] == [1, False] and c["gamma_lowered"] == [1, False],
          f"an alert already sent is not sent again, raised or lowered, and stays open: {c['gamma_raised']} "
          f"{c['gamma_lowered']}")
    check(c["gamma_progress"] == [1, "progress"] and c["gamma_after"] == [1, 2],
          f"progress closes it as before, and the next clock runs under the new 5 minutes: {c['gamma_progress']} "
          f"{c['gamma_after']}")
    check(c["beta_raised"] == [1, 1, 1, 2],
          f"raised to an hour 20 minutes in, before any alert: nothing new at 31 or 59, one at 61: {c['beta_raised']}")
    check(c["alpha_before"] == [1] and c["alpha_lowered"] == [2, 2, 2, 2],
          f"lowered to 30 minutes 40 minutes in: one alert at the next pass, and only one: {c['alpha_lowered']}")
    check(all(v == {"threshold_seconds": 1800, "threshold_source": "default"} for v in c["corrupt"].values()),
          f"a stored value out of bounds, or not a whole number, is read as the default: {c['corrupt']}")
    check(c["fires_after"] == {"alpha": "3600", "beta": "1800", "gamma": "900"} and c["beta_fires_after_change"] == "3600",
          f"and the status says when each would fire, by its own threshold, as soon as it changes: {c['fires_after']} "
          f"{c['beta_fires_after_change']}")
    check(c["listener_default"] == [0, 1] and c["listener_status"] == {
              "threshold_seconds": 1200, "threshold_source": "default", "default_threshold_seconds": 1200},
          f"a project that declares none keeps the listener's own setting, here 20 minutes: {c['listener_default']} "
          f"{c['listener_status']}")


def command_checks(got: dict) -> None:
    d = got["command"]
    revision = d["start"][0]
    preview = d["preview"]
    check(preview[0] == 0 and preview[1].get("applied") is False
          and preview[1].get("before") == "default" and preview[1].get("after") == 1200
          and preview[1].get("apply_with") == f"ticket-board-write set-board-stall-threshold --seconds 1200 --apply "
                                              f"--expected-revision {revision}"
          and d["after_preview"] == d["start"] == [revision, None],
          f"the preview shows the change, validated by the board, and changes nothing: {preview} {d['after_preview']}")
    check(d["apply_without"][0] == 1 and "--apply needs --expected-revision" in d["apply_without"][2]
          and d["apply_moved"][0] == 1 and f"at revision {revision}, not the {revision - 1}" in d["apply_moved"][2],
          f"an apply needs the reviewed revision: {d['apply_without']} {d['apply_moved']}")
    check(d["apply"][0] == 0 and d["apply"][1].get("applied") is True and d["after_apply"][1] == 1200
          and d["after_apply"][0] == d["apply"][1].get("revision_after") > revision,
          f"the Director applies it: {d['apply']} {d['after_apply']}")
    check(d["same"][0] == 0 and d["same"][1].get("unchanged") is True, f"the same value again is no change: {d['same']}")
    for value, (answer, kept) in d["refused"].items():
        check(answer[0] == 1 and "board_stall threshold_seconds must be a whole number of seconds from 300 "
              "(5 minutes) to 86400 (24 hours)" in answer[2] and kept,
              f"{value} is refused, saying the bounds, and the prior setting stays: {answer}")
    soon, fraction, neither, both = d["malformed_cli"]
    check(soon[0] == 2 and fraction[0] == 2 and neither[0] == 2 and both[0] == 2,
          f"not a whole number, or not exactly one of --seconds and --default: {d['malformed_cli']}")
    for name, (answer, kept) in d["malformed_doc"].items():
        check(answer != "accepted" and "board_stall" in answer and kept,
              f"a document declaring it as {name} is refused, the prior setting kept: {answer}")
    check(d["bound_300"][0][0] == 0 and d["bound_300"][1][1] == 300 and d["bound_86400"][0][0] == 0
          and d["bound_86400"][1][1] == 86400, f"both bounds are accepted: {d['bound_300']} {d['bound_86400']}")
    check(d["status_set"] == {"threshold_seconds": 86400, "threshold_source": "workflow"},
          f"and in force at once, before any evaluation: {d['status_set']}")
    check(d["status_idle"] == {"threshold_seconds": 86400, "default_threshold_seconds": 1800},
          f"a pass with nothing runnable records the default beside the project's own: {d['status_idle']}")
    check(d["preview_refused"][0][0] == 1 and "board_stall threshold_seconds must be" in d["preview_refused"][0][2]
          and d["preview_refused"][1] == d["bound_86400"][1],
          f"even a preview of a refused value is refused by the board's own validation: {d['preview_refused']}")
    check(d["default"][0][0] == 0 and d["default"][1][1] is None
          and d["status_default"] == {"threshold_seconds": 1800, "threshold_source": "default"},
          f"--default removes it, and the default is in force: {d['default']} {d['status_default']}")
    for role, (answer, kept) in d["roles"].items():
        check(answer[0] == 1 and "only director may configure workflow" in answer[2] and kept,
              f"{role}'s own pane is refused, changing nothing: {answer}")
    check(all(v == "only director may configure workflow" for v in d["service_roles"].values()),
          f"the board refuses every other role: {d['service_roles']}")
    check(d["http"][0] == 1 and "over HTTP a request acts as the User, not director" in d["http"][2],
          f"over HTTP, write token and all, nobody is the Director: {d['http']}")
    check(d["final"] == d["default"][1], f"nothing they tried changed it: {d['final']}")


def lifecycle_checks(got: dict, upgraded: bool) -> None:
    life = got["lifecycle"]
    check(life["restarted_board"] == {"threshold_seconds": 2400}, f"a restarted board reads it: {life}")
    check(life["restarted_listener"] == {"threshold_seconds": 2400, "threshold_source": "workflow",
                                         "evaluated_threshold_seconds": 2400, "default_threshold_seconds": 1800},
          f"a restarted listener's first pass evaluates under it: {life['restarted_listener']}")
    check(life["rebuilt"] == [{"threshold_seconds": 2400}, "codex"],
          f"a runtime switch and a role-prompt edit rebuild the live document and keep it: {life['rebuilt']}")
    busy = life["busy"]
    check(busy["recorded"] == "3600" and busy["escalated"] == [1, 0] and busy["payload_threshold"] == "1800",
          f"a busy pane is escalated under the listener's 30 minutes though the project declares an hour: {busy}")
    if upgraded:
        check(life["upgraded"] == [0, 0, 1] and life["upgraded_status"] == {"threshold_seconds": 3600,
                                                                            "threshold_source": "workflow"},
              f"main's board, upgraded, alerts at its declared hour: {life['upgraded']} {life['upgraded_status']}")


def reprovision_race(cluster) -> dict:
    """Root's packet and a Director apply at the same moment, in both orders, through their real paths: the
    Director's `app.apply_workflow` and the packet's rendered workflow SQL. A third connection holds the workflow lock
    so each queues in a known order; `pg_stat_activity` confirms each is waiting before the next starts."""
    import time

    import psycopg
    from board_stall_test import example
    from router_role_board_test import Board
    from scripts.ticket_board.project_provision import build_plan, render_workflow_sql
    from scripts.ticket_board.workflow_config import validate

    record = validate(example(), project="pgu")  # root's record: no threshold of its own
    board = Board(cluster, "kappa", record)
    panes = Panes(board, cluster, "kappa")
    lock = "SELECT pg_advisory_xact_lock(hashtext('ticket_board.workflow_configuration'))"
    declared = lambda: (board.app.workflow_document()["document"].get("board_stall") or {}).get("threshold_seconds")  # noqa: E731

    def waiting(n: int) -> bool:
        for _ in range(200):
            if board.sql("SELECT count(*) FROM pg_stat_activity WHERE wait_event_type = 'Lock' AND wait_event = "
                         "'advisory' AND datname = current_database();") == str(n):
                return True
            time.sleep(0.05)
        return False

    def packet(out: dict, without_lock: bool) -> None:
        sql = render_workflow_sql(build_plan(project="pgu", owner_user="pgu-agent", workflow=record))
        if without_lock:  # the previous candidate's packet: no lock before the statement that reads the board
            sql = sql.replace(lock + ";\n", "")
        path = cluster.root / f"kappa-{len(out)}-{time.monotonic_ns()}.sql"
        path.write_text(sql)
        done = subprocess.run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", board.admin, "-f", str(path)],
                              capture_output=True, text=True)
        out["packet"] = "ok" if done.returncode == 0 else done.stderr.strip()[-300:]

    def director(out: dict, seconds: int, revision: int) -> None:
        document = dict(board.app.workflow_document()["document"], board_stall={"threshold_seconds": seconds})
        try:
            board.app.apply_workflow(document, expected_revision=revision, dry_run=False, caller_role="director")
            out["director"] = "ok"
        except Exception as exc:  # an answer, not a crash
            out["director"] = str(exc)[:300]

    def race(order: str, *, without_lock: bool = False) -> dict:
        panes.apply("--seconds", "5400")
        out: dict = {"before": declared()}
        revision = board.app.workflow_document()["revision"]
        holder = psycopg.connect(board.admin)
        holder.execute(lock)
        threads = {"director": threading.Thread(target=director, args=(out, 7200, revision)),
                   "packet": threading.Thread(target=packet, args=(out, without_lock))}
        first, second = ("director", "packet") if order == "director_first" else ("packet", "director")
        threads[first].start()
        out["first_waited"] = waiting(1)
        threads[second].start()
        out["second_waited"] = waiting(2)
        holder.commit()
        holder.close()
        for thread in threads.values():
            thread.join(60)
        out["after"] = declared()
        return out

    try:
        panes.register("director")
        seen = {"director_first": race("director_first"), "packet_first": race("packet_first"),
                "director_first_unlocked": race("director_first", without_lock=True)}
    finally:
        panes.close()
        board.close()
    return seen


def reprovision_checks(got: dict) -> None:
    r = got["reprovision"]
    check(r["rendered"], "root's packet applies its record through provisioned_workflow")
    check(r["set"] == [0, {"threshold_seconds": 5400}], f"the Director's command sets 90 minutes: {r['set']}")
    check(r["before_packet"] == ["ok", None],
          f"the previous candidate's packet, root's record applied as it is, drops it: {r['before_packet']}")
    check(r["set_again"] == [0, {"threshold_seconds": 5400}] and r["packet"] == ["ok", {"threshold_seconds": 5400}, True],
          f"the provisioning packet run again from root's pre-change record keeps the Director's value, and applies "
          f"root's record for everything else: {r['packet']}")
    check(r["status"] == {"threshold_seconds": 5400, "threshold_source": "workflow"} and r["alerts"] == [0, 0, 1],
          f"and it is the threshold in force: silent at 31 and 89 minutes, alerted at 91: {r['status']} {r['alerts']}")
    check(r["stale_record"] == ["ok", {"threshold_seconds": 5400}],
          f"a record carrying an older value of its own does not bring it back: {r['stale_record']}")
    check(r["cleared"] == [0, None] and r["cleared_packet"] == ["ok", None, {"threshold_seconds": 1800,
                                                                              "threshold_source": "default"}],
          f"cleared by the Director, it stays the default through the packet: {r['cleared_packet']}")
    check(r["fresh"] == ["ok", {"threshold_seconds": 3600}],
          f"a board running no workflow yet is seeded with root's record exactly: {r['fresh']}")
    race = got["race"]
    for order in ("director_first", "packet_first"):
        w = race[order]
        check(w["before"] == 5400 and w["first_waited"] and w["second_waited"] and w["director"] == "ok"
              and w["packet"] == "ok" and w["after"] == 7200,
              f"{order}: a Director apply of 2 hours and root's packet, queued on the workflow lock, both complete, and "
              f"the Director's 2 hours stands: {w}")
    w = race["director_first_unlocked"]
    check(w["first_waited"] and w["second_waited"] and w["director"] == "ok" and w["packet"] == "ok"
          and w["after"] == 5400,
          f"the previous candidate's packet, reading the board before it waited for the lock, wrote back the 90 minutes "
          f"it read over the Director's 2 hours (Audit's reproduction): {w}")


def mirrored() -> None:
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    body = MIGRATION.read_text().split("BEGIN;\n\n", 1)[1].rsplit("\nCOMMIT;", 1)[0]
    # The last definition of each function is the migration's, byte for byte; a later ticket's may follow (SYRD-725).
    heads = [f"CREATE OR REPLACE FUNCTION ticket_board.{name}(" for name in (
        "declared_board_stall_seconds", "note_board_stall", "board_stall_status", "provisioned_workflow")]
    at = schema.rfind(body)
    check(at >= 0 and all(at <= schema.rfind(h) < at + len(body) for h in heads),
          "schema.sql's last copies are the migration's, byte for byte")
    readers = subprocess.run(["git", "-C", str(ROOT), "grep", "--untracked", "-l", "-e", "->'board_stall'", "--", "scripts"],
                             capture_output=True, text=True).stdout.split()
    check(sorted(readers) == [str(MIGRATION.relative_to(ROOT)), "scripts/ticket_board/schema.sql"],
          f"only the board stall evaluator's own function reads the declared value: {readers}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3]) if sys.argv[3] else None)))
        return 0
    mirrored()
    if os.environ.get("SYRD724_MUTATION_SKIP_BEFORE") == "1":  # a mutant changes this tree only
        got = child(ROOT, None)
        concurrent_checks(got)
        command_checks(got)
        lifecycle_checks(got, upgraded=False)
        reprovision_checks(got)
        print(f"board_stall_threshold_test: {CHECKS} checks ok (mutation run: main's replay and the upgrade skipped)")
        return 0
    with tempfile.TemporaryDirectory(prefix="syrd724-before.", dir="/tmp") as tmp:
        before = tree_at(BEFORE, Path(tmp) / "before")
        before_checks(child(before, None))
        got = child(ROOT, before)
    concurrent_checks(got)
    command_checks(got)
    lifecycle_checks(got, upgraded=True)
    reprovision_checks(got)
    print(f"board_stall_threshold_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
