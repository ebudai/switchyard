#!/usr/bin/env python3
"""SYRD-271: a sign-off reviews one commit, and does not carry to another.

MEFP-4 was Audit-approved for 6d4ee1a. In Director review the Director routed
it back to Ops for a safety edit, Ops pushed 4f9fd17 and submitted it -- and
the board went straight to director_review with audit_signoff=true, Audit's
verdict on 6d4ee1a standing in for a review of 4f9fd17 nobody had done.

The route back was a plain `move`, which clears nothing, and the declared
executor skips a review stage whose sign-off is already set. Both are right
for what they are; what was missing is that a sign-off belongs to the commit
it reviewed. This replays MEFP-4 on real PostgreSQL through the real app:

* on the board as it shipped (`--before`), to show the defect is real;
* on this tree, where a submission carrying a different commit clears every
  review sign-off and enters review for that commit, and any later transition
  that would put a different commit behind a standing sign-off is refused.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
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
AUDITED = "6d4ee1aa99147e8118f59e637be02b660d62d064"
CHANGED = "4f9fd175593d5e804cadad77549b649a34c7d611"
INTEGRATED = "22ad84fc0000000000000000000000000000beef"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def mefp_shaped_workflow() -> dict:
    """The shipped document plus the one row MEFP-4 took back: a Director `route` that is a plain move."""
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


class Board:
    def __init__(self, cluster, db: str, schema: str) -> None:
        self.admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(self.admin, schema)
        try:
            t.create_roles(self.admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        t.psql(self.admin, t.RBAC_PATH.read_text())
        # Commits are checked against a repository; these tests are about the
        # sign-offs, so the board is told every well-formed hash is known.
        self.app = t.TicketBoardApp(
            cluster.root / f"frames-{db}", cluster.root / f"assets-{db}", project="cerulean", ticket_prefix="PGU",
            database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
        )
        self.app._resolve_known_commit = lambda value: (
            {AUDITED[:len(value)]: AUDITED, CHANGED[:len(value)]: CHANGED,
             INTEGRATED[:len(value)]: INTEGRATED}.get(value, value), [])
        with self.app._pg_connect() as conn:
            self.app._pg_set_caller_role(conn, "director")
            conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
            conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(mefp_shaped_workflow()),))
            conn.commit()

    def act(self, ticket_id: str, action: str, payload: dict, role: str) -> dict:
        return self.app.perform_workflow_action(ticket_id, action, payload, caller_role=role)

    def refused(self, ticket_id: str, action: str, payload: dict, role: str) -> str:
        try:
            self.act(ticket_id, action, payload, role)
        except Exception as exc:  # noqa: BLE001 - the refusal's text is what is checked
            return str(exc)
        return ""

    def audited_then_routed_back(self, ticket_id: str, *, inspection: bool = False, owner: str = "ops") -> dict:
        """MEFP-4 up to the moment it came back to Ops: reviewed for AUDITED, in Ops's hands again."""
        t.seed_postgres_ticket(self.admin, ticket_id, title="Preserve provisioning", state="in_progress",
                               assignee=owner, needs_inspection=inspection)
        self.act(ticket_id, "submit_to_audit", {"commit_hash": AUDITED}, owner)
        if inspection:
            self.act(ticket_id, "inspector_sign_off", {"text": "inspected 6d4ee1a"}, "inspector")
        reviewed = self.act(ticket_id, "audit_sign_off", {"text": "approved 6d4ee1a"}, "audit")
        check((reviewed["state"], reviewed["audit_signoff"], reviewed["commit_hash"])
              == ("director_review", True, AUDITED), f"{ticket_id} reached Director review for {AUDITED[:7]}: {reviewed}")
        back = self.act(ticket_id, "route", {"target": "in_progress", "assignee": owner}, "director")
        check((back["state"], back["assignee"]) == ("in_progress", owner), f"and was routed back to {owner}: {back}")
        return back


