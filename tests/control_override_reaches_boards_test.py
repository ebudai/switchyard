#!/usr/bin/env python3
"""SYRD-275: the Director's narrated override works on a board built the way boards are built.

MEFP's Director, recovering MEFP-5 with `override-move`, was refused with
"role director cannot call force_move". SYRD-78 had made that work -- in
schema.sql, with no migration. Production builds every board as schema.sql and
then every pgu* migration, so the newest migration copy of a function is the
one that runs; the suites load schema.sql alone and tested a require_actor no
board had.

So this builds boards exactly as provisioning does -- schema.sql, the real
`ticket-board-migrate` runner over the migrations directory, rbac.sql, then a
declared workflow -- from the tree as it was before this change (`--before`,
the reproduction) and from this tree, and drives the Director's
`override-move` through the real write client and HTTP server.
"""

from __future__ import annotations

import copy
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

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.write_client import TicketBoardWriteClient  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402
from workflow_document_eras import before_relaying  # noqa: E402

CHECKS = 0
MIGRATION = ROOT / "scripts/ticket_board/migrations/pgu963_syrd275_control_override_reaches_boards.sql"
COMMIT = "6d4ee1aa99147e8118f59e637be02b660d62d064"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout


def tree_before(into: Path) -> Path:
    """The board's SQL and migration runner as they stood before this change."""
    adding = git("log", "--format=%H", "--diff-filter=A", "--", str(MIGRATION.relative_to(ROOT))).split()
    assert adding, "the migration is not committed; clone-based checks see only commits"
    archive = subprocess.run(
        ["git", "-C", str(ROOT), "archive", f"{adding[-1]}^", "scripts/ticket_board", "scripts/ticket-board-migrate"],
        check=True, capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


class ProvisionedBoard:
    """schema.sql, then ticket-board-migrate, then rbac.sql -- the provisioning order."""

    def __init__(self, cluster, db: str, tree: Path) -> None:
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        # The provisioning companion creates the writer and listener roles
        # before anything else runs; the pane roles come with them here.
        t.psql(self.admin, """
DO $$ BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_service') THEN
        CREATE ROLE ticket_board_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'ticket_board_listener') THEN
        CREATE ROLE ticket_board_listener LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;
    END IF;
END $$;
""")
        try:
            t.create_roles(self.admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(self.admin, (tree / "scripts/ticket_board/schema.sql").read_text())
        runner = subprocess.run(
            ["bash", str(tree / "scripts/ticket-board-migrate")],
            env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": self.admin},
            capture_output=True, text=True,
        )
        assert runner.returncode == 0, runner.stderr
        self.applied = runner.stderr + runner.stdout
        t.psql(self.admin, (tree / "scripts/ticket_board/rbac.sql").read_text())
        self.app = t.TicketBoardApp(
            cluster.root / f"frames-{db}", cluster.root / f"assets-{db}", project="cerulean", ticket_prefix="PGU",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
        )
        document = copy.deepcopy(before_relaying())
        document["project"] = "cerulean"
        document.setdefault("reassign", {})
        document.setdefault("remove_stages", [])
        with self.app._pg_connect() as conn:
            self.app._pg_set_caller_role(conn, "director")
            conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
            conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(document),))
            conn.commit()
        # MEFP-5's shape: in Director review on a sign-off the Director no
        # longer trusts, and needing to go back to Audit.
        t.seed_postgres_ticket(self.admin, "PGU-5", title="Recover", state="director_review", assignee="director",
                               commit_hash=COMMIT, audit_signoff=True)

    def override_move(self, role: str, state: str, assignee: str) -> tuple[bool, str]:
        """`ticket-board-write override-move`, as `role`, through the real server."""
        server = t.TicketBoardServer(("127.0.0.1", 0), self.app, director_notifier=t.QuietNotifier())
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            client = TicketBoardWriteClient(f"http://127.0.0.1:{server.server_port}", caller_role=role,
                                            write_token=server.write_token)
            try:
                client.override_move("PGU-5", state=state, assignee=assignee, caller_role=role)
            except Exception as exc:  # noqa: BLE001 - the refusal's text is what is checked
                return False, str(exc)
            return True, ""
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def row(self) -> tuple[str, str, bool]:
        ticket = self.app.get_ticket("PGU-5")
        return ticket["state"], ticket["assignee"], ticket["audit_signoff"]


def run_before(cluster, tmp: Path) -> None:
    board = ProvisionedBoard(cluster, "before", tree_before(tmp / "before"))
    check("apply pgu9" in board.applied, f"the real runner applied the migrations: {board.applied[-200:]!r}")
    moved, why = board.override_move("director", "audit", "audit")
    check(not moved and "role director cannot call force_move" in why,
          f"reproduced: the Director's override is refused on a provisioned board: {why!r}")
    check(board.row() == ("director_review", "director", True), f"and nothing moved: {board.row()}")


