#!/usr/bin/env python3
"""SYRD-413: a changed commit needs a fresh, exact-commit Audit -- on a board built as production builds it.

MEFP-22 (and MEFP-5 before it: SYRD-274, SYRD-315, SYRD-394) was Audit-approved
for one commit, routed back to its implementer by the Director's declared
`route director_review -> in_progress` (a plain move, which clears nothing),
and resubmitted with a different commit -- and went straight to Final Sign-Off
on the old approval. SYRD-271 (19132fe) fixed that on main. MEFP's board was
still running 49abeb4, which predates it.

SYRD-271's own suite loads schema.sql alone, and SYRD-275 showed that proves
nothing about production: every board is built as schema.sql, then EVERY
migration through the real runner, then rbac.sql, and the newest migration's
copy of a function is what runs. So this replays MEFP-22's exact sequence --
approval, route back, an audit_prompt edit, resubmission of a new commit --
on boards built that way:

* from 49abeb4, the release MEFP was running: the defect, reproduced;
* from this tree: the new commit enters Audit unsigned, and only Audit's
  approval of that exact commit moves it on; the same commit resubmitted keeps
  its review; and the Director's sanctioned negative reset (director_edit)
  works, while an ordinary flag edit stays refused.
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
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402
from workflow_document_eras import before_relaying  # noqa: E402

CHECKS = 0
INSTALLED = "49abeb4b00d47805e515af455ce506cb4ae41888"  # what MEFP ran for MEFP-5 and MEFP-22
APPROVED = "cf704eb143bc73baef1d3f8c9e6b1a47937f42ca"
CHANGED = "6095239f911582871c16a4811c8f469c83f2cd45"
TICKET = "PGU-22"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def mefp_shaped() -> dict:
    """The shipped document plus MEFP's Director route back to implementation: a plain move."""
    document = copy.deepcopy(before_relaying())
    document["project"] = "cerulean"
    document.setdefault("reassign", {})
    document.setdefault("remove_stages", [])
    document["transitions"].append({
        "action": "route", "from": "director_review", "to": "in_progress", "actors": ["director"],
        "primitive": "move", "owner_scoped": False, "allow_no_code": False, "clear_signoffs": [],
        "require_commit": False, "require_reason": False, "label": "Route",
    })
    return document


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts/ticket_board",
                              "scripts/ticket-board-migrate"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


class Board:
    """Companion roles, schema.sql, the real ticket-board-migrate, rbac.sql, a declared workflow."""

    def __init__(self, cluster, db: str, tree: Path) -> None:
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
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
        runner = subprocess.run(["bash", str(tree / "scripts/ticket-board-migrate")],
                                env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": self.admin},
                                capture_output=True, text=True)
        assert runner.returncode == 0, runner.stderr
        self.applied = runner.stderr + runner.stdout
        t.psql(self.admin, (tree / "scripts/ticket_board/rbac.sql").read_text())
        self.app = t.TicketBoardApp(
            cluster.root / f"frames-{db}", cluster.root / f"assets-{db}", project="cerulean", ticket_prefix="PGU",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
        )
        # Commits are checked against a repository; this is about the sign-off.
        self.app._resolve_known_commit = lambda value: (
            {APPROVED[:len(value)]: APPROVED, CHANGED[:len(value)]: CHANGED}.get(value, value), [])
        with self.app._pg_connect() as conn:
            self.app._pg_set_caller_role(conn, "director")
            conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
            conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(mefp_shaped()),))
            conn.commit()

    def act(self, action: str, payload: dict, role: str) -> dict:
        return self.app.perform_workflow_action(TICKET, action, payload, caller_role=role)

    def row(self) -> tuple[str, str, bool, str]:
        ticket = self.app.get_ticket(TICKET)
        return ticket["state"], ticket["assignee"], ticket["audit_signoff"], ticket["commit_hash"]

    def approved_then_routed_back(self) -> None:
        """MEFP-22 up to the rework: Audit approved APPROVED, the Director routed it back to Main."""
        t.seed_postgres_ticket(self.admin, TICKET, title="MEFP-22", state="in_progress", assignee="main")
        self.act("submit_to_audit", {"commit_hash": APPROVED}, "main")
        self.act("audit_sign_off", {"text": f"Audit sign-off for {APPROVED}"}, "audit")
        check(self.row() == ("director_review", "director", True, APPROVED), f"approved for {APPROVED[:7]}: {self.row()}")
        self.act("route", {"target": "in_progress", "assignee": "main"}, "director")
        check(self.row() == ("in_progress", "main", True, APPROVED),
              f"routed back to Main, the approval still recorded: {self.row()}")
        # What Main did next: point the Audit prompt at the new commit, then submit it.
        self.app.update_ticket(TICKET, {"audit_prompt": f"Review exactly {CHANGED}"}, caller_role="main")