def replay(board: Board) -> dict:
    board.audited_then_routed_back("PGU-4")
    return board.act("PGU-4", "submit_to_audit", {"commit_hash": CHANGED}, "ops")


def run_before(cluster) -> None:
    """The defect, on the board as it shipped before this change."""
    base = subprocess.run(["git", "-C", str(ROOT), "merge-base", "HEAD", "origin/main"],
                          check=True, capture_output=True, text=True).stdout.strip()
    schema = subprocess.run(["git", "-C", str(ROOT), "show", f"{base}:scripts/ticket_board/schema.sql"],
                            check=True, capture_output=True, text=True).stdout
    resubmitted = replay(Board(cluster, "before", schema))
    check((resubmitted["state"], resubmitted["audit_signoff"], resubmitted["commit_hash"])
          == ("director_review", True, CHANGED),
          f"reproduced: the changed commit skips Audit on the old sign-off: {resubmitted['state']} "
          f"{resubmitted['audit_signoff']} {resubmitted['commit_hash'][:7]}")


def main() -> int:
    before_only = "--before" in sys.argv
    with temporary_cluster(prefix="syrd271-", shutdown="immediate") as cluster:
        run_before(cluster)
        if not before_only:
            run_after(cluster)
            run_undeclared(cluster)
    print(f"signoff_follows_commit_test: {CHECKS} checks ok")
    return 0