def run_after(cluster) -> None:
    board = ProvisionedBoard(cluster, "after", ROOT)
    check("apply pgu963_syrd275_control_override_reaches_boards.sql" in board.applied,
          "the runner applied this migration")

    # The ordinary path is still closed: Director review does not route to Audit.
    try:
        board.app.perform_workflow_action("PGU-5", "route", {"target": "audit", "assignee": "audit"},
                                          caller_role="director")
    except Exception as exc:  # noqa: BLE001
        ordinary = str(exc)
    else:
        ordinary = ""
    check("unknown or ambiguous workflow action" in ordinary or "unauthorized configured transition" in ordinary,
          f"the undeclared route is still refused: {ordinary!r}")

    # Nobody but the control role gets the override.
    for role in ("ops", "audit", "main"):
        moved, why = board.override_move(role, "audit", "audit")
        check(not moved and (f"{role} cannot call override_move" in why or f"role {role} cannot call force_move" in why),
              f"{role} is refused: {why!r}")
        check(board.row() == ("director_review", "director", True), f"and nothing moved for {role}: {board.row()}")

    # And the database itself refuses them, whatever the server does.
    service = t.conninfo(cluster.socket_dir, cluster.port, "after", t.SERVICE_ROLE)
    try:
        t.psql(service, "SELECT set_config('ticket_board.caller_role', 'ops', false); "
                        "SELECT ticket_board.force_move('PGU-5', 'audit', 'audit');")
    except AssertionError as exc:
        direct = str(exc)
    else:
        direct = ""
    check("role ops cannot call force_move" in direct, f"the database refuses ops directly: {direct!r}")
    check(board.row() == ("director_review", "director", True), f"and nothing moved: {board.row()}")

    # The Director's narrated override lands -- and grants nothing.
    moved, why = board.override_move("director", "audit", "audit")
    check(moved, f"the Director's override works on a provisioned board: {why!r}")
    check(board.row()[:2] == ("audit", "audit"), f"MEFP-5 is back with Audit: {board.row()}")

    # The operation's own checks still run for the role that may ask.
    moved, why = board.override_move("director", "nowhere", "audit")
    check(not moved and "nowhere" in why, f"an invalid state is still refused: {why!r}")


# Functions whose provisioned body (what production runs) still differs from
# schema.sql's (what the suites load), ignoring comments and whitespace. Each
# is named with why it is not fixed here; anything else that diverges fails.
KNOWN_DIVERGENCE = {
    # Production runs pgu913's body, which authorizes a no-commit submission
    # as the ordinary submit_to_audit; schema.sql's is owner-scoped. Tightening
    # it changes who may waive a commit on every live board -- reported on
    # SYRD-275 for its own ticket rather than changed silently here.
    "submit_to_audit_without_commit(id text, reason text)",
}
KNOWN_PROVISIONED_ONLY = {
    # pgu450 installs it; schema.sql has since dropped it. Nothing calls it.
    "remember_ticket_implementer_assignee()",
}
FUNCTIONS = """
SELECT coalesce(jsonb_object_agg(
    p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')',
    md5(regexp_replace(regexp_replace(pg_get_functiondef(p.oid), '--[^\n]*', '', 'g'), '\s+', '', 'g'))
), '{}')::text
FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
WHERE n.nspname = 'ticket_board';
"""


def run_parity(cluster) -> None:
    """What the suites test is what production runs, function by function."""
    provisioned = ProvisionedBoard(cluster, "parity_provisioned", ROOT)
    fresh = t.conninfo(cluster.socket_dir, cluster.port, "parity_fresh")
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "parity_fresh"])
    t.psql(fresh, (ROOT / "scripts/ticket_board/schema.sql").read_text())
    suites = json.loads(t.psql(fresh, FUNCTIONS))
    production = json.loads(t.psql(provisioned.admin, FUNCTIONS))
    check(len(suites) > 150, f"the comparison sees the board's functions: {len(suites)}")
    for name in ("require_actor(p_allowed_roles text[], p_action text)", "control_override_actions()"):
        check(name in production and suites.get(name) == production[name],
              f"production runs the tested {name}")
    diverged = {name for name in set(suites) & set(production) if suites[name] != production[name]}
    check(diverged == KNOWN_DIVERGENCE,
          f"no other function runs differently in production than in the suites: {sorted(diverged ^ KNOWN_DIVERGENCE)}")
    check(set(suites) - set(production) == set(), f"nothing tested is missing in production: {sorted(set(suites) - set(production))}")
    check(set(production) - set(suites) == KNOWN_PROVISIONED_ONLY,
          f"nothing untested runs in production: {sorted(set(production) - set(suites))}")


def main() -> int:
    before_only = "--before" in sys.argv
    with tempfile.TemporaryDirectory(prefix="syrd275.") as tmp:
        with temporary_cluster(prefix="syrd275-", shutdown="immediate") as cluster:
            run_before(cluster, Path(tmp))
            if not before_only:
                run_after(cluster)
                run_parity(cluster)
    print(f"control_override_reaches_boards_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
