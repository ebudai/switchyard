#!/usr/bin/env python3
"""A worker's serial slot is reported as the routing gate holds it, apart from its readiness.

MEFP, 2026-09-29: MEFP-25 and MEFP-31 sat at User UAT, assigned to the user,
holding luna-1 and luna-2. `switchyard worker-pool mefp status` said both were
`ready, running` with nothing held, and the Director routed MEFP-12 and MEFP-13
to them; the board's serial gate diverted both, naming MEFP-25 and MEFP-31
(SYRD-476). Status read each worker's queue -- the tickets assigned to it --
and a ticket at UAT is assigned to the user. Which ticket holds a worker was
computed only inside the gate.

Every board here is built the way provisioning builds one -- the companion
roles, schema.sql, the real `ticket-board-migrate`, rbac.sql -- with the
shipped document expanded by the real pool expansion, so the pool's workers
are real implementers. A ticket is taken to User UAT by real transitions, the
real board server serves it, and the real `switchyard worker-pool status`
reads it over HTTP through its shipped reader. The gate is then asked to route
a second ticket to the same worker, and what it does is compared with what
status said -- for a UAT stage declared as a review stage (it holds), as a
system stage (it releases), and as a system stage under "lifecycle" (it holds).
"""

from __future__ import annotations

import copy
import http.server
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
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import lifecycle_reservation_test as lr  # noqa: E402
import ticket_board_write_api_test as t  # noqa: E402
import worker_pool_lifecycle_test as lifecycle  # noqa: E402
from scripts import team_launcher, worker_pool  # noqa: E402
from scripts.ticket_board import board_skill  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

CHECKS = 0
PROJECT = lifecycle.PROJECT
POOL = lifecycle.POOL
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu965_syrd476_serial_reservations.sql"
UAT, NEXT = "PGU-9", "PGU-10"
WORKER = "impl-1"
LIVE_BOARDS = ("127.0.0.1:26623",)  # MEFP's board: the lifecycle fixture's default URL


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def document_for(uat_kind: str, policy: str | None) -> dict:
    """The shipped document, as MEFP runs it, with the pool expanded into it."""
    document = lr.mefp_shaped(policy)
    document["project"] = PROJECT
    for role in document["roles"]:
        if role.get("target"):
            role["target"] = f"{PROJECT}-{role['name']}:0.0"
    for stage in document["stages"]:
        if stage["name"] == "user_review":
            stage["kind"] = uat_kind
    expanded, _changes = worker_pool.expand_pool(document, lifecycle.pool_of(POOL), project=PROJECT)
    return expanded


def tree_before(into: Path) -> Path:
    adding = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H", "--diff-filter=A", "--",
                             str(MIGRATION.relative_to(ROOT))], check=True, capture_output=True, text=True).stdout.split()
    assert adding, "the migration is not committed; clone-based checks see only commits"
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", f"{adding[-1]}^", "scripts/ticket_board",
                              "scripts/ticket-board-migrate"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


class Board:
    """One board built the production way from `tree`, carrying `document`."""

    def __init__(self, cluster, db: str, tree: Path, document: dict) -> None:
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        members = worker_pool.live_members(document, lifecycle.pool_of(POOL))
        t.psql(self.admin, "DO $$ BEGIN\n" + "\n".join(
            f"IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{name}') THEN "
            f"CREATE ROLE \"{name}\" LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION; END IF;"
            for name in ("ticket_board_service", "ticket_board_listener", *members)
        ) + "\nEND $$;")
        try:
            t.create_roles(self.admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(self.admin, (tree / "scripts/ticket_board/schema.sql").read_text())
        runner = subprocess.run(["bash", str(tree / "scripts/ticket-board-migrate")],
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": self.admin},
                                capture_output=True, text=True)
        assert runner.returncode == 0, runner.stderr
        self.applied = runner.stderr + runner.stdout
        t.psql(self.admin, (tree / "scripts/ticket_board/rbac.sql").read_text())
        self.app = t.TicketBoardApp(
            cluster.root / f"frames-{db}", cluster.root / f"assets-{db}", project=PROJECT, ticket_prefix="PGU",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
        )
        self.app._resolve_known_commit = lambda value: (lr.COMMIT, [])
        with self.app._pg_connect() as conn:
            self.app._pg_set_caller_role(conn, "director")
            conn.execute(f"SELECT set_config('ticket_board.project','{PROJECT}',false)")
            conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(document),))
            conn.commit()
        t.seed_postgres_ticket(self.admin, UAT, title="Bug A", state="in_progress", assignee=WORKER,
                               needs_user_signoff=True)
        t.seed_postgres_ticket(self.admin, NEXT, title="Bug B", state="analysis", assignee="director",
                               needs_user_signoff=True)

    def act(self, ticket: str, action: str, payload: dict, role: str) -> dict:
        return self.app.perform_workflow_action(ticket, action, payload, caller_role=role)

    def to_uat(self) -> None:
        self.act(UAT, "submit_to_audit", {"commit_hash": lr.COMMIT}, WORKER)
        self.act(UAT, "audit_sign_off", {"text": "approved"}, "audit")
        uat = self.act(UAT, "director_dat_sign_off", {"text": "ready for UAT"}, "director")
        check((uat["state"], uat["assignee"]) == ("user_review", "user"), f"{UAT} is at User UAT: {uat}")

    def route_next(self) -> dict:
        """What the gate does with a second ticket for the same worker."""
        return self.act(NEXT, "route", {"target": "in_progress", "assignee": WORKER}, "director")