def over_http(board: Board, ticket_id: str, operation: str, payload: dict, role: str) -> dict:
    """The request `ticket-board-write` makes, through the real server's boundary."""
    import threading
    from scripts.ticket_board.write_client import TicketBoardWriteClient

    server = t.TicketBoardServer(("127.0.0.1", 0), board.app, director_notifier=t.QuietNotifier())
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        client = TicketBoardWriteClient(f"http://127.0.0.1:{server.server_port}", caller_role=role,
                                        write_token=server.write_token)
        return client._ticket_action(ticket_id, operation, payload, caller_role=role)["ticket"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def run_after(cluster) -> None:
    board = Board(cluster, "after", t.SCHEMA_PATH.read_text())

    # MEFP-4, through the server: the changed commit enters Audit, unsigned.
    board.audited_then_routed_back("PGU-4")
    resubmitted = over_http(board, "PGU-4", "submit_to_audit", {"commit_hash": CHANGED}, "ops")
    check((resubmitted["state"], resubmitted["assignee"], resubmitted["audit_signoff"], resubmitted["commit_hash"])
          == ("audit", "audit", False, CHANGED),
          f"the changed commit goes to Audit for review, with no sign-off: {resubmitted['state']}/"
          f"{resubmitted['assignee']} {resubmitted['audit_signoff']} {resubmitted['commit_hash'][:7]}")
    check(board.refused("PGU-4", "mark_done", {"commit_hash": CHANGED}, "director"),
          "and it cannot be closed from there")
    approved = board.act("PGU-4", "audit_sign_off", {"text": "approved 4f9fd17"}, "audit")
    check((approved["state"], approved["audit_signoff"], approved["commit_hash"])
          == ("director_review", True, CHANGED), f"Audit's approval of that exact commit moves it on: {approved}")
    done = board.act("PGU-4", "mark_done", {"commit_hash": CHANGED}, "director")
    check(done["state"] == "done" and done["commit_hash"] == CHANGED, f"and closes it: {done['state']}")

    # The same commit resubmitted keeps its review: that exact work was seen.
    board.audited_then_routed_back("PGU-5")
    same = board.act("PGU-5", "submit_to_audit", {"commit_hash": AUDITED}, "ops")
    check((same["state"], same["audit_signoff"], same["commit_hash"]) == ("director_review", True, AUDITED),
          f"resubmitting the audited commit itself is not new work: {same['state']} {same['audit_signoff']}")

    # Closing is not resubmission. The Director closes on the integration
    # commit -- the audited candidate cherry-picked or merged onto an advanced
    # main, provenance narrated (SYRD-163, SYRD-194, SYRD-226) -- so mark_done
    # recording a commit other than the audited one stays allowed.
    closed = board.act("PGU-5", "mark_done", {"commit_hash": INTEGRATED}, "director")
    check((closed["state"], closed["commit_hash"], closed["audit_signoff"]) == ("done", INTEGRATED, True),
          f"the Director still closes on an integration commit: {closed['state']} {closed['commit_hash'][:8]}")

    # Every review flag, not just Audit's: with inspection on the path, both
    # sign-offs go, and the new commit starts at inspection.
    board.audited_then_routed_back("PGU-6", inspection=True)
    inspected = board.act("PGU-6", "submit_to_audit", {"commit_hash": CHANGED}, "ops")
    check((inspected["state"], inspected["inspector_signoff"], inspected["audit_signoff"])
          == ("inspection", False, False),
          f"inspection and audit are both owed again: {inspected['state']} "
          f"{inspected['inspector_signoff']} {inspected['audit_signoff']}")

    # A user sign-off that stood for the old commit is cleared too -- taken
    # the whole way: Audit, the Director's DAT, the User, then routed back.
    t.seed_postgres_ticket(board.admin, "PGU-7", title="User-visible change", state="in_progress", assignee="main",
                           needs_user_signoff=True)
    seeded = board.app.get_ticket("PGU-7")
    check((seeded["state"], seeded["assignee"]) == ("in_progress", "main"), f"PGU-7 is Main's: {seeded['state']}")
    board.act("PGU-7", "submit_to_audit", {"commit_hash": AUDITED}, "main")
    board.act("PGU-7", "audit_sign_off", {"text": "approved"}, "audit")
    board.act("PGU-7", "director_dat_sign_off", {"text": "fine"}, "director")
    accepted = board.act("PGU-7", "user_sign_off", {"text": "accepted"}, "user")
    check((accepted["state"], accepted["user_signoff"], accepted["audit_signoff"]) == ("director_review", True, True),
          f"PGU-7 reached Director review with both sign-offs for {AUDITED[:7]}: {accepted['state']}")
    back = board.act("PGU-7", "route", {"target": "in_progress", "assignee": "main"}, "director")
    check((back["state"], back["user_signoff"], back["audit_signoff"]) == ("in_progress", True, True),
          f"and was routed back still carrying both: {back['state']} {back['user_signoff']} {back['audit_signoff']}")
    user_path = board.act("PGU-7", "submit_to_audit", {"commit_hash": CHANGED}, "main")
    check(not user_path["user_signoff"] and not user_path["audit_signoff"] and user_path["state"] == "audit",
          f"a changed commit clears the user's sign-off as well as Audit's: {user_path['state']} "
          f"user={user_path['user_signoff']} audit={user_path['audit_signoff']}")

    # No code where there was code is different work too.
    board.audited_then_routed_back("PGU-8", owner="app")
    no_code = board.act("PGU-8", "submit_to_audit_without_commit", {"reason": "reverted; nothing to ship"}, "app")
    check((no_code["state"], no_code["audit_signoff"], no_code["commit_hash"], no_code["commit_exempt"])
          == ("audit", False, "", True),
          f"a no-code submission after an audited commit is reviewed as such: {no_code['state']} "
          f"{no_code['audit_signoff']} {no_code['commit_exempt']}")
    # And a no-code submission has no commit to prove it is the same work, so
    # a second one after its approval is reviewed again, not waved through.
    approved = board.act("PGU-8", "audit_sign_off", {"text": "no-code approved"}, "audit")
    check((approved["state"], approved["audit_signoff"]) == ("director_review", True), f"{approved['state']}")
    board.act("PGU-8", "route", {"target": "in_progress", "assignee": "app"}, "director")
    again = board.act("PGU-8", "submit_to_audit_without_commit", {"reason": "a different finding"}, "app")
    check((again["state"], again["audit_signoff"]) == ("audit", False),
          f"a second no-code submission enters Audit unsigned: {again['state']} {again['audit_signoff']}")


def run_undeclared(cluster) -> None:
    """The undeclared trigger had the same shortcut; the same sequence through it.

    Through `update_ticket`, with the patches the server builds for these
    operations on a board with no declared workflow.
    """
    admin = t.conninfo(cluster.socket_dir, cluster.port, "legacy")
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "legacy"])
    t.psql(admin, t.SCHEMA_PATH.read_text())
    t.psql(admin, t.RBAC_PATH.read_text())
    app = t.TicketBoardApp(
        cluster.root / "frames-legacy", cluster.root / "assets-legacy", project="cerulean", ticket_prefix="PGU",
        database_url=t.conninfo(cluster.socket_dir, cluster.port, "legacy", t.SERVICE_ROLE),
    )
    app._resolve_known_commit = lambda value: (
        {AUDITED[:len(value)]: AUDITED, CHANGED[:len(value)]: CHANGED}.get(value, value), [])
    check(app.workflow_configuration() is None, "this board declares no workflow")
    t.seed_postgres_ticket(admin, "PGU-9", title="Undeclared", state="director_review", assignee="director",
                           commit_hash=AUDITED, audit_signoff=True)

    # An ordinary route back clears the sign-off on this path already; the
    # narrated override does not, which is how one can still be standing when
    # different work arrives.
    back = app.force_move_ticket("PGU-9", "in_progress", "ops", suppress_notification=True, caller_role="director")
    check((back["state"], back["audit_signoff"]) == ("in_progress", True),
          f"undeclared: back with Ops by override, the sign-off still set: {back['state']} {back['audit_signoff']}")
    resubmitted = app.update_ticket("PGU-9", {"state": "audit", "commit_hash": CHANGED}, caller_role="ops")
    check((resubmitted["state"], resubmitted["audit_signoff"], resubmitted["commit_hash"]) == ("audit", False, CHANGED),
          f"undeclared: the changed commit enters Audit unsigned: {resubmitted['state']} "
          f"{resubmitted['audit_signoff']} {resubmitted['commit_hash'][:7]}")

    # The undeclared submit_to_audit clears Audit's and the Inspector's
    # sign-offs itself, but never the User's -- and entering user_review
    # already signed skips it. The User's verdict on the old commit goes too.
    t.seed_postgres_ticket(admin, "PGU-10", title="Undeclared, user-visible", state="director_review",
                           assignee="director", commit_hash=AUDITED, audit_signoff=True,
                           needs_user_signoff=True, user_signoff=True)
    back = app.force_move_ticket("PGU-10", "in_progress", "app", suppress_notification=True, caller_role="director")
    check((back["state"], back["user_signoff"]) == ("in_progress", True),
          f"undeclared: back with App, the User's sign-off still set: {back['state']} {back['user_signoff']}")
    user_path = app.update_ticket("PGU-10", {"state": "audit", "commit_hash": CHANGED}, caller_role="app")
    check((user_path["state"], user_path["user_signoff"], user_path["audit_signoff"]) == ("audit", False, False),
          f"undeclared: the changed commit clears the User's sign-off too: {user_path['state']} "
          f"user={user_path['user_signoff']} audit={user_path['audit_signoff']}")

    # Only what is CARRIED goes. A write that grants a sign-off while it
    # submits -- how these suites stage "inspected, then submitted" -- keeps it.
    t.seed_postgres_ticket(admin, "PGU-11", title="Undeclared, inspected", state="in_progress", assignee="main",
                           needs_inspection=True)
    t.psql(admin, "UPDATE ticket_board.tickets SET inspector_signoff = true, state = 'audit', "
                  f"commit_hash = '{CHANGED}' WHERE id = 'PGU-11';")
    granted = app.get_ticket("PGU-11")
    check((granted["state"], granted["inspector_signoff"]) == ("audit", True),
          f"undeclared: a sign-off granted by the same write stands: {granted['state']} {granted['inspector_signoff']}")

if __name__ == "__main__":
    raise SystemExit(main())
