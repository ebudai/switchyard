#!/usr/bin/env python3
"""The Director can put down a review nobody has decided yet.

Live syrd, 2026-09-24: honouring the User's request to defer SYRD-37 until
provider pinning and canaries were understood, the Director ran the advertised
`defer` from User Review. The board refused it -- "stage signoff required" --
because user_signoff was false. The User had neither accepted nor rejected the
rehearsal; that was the point. SYRD-37 stayed in User Review, holding a serial
reservation (SYRD-263).

The executor required a stage's sign-off before any transition left it except
`return` and `reopen`. The SYRD-92 suite deferred a review too, but always one
whose own sign-off was already true, so the gate never fired there.

Real PostgreSQL, the real handler, today's shipped workflow, on a fresh board
and on one that arrives by upgrade.
"""

from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (ROOT, ROOT / "tests"):
    if str(extra) not in sys.path:
        sys.path.insert(0, str(extra))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from schema_function_drift import assert_no_drift, owning_migration  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

WORKFLOW = json.loads((ROOT / "examples" / "workflows" / "inspection.json").read_text())
MIGRATIONS = ROOT / "scripts" / "ticket_board" / "migrations"
MIGRATION = MIGRATIONS / "pgu959_syrd263_defer_unaccepted_review.sql"
COMMIT = "637733ce034d0000000000000000000000000000"
IMPLEMENTERS = ("main", "app", "ops")

CHECKS = 0


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(ROOT), *args], check=True, capture_output=True, text=True).stdout


def schema_before_this_change() -> str:
    """schema.sql as it stood before the migration joined the tree."""
    adding = git("log", "--format=%H", "--diff-filter=A", "--", str(MIGRATION.relative_to(ROOT))).split()
    assert adding, "the migration is not committed; clone-based checks see only commits"
    return git("show", f"{adding[-1]}^:scripts/ticket_board/schema.sql")


def board(cluster, db: str, *, legacy: bool, document: dict):
    admin = t.conninfo(cluster.socket_dir, cluster.port, db)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
    t.psql(admin, schema_before_this_change() if legacy else t.SCHEMA_PATH.read_text())
    try:
        t.create_roles(admin)
    except AssertionError as exc:
        if "already exists" not in str(exc):
            raise
    if legacy:
        # What the runner applies to a board that predates this release.
        t.psql(admin, MIGRATION.read_text())
    t.psql(admin, t.RBAC_PATH.read_text())
    app = t.TicketBoardApp(
        cluster.root / f"frames-{db}",
        cluster.root / f"assets-{db}",
        project="cerulean",
        ticket_prefix="PGU",
        database_url=t.conninfo(cluster.socket_dir, cluster.port, db, t.SERVICE_ROLE),
    )
    document = copy.deepcopy(document)
    document["project"] = "cerulean"
    document.setdefault("reassign", {})
    document.setdefault("remove_stages", [])
    with app._pg_connect() as conn:
        app._pg_set_caller_role(conn, "director")
        conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
        conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(document),))
        conn.commit()
    return app, admin


def row(admin: str, ticket_id: str) -> dict:
    fields = ("state", "assignee", "parked", "commit_hash", "audit_signoff", "inspector_signoff",
              "user_signoff", "needs_user_signoff", "needs_inspection", "title")
    raw = t.psql(admin, "SELECT " + " || '|' || ".join(f"{f}::text" for f in fields)
                 + f" FROM ticket_board.tickets WHERE id = '{ticket_id}';").strip()
    values = dict(zip(fields, raw.split("|")))
    return {k: (v == "true" if v in ("true", "false") else v) for k, v in values.items()}


def comments(admin: str, ticket_id: str) -> str:
    return t.psql(admin, f"SELECT count(*) FROM ticket_board.ticket_comments WHERE ticket_id = '{ticket_id}';")


def reserved_by(admin: str, ticket_id: str) -> list[str]:
    return [
        role for role in IMPLEMENTERS
        if t.psql(admin, f"SELECT coalesce(ticket_board.ticket_current_reserved_ticket('{role}'),'');").strip()
        == ticket_id
    ]


def refused(call) -> str:
    try:
        call()
    except Exception as exc:  # the handler raises the database's refusal
        return str(exc)
    raise AssertionError("the transition was allowed")