class Served:
    """The real board server over a board, on a port of its own."""

    def __init__(self, app) -> None:
        self.server = t.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=t.QuietNotifier())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class OlderBoard(http.server.BaseHTTPRequestHandler):
    """A board older than /api/reservations: it serves /api/board and nothing about slots."""

    board: dict = {}
    asked: list[str] = []

    def do_GET(self):  # noqa: N802
        OlderBoard.asked.append(self.path)
        if self.path == "/api/board":
            body, status = json.dumps(OlderBoard.board).encode(), 200
        else:
            body, status = b"not found", 404
        self.send_response(status)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


def _probes(args, **_kwargs):
    """The read-only probes readiness makes; no session is live and nothing is started."""
    command = [str(part) for part in args]
    if command[-2:-1] == ["-c"] and command[-1].startswith("command -v "):
        return subprocess.CompletedProcess(command, 0, stdout="/usr/bin/hermes\n")
    if "config" in command and "check" in command:
        return subprocess.CompletedProcess(command, 0, stdout="\N{CHECK MARK} OPENROUTER_API_KEY\n")
    if command[:2] == ["tmux", "has-session"]:
        return subprocess.CompletedProcess(command, 1)
    raise AssertionError(f"status reached past a read-only probe: {command}")


def status_from(tmp: Path, board_url: str, document: dict) -> list[str]:
    """The real `switchyard worker-pool status`, reading the board at `board_url` itself."""
    config_dir = tmp / f"tenant-{len(list(tmp.glob('tenant-*')))}"
    config_dir.mkdir()
    _config, config_path = lifecycle.config_with_pool(config_dir, pool=POOL, workers=[WORKER, "impl-2"])
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["board_url"] = board_url
    config_path.write_text(json.dumps(raw), encoding="utf-8")
    home = config_dir / "home"
    board_skill.install_board_skill(home=home, source=board_skill.default_source(ROOT), source_commit="syrd476")
    loaded = team_launcher.load_project_config(PROJECT, config_path)
    check(loaded.board_url == board_url and not any(live in loaded.board_url for live in LIVE_BOARDS),
          f"status would read a live board: {loaded.board_url}")
    said: list[str] = []
    original = team_launcher._owner_home_for_auth
    team_launcher._owner_home_for_auth = lambda _owner: home
    try:
        code = team_launcher.switchyard_worker_pool_command(
            PROJECT, action="status", member="",
            config_dir=config_dir, registry_dir=config_dir / "registry",
            board_reader=lambda _config: {"document": document},
            runner=_probes, print_func=said.append,
        )
    finally:
        team_launcher._owner_home_for_auth = original
    check(code == 0, said)
    return said


def _line(said: list[str], member: str = WORKER) -> str:
    return next((line for line in said if line.strip().startswith(f"{member} (")), "\n".join(said))


def run_policies(cluster, tmp: Path) -> None:
    cases = (
        # (label, UAT stage kind, policy, the gate holds the worker at UAT)
        ("UAT declared a review stage (the shipped document)", "review", None, True),
        ("UAT declared a system stage (MEFP's revision 57)", "system", None, False),
        ("UAT a system stage under lifecycle", "system", "lifecycle", True),
    )
    for index, (label, kind, policy, holds) in enumerate(cases):
        document = document_for(kind, policy)
        board = Board(cluster, f"policy{index}", ROOT, document)
        check("apply pgu965_syrd476_serial_reservations.sql" in board.applied, "the runner applied the migration")
        served = Served(board.app)
        try:
            # Before any review: the worker's own implementation ticket holds it.
            said = status_from(tmp, served.url, document)
            check(f"serial slot held by {UAT} (in_progress, assigned to {WORKER})" in _line(said), (label, said))
            board.to_uat()
            said = status_from(tmp, served.url, document)
            line, summary = _line(said), said[0]
            other = _line(said, "impl-2")
        finally:
            served.close()
        check("serial slot free" in other, (label, other))
        # Then ask the gate the question status answered.
        routed = board.route_next()
        if holds:
            check(f"serial slot held by {UAT} (user_review, assigned to user)" in line, (label, line))
            check("1 held by a ticket" in summary, (label, summary))
            check((routed["state"], routed["queued_behind_ticket"]) == ("backlog", UAT), (label, routed))
        else:
            check("serial slot free" in line, (label, line))
            check("0 held by a ticket" in summary, (label, summary))
            check((routed["state"], routed["assignee"], routed["queued_behind_ticket"])
                  == ("in_progress", WORKER, ""), (label, routed))
        # Ready and running are said apart from the slot, never instead of it.
        check("serial slot" in line and ("ready," in line or "not ready," in line), line)