def run_installed(cluster, tmp: Path) -> None:
    board = Board(cluster, "installed", tree_at(INSTALLED, tmp / "installed"))
    board.approved_then_routed_back()
    board.act("submit_to_audit", {"commit_hash": CHANGED}, "main")
    check(board.row() == ("director_review", "director", True, CHANGED),
          f"reproduced on MEFP's release: {CHANGED[:7]} reached Final Sign-Off on {APPROVED[:7]}'s approval: {board.row()}")


def run_current(cluster) -> None:
    board = Board(cluster, "current", ROOT)
    check("apply pgu961_syrd271_signoff_follows_commit.sql" in board.applied, "the runner applied SYRD-271's migration")
    board.approved_then_routed_back()
    board.act("submit_to_audit", {"commit_hash": CHANGED}, "main")
    check(board.row() == ("audit", "audit", False, CHANGED),
          f"the changed commit enters Audit, unsigned: {board.row()}")
    try:
        board.act("mark_done", {"commit_hash": CHANGED}, "director")
    except Exception as exc:  # noqa: BLE001 - the refusal's text is what is checked
        closing = str(exc)
    else:
        closing = ""
    check("unknown or ambiguous workflow action" in closing, f"and cannot be closed from Audit: {closing!r}")
    board.act("audit_sign_off", {"text": f"Audit sign-off for {CHANGED}"}, "audit")
    check(board.row() == ("director_review", "director", True, CHANGED),
          f"only Audit's approval of that exact commit moves it on: {board.row()}")

    # The same commit resubmitted keeps its review: that exact work was seen.
    board.act("route", {"target": "in_progress", "assignee": "main"}, "director")
    board.act("submit_to_audit", {"commit_hash": CHANGED}, "main")
    check(board.row() == ("director_review", "director", True, CHANGED),
          f"resubmitting the audited commit itself is not new work: {board.row()}")

    # SYRD-315's other half: the Director can withdraw an approval it no longer
    # trusts through the sanctioned edit, never through an ordinary field edit.
    try:
        board.app.update_ticket(TICKET, {"audit_signoff": False}, caller_role="director")
    except Exception as exc:  # noqa: BLE001
        ordinary = str(exc)
    else:
        ordinary = ""
    check(ordinary != "" and board.row()[2] is True, f"an ordinary flag edit is still refused: {ordinary!r}")
    board.app.director_edit_ticket(TICKET, {"audit_signoff": False}, reason="Approval withdrawn pending re-audit.",
                                   caller_role="director")
    check(board.row()[2] is False, f"director_edit withdraws the approval: {board.row()}")
    try:
        board.app.director_edit_ticket(TICKET, {"audit_signoff": True}, reason="x", caller_role="director")
    except Exception as exc:  # noqa: BLE001
        raised = str(exc)
    else:
        raised = ""
    check(raised != "" and board.row()[2] is False, f"and can never grant one: {raised!r}")


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd413.") as tmp:
        with temporary_cluster(prefix="syrd413-", shutdown="immediate") as cluster:
            run_installed(cluster, Path(tmp))
            run_current(cluster)
    print(f"changed_commit_fresh_audit_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