def undecided_reviews_are_parked(app, admin: str, label: str) -> None:
    # --- SYRD-37 exactly: audited, in User Review, the User has not decided ---
    t.seed_postgres_ticket(
        admin, "PGU-37", title="Eight-Hermes rehearsal", state="user_review", assignee="user",
        audit_signoff=True, needs_user_signoff=True, user_signoff=False, commit_hash=COMMIT,
    )
    # As a real submission leaves it: the review is held against its implementer.
    t.psql(admin, "INSERT INTO ticket_board.ticket_notification_state(ticket_id,current_state,current_assignee,last_implementer_assignee) "
                  "VALUES ('PGU-37','user_review','user','ops') ON CONFLICT (ticket_id) DO UPDATE SET "
                  "current_state='user_review', last_implementer_assignee='ops';")
    held_before = reserved_by(admin, "PGU-37")
    check(held_before == ["ops"], (label, "the fixture holds no reservation to release", held_before))
    before = row(admin, "PGU-37")
    notes_before = comments(admin, "PGU-37")

    parked = app.perform_workflow_action("PGU-37", "defer", {}, caller_role="director")

    after = row(admin, "PGU-37")
    check(parked["state"] == "backlog", (label, parked["state"]))
    check(after["assignee"] == "unassigned" and after["parked"] is True, (label, after))
    # Provenance rides along: the audited commit and the audit's sign-off...
    check(after["commit_hash"] == COMMIT, (label, after))
    check(after["audit_signoff"] is True, (label, after))
    # ...and the User's decision is neither recorded nor erased: still undecided.
    check(after["user_signoff"] is False, (label, after))
    check(after["needs_user_signoff"] is True, (label, after))
    check(after["title"] == before["title"], (label, after))
    check(comments(admin, "PGU-37") == notes_before, (label, "a comment was added or removed"))
    # And the slot it held is handed back.
    check(reserved_by(admin, "PGU-37") == [], (label, "still reserved", held_before))

    # --- every other review nobody has decided parks the same way ---------
    for ticket_id, stage, owner, flags, signoff in (
        ("PGU-41", "inspection", "inspector", {"needs_inspection": True}, "inspector_signoff"),
        ("PGU-42", "audit", "audit", {}, "audit_signoff"),
    ):
        t.seed_postgres_ticket(admin, ticket_id, title=f"Unjudged {stage}", state=stage,
                               assignee=owner, commit_hash=COMMIT, **flags)
        moved = app.perform_workflow_action(ticket_id, "defer", {}, caller_role="director")
        now = row(admin, ticket_id)
        check(moved["state"] == "backlog", (label, stage, moved["state"]))
        check(now[signoff] is False, (label, stage, "a verdict was recorded by parking", now))
        check(now["commit_hash"] == COMMIT, (label, stage, now))

    # --- deferral stays the Director's --------------------------------------
    t.seed_postgres_ticket(admin, "PGU-43", title="Still undecided", state="user_review", assignee="user",
                           audit_signoff=True, needs_user_signoff=True, commit_hash=COMMIT)
    for caller in ("user", "audit", "ops"):
        before = row(admin, "PGU-43")
        refused(lambda caller=caller: app.perform_workflow_action("PGU-43", "defer", {}, caller_role=caller))
        check(row(admin, "PGU-43") == before, (label, caller))


def forward_moves_still_need_the_signoff(app, admin: str, label: str) -> None:
    """Only the destination's shape opens the gate; a forward move stays shut."""
    t.seed_postgres_ticket(admin, "PGU-50", title="Skip ahead", state="user_review", assignee="user",
                           audit_signoff=True, needs_user_signoff=True, commit_hash=COMMIT)
    before = row(admin, "PGU-50")
    said = refused(lambda: app.perform_workflow_action("PGU-50", "skip_user_review", {}, caller_role="director"))
    check("stage signoff required" in said, (label, said))
    check(row(admin, "PGU-50") == before, (label, row(admin, "PGU-50")))


def with_forward_move(document: dict) -> dict:
    document = copy.deepcopy(document)
    template = next(tr for tr in document["transitions"] if tr["from"] == "user_review" and tr["action"] == "defer")
    document["transitions"].append({
        **template, "action": "skip_user_review", "label": "Skip user review", "to": "director_review",
    })
    return document


def test_the_migration_is_the_copy_an_upgraded_board_runs() -> None:
    assert_no_drift("enforce_declared_ticket_update")
    check(owning_migration("enforce_declared_ticket_update") == MIGRATION,
          owning_migration("enforce_declared_ticket_update").name)
    later = [p.name for p in sorted(MIGRATIONS.glob("pgu*.sql")) if p.name > MIGRATION.name]
    check(later == [], later)


def main() -> int:
    test_the_migration_is_the_copy_an_upgraded_board_runs()
    with temporary_cluster(prefix="syrd263-", shutdown="immediate") as cluster:
        for label, legacy in (("fresh", False), ("upgraded", True)):
            app, admin = board(cluster, f"syrd263_{label}", legacy=legacy, document=WORKFLOW)
            undecided_reviews_are_parked(app, admin, label)
            app, admin = board(cluster, f"syrd263_{label}_fwd", legacy=legacy, document=with_forward_move(WORKFLOW))
            forward_moves_still_need_the_signoff(app, admin, label)
        # Idempotent: the runner may apply it again.
        t.psql(admin, MIGRATION.read_text())
        t.psql(admin, MIGRATION.read_text())
        assert_no_drift("enforce_declared_ticket_update")
    print(f"director_defer_unaccepted_review_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