def run_before(cluster, tmp: Path) -> None:
    """The board as it shipped: nothing can read the slot, though the gate holds it."""
    document = document_for("review", None)
    board = Board(cluster, "before", tree_before(tmp / "before"), document)
    board.to_uat()
    try:
        board.app.serial_reservations()
        answered = True
    except Exception:  # noqa: BLE001 - the function it would ask does not exist yet
        answered = False
    check(not answered, "a board from before this change reports reservations")
    check(board.route_next()["queued_behind_ticket"] == UAT, "yet its gate held the worker at UAT all along")

    # Such a board, as status meets it: /api/board and no /api/reservations.
    snapshot = board.app.snapshot()
    OlderBoard.board = json.loads(json.dumps(snapshot, default=str))
    OlderBoard.asked = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), OlderBoard)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        said = status_from(tmp, f"http://127.0.0.1:{server.server_address[1]}", document)
    finally:
        server.shutdown()
        server.server_close()
    check("/api/reservations" in OlderBoard.asked, OlderBoard.asked)
    line = _line(said)
    check("serial slot unknown: this board does not report serial reservations" in line, line)
    # The one reservation fact such a board publishes is kept as evidence.
    check(f"({NEXT} is queued for it behind {UAT})" in line, line)
    check("capacity unknown" in said[0] and "can take a ticket" not in said[0], said[0])
    check("serial slot free" not in "\n".join(said), "an unreported slot must never read as free")


def test_serial_slots_are_what_the_gate_holds() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd476.") as tmp:
        previous = os.environ.get("HOME")
        os.environ["HOME"] = tmp  # nothing here may reach the real home
        try:
            with temporary_cluster(prefix="syrd476.") as cluster:
                run_before(cluster, Path(tmp))
                run_policies(cluster, Path(tmp))
        finally:
            if previous is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = previous


def test_a_slot_the_board_does_not_list_is_not_free() -> None:
    state = worker_pool.WorkerReadiness(role=WORKER, target=f"{PROJECT}-{WORKER}:0.0",
                                        **worker_pool._reservation_of(WORKER, {"impl-2": None}, None))
    check(state.slot() == "serial slot unknown: the board does not count it as an implementer", state.slot())
    check(state.can_take_work is not True, state.can_take_work)
    free = worker_pool.WorkerReadiness(role=WORKER, target="x", session=True,
                                       **worker_pool._reservation_of(WORKER, {WORKER: None}, None))
    check(free.slot() == "serial slot free", free.slot())


def test_the_mefp_line_says_ready_running_and_held() -> None:
    """luna-1 on 2026-09-29: ready and running, and not free to take MEFP-12."""
    live = dict(declared=True, routed=("in_progress",), onboarding=True, skill="present",
                runtime="authenticated", account=True, worktree=True, session=True)
    held = worker_pool.WorkerReadiness(role="luna-1", target="mefp-luna-1:0.0", **live, **worker_pool._reservation_of(
        "luna-1", {"luna-1": {"ticket": "MEFP-25", "state": "user_review", "assignee": "user"}}, None))
    check(held.describe() == "luna-1 (mefp-luna-1:0.0): ready, running; serial slot held by MEFP-25 "
                             "(user_review, assigned to user)", held.describe())
    check(held.can_take_work is False, "a held worker cannot take a ticket")
    free = worker_pool.WorkerReadiness(role="luna-5", target="mefp-luna-5:0.0", **live,
                                       **worker_pool._reservation_of("luna-5", {"luna-5": None}, None))
    check(free.can_take_work is True and free.describe().endswith("ready, running; serial slot free"),
          free.describe())
    unknown = worker_pool.WorkerReadiness(role="luna-1", target="mefp-luna-1:0.0", **live,
                                          **worker_pool._reservation_of("luna-1", None, None))
    check(unknown.can_take_work is None, "an unreported slot is neither free nor held")


def main() -> int:
    for name, case in list(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"serial_reservation_status_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
