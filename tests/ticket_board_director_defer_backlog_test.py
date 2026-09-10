#!/usr/bin/env python3
"""SYRD-92: the Director can put work down without destroying it.

A declarative tenant lost its Backlog stage and its `defer` transition and still
validated, because the capability floor only asked whether the Director could
leave each stage -- and `cancel` leaves every stage. Cancel is the wrong exit:
it ends the work. Live syrd revision 22 is that document, so `defer` answered
`director cannot call defer` while the UI still showed Deferred.

Checked against real PostgreSQL and the real handler: the floor that now refuses
such a document, the migration that repairs one already stored, and what a
deferral actually does to a ticket.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from scripts.ticket_board.workflow_config import parking_stage_names, validate  # noqa: E402

from temporary_cluster import temporary_cluster  # noqa: E402

CANONICAL = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
MIGRATION_PATH = ROOT / "scripts/ticket_board/migrations/pgu929_syrd92_director_defer_backlog.sql"
MIGRATION = MIGRATION_PATH.read_text()


def rejected(call, expected: str = ""):
    try:
        call()
    except Exception as exc:  # noqa: BLE001 -- the refusal is the assertion
        if expected:
            assert expected in str(exc), f"expected {expected!r} in: {exc}"
        return str(exc)
    raise AssertionError(f"expected a refusal containing {expected!r}")


def degraded(document: dict) -> dict:
    """A revision-22 shaped tenant: no parking stage, cancel as the only exit."""
    document = copy.deepcopy(document)
    document["stages"] = [s for s in document["stages"] if s["name"] != "backlog"]
    document["transitions"] = [x for x in document["transitions"] if "backlog" not in (x["from"], x["to"])]
    document["queue"] = {"stage": "analysis", "assignee": "director"}
    document["remove_stages"] = ["backlog"]
    return document


def ticket_row(admin: str, ticket_id: str) -> dict:
    row = t.psql(
        admin,
        "SELECT state || '|' || assignee || '|' || parked::text || '|' || queued_for_assignee || '|' "
        "|| audit_signoff::text || '|' || title "
        f"FROM ticket_board.tickets WHERE id = '{ticket_id}';",
    )
    state, assignee, parked, queued_for, audit_signoff, title = row.split("|", 5)
    return {
        "state": state,
        "assignee": assignee,
        "parked": parked == "true",
        "queued_for_assignee": queued_for,
        "audit_signoff": audit_signoff == "true",
        "title": title,
    }


# --- the floor -------------------------------------------------------------


def test_the_canonical_workflow_keeps_somewhere_to_defer_to() -> None:
    cfg = validate(copy.deepcopy(CANONICAL))
    parking = parking_stage_names(cfg)
    assert parking == {"backlog"}, parking
    stage = next(s for s in cfg["stages"] if s["name"] == "backlog")
    # What the ticket asks a Backlog to be: not finished, nobody owns it, and it
    # notifies nobody, so parking work there does not hand it to someone.
    assert stage["terminal"] is False and stage["owners"] == [] and stage["notify"]["kind"] == "none", stage
    defers = {x["from"] for x in cfg["transitions"] if x["to"] == "backlog" and "director" in x["actors"]}
    active = {s["name"] for s in cfg["stages"] if not s["terminal"] and s["name"] != "backlog"}
    assert defers == active, (defers, active)
    revivals = [x for x in cfg["transitions"] if x["from"] == "backlog" and x["to"] == "analysis"]
    assert revivals and "director" in revivals[0]["actors"], revivals


def test_cancel_is_not_a_substitute_for_somewhere_to_put_work_down() -> None:
    """The exact hole SYRD-82's floor left open."""
    rejected(lambda: validate(degraded(CANONICAL)), "stage where deferred work can wait")

    # A parking stage with no way out strands whatever lands in it.
    stranded = copy.deepcopy(CANONICAL)
    stranded["transitions"] = [x for x in stranded["transitions"] if not (x["from"] == "backlog" and x["to"] == "analysis")]
    rejected(lambda: validate(stranded), "ordinary way back")

    # An active stage with no defer out of it is work that can only be cancelled.
    for stage in ("draft", "analysis", "in_progress", "audit", "director_review"):
        missing = copy.deepcopy(CANONICAL)
        missing["transitions"] = [
            x for x in missing["transitions"] if not (x["from"] == stage and x["to"] == "backlog")
        ]
        message = rejected(lambda missing=missing: validate(missing), "defer work out of every active stage")
        assert stage in message, (stage, message)


def test_a_review_stage_is_not_exempt_from_the_floor() -> None:
    """A defer is not a review decision, so being a review earns no exemption.

    SYRD-71 gives the Director defer control from any stage and prohibits only
    forged sign-offs. Deferring raises no sign-off and clears none, so a tenant
    must not lose the control merely because a stage is a gate. Whether anybody
    is mid-judgement is decided by whoever uses it, not by the document.
    """
    cfg = validate(copy.deepcopy(CANONICAL))
    reviews = [s["name"] for s in cfg["stages"] if s["kind"] == "review" and not s["terminal"]]
    assert reviews, cfg["stages"]
    for stage in reviews:
        defer = [x for x in cfg["transitions"] if x["from"] == stage and x["to"] == "backlog"]
        assert defer, stage
        assert defer[0]["actors"] == ["director"], defer
        assert defer[0]["clear_signoffs"] == [], defer
        assert defer[0]["primitive"] == "move", defer


# --- the migration's place in the sequence ---------------------------------


def migration_numbers() -> dict[int, list[str]]:
    numbers: dict[int, list[str]] = {}
    for path in sorted(MIGRATION_PATH.parent.glob("pgu*.sql")):
        numbers.setdefault(int(path.name[3:].split("_", 1)[0]), []).append(path.name)
    return numbers


def test_the_migration_is_numbered_above_every_migration_that_exists() -> None:
    """A new migration may not reuse or undercut a number already in the tree.

    The runner applies every file it has not recorded, in filename order. A
    tenant that already ran a higher-numbered migration will still apply a new
    lower-numbered one, out of order, whenever it arrives -- so the number is
    not decoration, it is the only thing that keeps a fresh install applying
    these in the same order an upgraded tenant did.
    """
    numbers = migration_numbers()
    mine = int(MIGRATION_PATH.name[3:].split("_", 1)[0])
    # Older numbers were reused more than once in this tree's history, so this
    # asks only about the one being added: it shares its number with nothing,
    # and nothing sorts after it.
    assert numbers[mine] == [MIGRATION_PATH.name], numbers[mine]
    assert mine == max(numbers), (mine, max(numbers), numbers[max(numbers)])


def test_the_migration_carries_the_schema_definition_of_every_function_it_creates() -> None:
    """A copy older than the schema it ships beside is a silent rollback.

    An upgraded tenant applies only the migrations it has not recorded. If this
    file re-created a function from a definition predating another migration the
    tenant already ran, that other migration's work would be undone with no
    error anywhere. So every body here is checked against schema.sql, and the
    two capabilities most recently added to these functions are named outright
    so the check cannot pass vacuously.
    """
    schema = (ROOT / "scripts/ticket_board/schema.sql").read_text()
    migration = MIGRATION_PATH.read_text()

    def bodies(text: str, name: str) -> list[str]:
        marker = f"CREATE OR REPLACE FUNCTION ticket_board.{name}("
        starts = [i for i in range(len(text)) if text.startswith(marker, i)]
        return [text[start : text.index("\n$$;", start) + 4] for start in starts]

    created = sorted(
        {
            line.split("ticket_board.")[1].split("(")[0]
            for line in migration.splitlines()
            if line.startswith("CREATE OR REPLACE FUNCTION ticket_board.")
        }
    )
    assert created, migration[:200]
    for name in created:
        in_schema = bodies(schema, name)
        assert in_schema, f"{name} is created by the migration but not by schema.sql"
        assert bodies(migration, name)[-1] == in_schema[-1], name

    # SYRD-83's director edit lives in both of these functions, and this
    # migration re-creates both.
    assert "director_edit_target" in bodies(migration, "enforce_declared_ticket_update")[-1]
    assert "'director_edit'" in bodies(migration, "validate_declared_workflow")[-1]


def main() -> int:
    with temporary_cluster(prefix="syrd92-defer-", shutdown="immediate") as cluster:
        for test in (
            test_the_canonical_workflow_keeps_somewhere_to_defer_to,
            test_cancel_is_not_a_substitute_for_somewhere_to_put_work_down,
            test_a_review_stage_is_not_exempt_from_the_floor,
            test_the_migration_is_numbered_above_every_migration_that_exists,
            test_the_migration_carries_the_schema_definition_of_every_function_it_creates,
        ):
            test()
        run_database_checks(cluster)
    print("ticket_board_director_defer_backlog_test: ok")
    return 0


def board(cluster, db: str, document: dict):
    admin = t.conninfo(cluster.socket_dir, cluster.port, db)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
    t.psql(admin, t.SCHEMA_PATH.read_text())
    try:
        t.create_roles(admin)  # cluster-wide, so the second board in this cluster finds them
    except AssertionError as exc:
        if "already exists" not in str(exc):
            raise
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
    healthy = bool(parking_stage_names(document))
    if healthy:
        with app._pg_connect() as conn:
            app._pg_set_caller_role(conn, "director")
            conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
            conn.execute("SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(document),))
            conn.commit()
    else:
        # A tenant like live syrd revision 22 cannot be created through the
        # supported path any more -- that is the point of this change. It is
        # seeded the way it actually exists: a document stored before the
        # validator that would now refuse it, with the stage and transition
        # tables projected from it.
        seed_stored_document(admin, document)
    return app, admin


def seed_stored_document(admin: str, document: dict) -> None:
    payload = json.dumps(document).replace("'", "''")
    stages = "".join(
        "INSERT INTO ticket_board.workflow_stages"
        "(name,display_label,rank,owner_roles,entry_gate_field,gate_skip_to,exit_signoff_field,is_terminal) VALUES ("
        f"'{stage['name']}','{stage['label']}',{rank},"
        "ARRAY[" + ",".join(f"'{owner}'" for owner in stage["owners"]) + "]::text[],"
        + (f"'{stage['gate']}'" if stage["gate"] else "NULL") + ","
        + (f"'{stage['skip_to']}'" if stage["skip_to"] else "NULL") + ","
        + (f"'{stage['signoff']}'" if stage["signoff"] else "NULL") + ","
        + ("true" if stage["terminal"] else "false") + ") ON CONFLICT(name) DO UPDATE SET "
        "display_label=EXCLUDED.display_label,rank=EXCLUDED.rank,owner_roles=EXCLUDED.owner_roles,"
        "entry_gate_field=EXCLUDED.entry_gate_field,gate_skip_to=EXCLUDED.gate_skip_to,"
        "exit_signoff_field=EXCLUDED.exit_signoff_field,is_terminal=EXCLUDED.is_terminal;"
        for rank, stage in enumerate(document["stages"])
    )
    transitions = "".join(
        "INSERT INTO ticket_board.workflow_transitions"
        "(from_stage,to_stage,action_name,allowed_roles,owner_scoped,director_override) VALUES ("
        f"'{tr['from']}','{tr['to']}','{tr['action']}',"
        "ARRAY[" + ",".join(f"'{actor}'" for actor in tr["actors"]) + "]::text[],"
        + ("true" if tr["owner_scoped"] else "false") + ",false);"
        for tr in document["transitions"]
    )
    roles = "".join(
        "INSERT INTO ticket_board.workflow_roles(name,definition) VALUES "
        f"('{role['name']}','" + json.dumps(role).replace("'", "''") + "'::jsonb) "
        "ON CONFLICT(name) DO UPDATE SET definition=EXCLUDED.definition;"
        for role in document["roles"]
    )
    t.psql(
        admin,
        "BEGIN;\n"
        "DELETE FROM ticket_board.workflow_transitions;\n"
        f"DELETE FROM ticket_board.workflow_stages WHERE name NOT IN ("
        + ",".join(f"'{stage['name']}'" for stage in document["stages"])
        + ");\n"
        + roles
        + stages
        + transitions
        + f"INSERT INTO ticket_board.workflow_revisions(actor,document) VALUES ('seed','{payload}'::jsonb);\n"
        "INSERT INTO ticket_board.workflow_configuration(singleton,revision,document) "
        f"VALUES (true,(SELECT max(revision) FROM ticket_board.workflow_revisions),'{payload}'::jsonb) "
        "ON CONFLICT(singleton) DO UPDATE SET revision=EXCLUDED.revision,document=EXCLUDED.document;\n"
        "COMMIT;",
    )


def run_database_checks(cluster) -> None:
    # --- an existing tenant is repaired in place ---------------------------
    app, admin = board(cluster, "syrd92_upgrade", degraded(CANONICAL))
    before = app.workflow_document()["document"]
    assert not parking_stage_names(before), before["stages"]
    assert not [x for x in before["transitions"] if x["action"] == "defer"], before["transitions"]

    t.seed_postgres_ticket(
        admin,
        "PGU-9",
        title="Survives the repair",
        state="audit",
        assignee="audit",
        audit_signoff=True,
        commit_exempt=True,
    )

    t.psql(admin, "BEGIN;\n" + MIGRATION + "\nCOMMIT;")
    after = app.workflow_document()["document"]
    assert parking_stage_names(after) == {"backlog"}, after["stages"]
    active_stages = {s["name"] for s in after["stages"] if not s["terminal"] and s["name"] != "backlog"}
    assert {x["from"] for x in after["transitions"] if x["action"] == "defer"} == active_stages
    # The tenant's own serial-focus holding destination is not rewritten.
    assert after["queue"] == before["queue"], (before["queue"], after["queue"])
    # The repair withdraws the removal that would undo it on the next apply.
    assert "backlog" not in after["remove_stages"], after["remove_stages"]
    assert validate(copy.deepcopy(after)), "the repaired document must satisfy the floor"
    # Nothing else moved: sign-offs, stages and unrelated tickets are untouched.
    assert ticket_row(admin, "PGU-9")["audit_signoff"] is True, ticket_row(admin, "PGU-9")
    assert ticket_row(admin, "PGU-9")["state"] == "audit"
    assert [s["name"] for s in before["stages"]] == [
        s["name"] for s in after["stages"] if s["name"] != "backlog"
    ]

    revision = app.workflow_document()["revision"]
    t.psql(admin, "BEGIN;\n" + MIGRATION + "\nCOMMIT;")
    assert app.workflow_document()["revision"] == revision, "the repair must be idempotent"

    run_deferral_checks(app, admin)

    # --- a freshly provisioned board already has it ------------------------
    fresh_app, fresh_admin = board(cluster, "syrd92_fresh", CANONICAL)
    fresh = fresh_app.workflow_document()["document"]
    assert parking_stage_names(fresh) == {"backlog"}, fresh["stages"]
    t.psql(fresh_admin, "BEGIN;\n" + MIGRATION + "\nCOMMIT;")
    assert fresh_app.workflow_document()["document"] == fresh, "a healthy tenant must not be rewritten"

    run_fresh_board_checks(fresh_app, fresh_admin)
    run_upgrade_history_checks(cluster)


def run_fresh_board_checks(app, admin: str) -> None:
    # --- the database refuses to install a document without one -------------
    #
    # Checked in PostgreSQL, not in the Python validator in front of it: an
    # apply that reaches the database another way is where revision 22 came
    # from, so this is the layer that has to say no.
    no_defer = copy.deepcopy(CANONICAL)
    no_defer["transitions"] = [
        x for x in no_defer["transitions"] if not (x["from"] == "audit" and x["to"] == "backlog")
    ]
    no_way_back = copy.deepcopy(CANONICAL)
    no_way_back["transitions"] = [
        x for x in no_way_back["transitions"] if not (x["from"] == "backlog" and x["to"] == "analysis")
    ]
    for document, expected in (
        (degraded(CANONICAL), "stage where deferred work can wait"),
        (no_defer, "defer work out of every active stage"),
        (no_way_back, "ordinary way back"),
    ):
        document = copy.deepcopy(document)
        document["project"] = "cerulean"
        document.setdefault("reassign", {})
        document.setdefault("remove_stages", [])
        with app._pg_connect() as conn:
            app._pg_set_caller_role(conn, "director")
            conn.execute("SELECT set_config('ticket_board.project','cerulean',false)")
            rejected(
                lambda conn=conn, document=document: conn.execute(
                    "SELECT ticket_board.apply_declared_workflow(%s::jsonb)", (json.dumps(document),)
                ),
                expected,
            )
            conn.rollback()

    # --- serial-focus overflow is not a deferral ----------------------------
    #
    # This board's holding destination is the parking stage itself, so the two
    # meet. A ticket redirected there is waiting on a busy implementer, not put
    # down by anyone: it keeps the bookkeeping that says who it is queued for,
    # and it is not marked parked.
    t.seed_postgres_ticket(admin, "PGU-30", title="Reserved work", state="in_progress", assignee="app")
    t.seed_postgres_ticket(admin, "PGU-31", title="Second for the same hand", state="analysis", assignee="director")
    assert t.psql(admin, "SELECT ticket_board.ticket_current_reserved_ticket('app');") == "PGU-30"

    routed = app.perform_workflow_action("PGU-31", "route", {"assignee": "app"}, caller_role="director")
    assert routed["state"] == "backlog", routed
    queued = ticket_row(admin, "PGU-31")
    assert queued["queued_for_assignee"] == "app", queued
    assert queued["parked"] is False, queued


# --- the two upgrade histories ---------------------------------------------


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout


def schema_before(migration_name: str) -> str:
    """schema.sql as it stood before `migration_name` was added to the tree."""
    adding = git(
        "log", "--format=%H", "--diff-filter=A", "--",
        f"scripts/ticket_board/migrations/{migration_name}",
    ).strip().splitlines()
    assert adding, migration_name
    return git("show", f"{adding[-1]}^:scripts/ticket_board/schema.sql")


def run_migration_runner(cluster, db: str) -> str:
    """The shipped runner, not a reimplementation of it.

    Which migrations a tenant applies, and in what order, is this script's
    decision; asking it directly is the only way the ordering under test is the
    real one.
    """
    proc = subprocess.run(
        [str(ROOT / "scripts/ticket-board-migrate")],
        text=True,
        capture_output=True,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "TICKET_BOARD_ADMIN_DATABASE_URL": t.conninfo(cluster.socket_dir, cluster.port, db),
        },
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout
    return proc.stderr


def pre_record_migrations(admin: str, *, applying: set[str]) -> None:
    """Say which migrations this tenant has already run.

    Everything in the tree except `applying`, so the runner is left with exactly
    the upgrade under test rather than replaying the whole history.
    """
    names = [path.name for path in sorted(MIGRATION_PATH.parent.glob("*.sql")) if path.name not in applying]
    assert names and applying <= {path.name for path in MIGRATION_PATH.parent.glob("*.sql")}
    values = ",".join(f"('{name}')" for name in names)
    t.psql(
        admin,
        "CREATE TABLE IF NOT EXISTS ticket_board.schema_migrations ("
        "name text PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now());\n"
        f"INSERT INTO ticket_board.schema_migrations(name) VALUES {values} ON CONFLICT DO NOTHING;",
    )


def installed_body(admin: str, function: str) -> str:
    return t.psql(
        admin,
        "SELECT string_agg(p.prosrc, E'\\n') FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
        f"WHERE n.nspname = 'ticket_board' AND p.proname = '{function}';",
    )


def assert_current_floor_and_director_edit(admin: str, label: str) -> None:
    """Both capabilities survive the upgrade, whichever way the tenant got here."""
    guard = installed_body(admin, "enforce_declared_ticket_update")
    validator = installed_body(admin, "validate_declared_workflow")
    # SYRD-83's generic Director edit: the window in the guard, and the
    # capability name the role vocabulary has to keep accepting.
    assert "director_edit_target" in guard, label
    assert "'director_edit'" in validator, label
    # SYRD-82's floor, which pgu927 installed and nothing here may undo.
    assert "director must be able to move work out of every stage" in validator, label
    # SYRD-92's own three rules.
    for rule in (
        "workflow must keep a stage where deferred work can wait",
        "director must be able to defer work out of every active stage",
        "deferred work must have an ordinary way back",
    ):
        assert rule in validator, (label, rule)
    # Not just present as text: the validator actually refuses a degraded
    # document, and accepts a role that declares the director_edit capability.
    document = copy.deepcopy(degraded(CANONICAL))
    document["project"] = "cerulean"
    rejected(
        lambda: t.psql(admin, f"SELECT ticket_board.validate_declared_workflow('{json.dumps(document).replace(chr(39), chr(39) * 2)}'::jsonb);"),
        "stage where deferred work can wait",
    )
    with_edit = copy.deepcopy(CANONICAL)
    with_edit["project"] = "cerulean"
    with_edit.setdefault("reassign", {})
    with_edit.setdefault("remove_stages", [])
    for role in with_edit["roles"]:
        if role["name"] == "director" and "director_edit" not in role.get("capabilities", []):
            role.setdefault("capabilities", []).append("director_edit")
    t.psql(
        admin,
        f"SELECT ticket_board.validate_declared_workflow('{json.dumps(with_edit).replace(chr(39), chr(39) * 2)}'::jsonb);",
    )


def run_upgrade_history_checks(cluster) -> None:
    """Both ways a tenant can arrive here end in the same place.

    This migration is numbered last, but an already-upgraded tenant does not
    replay the sequence -- it applies only what it has not recorded. So the two
    histories that matter are a tenant that runs the ordered tail from before
    the director edit existed, and a tenant that already has the director edit
    and runs this one alone. A stale copy inside this file would show up as the
    director edit missing from the second, and out-of-order numbering as a
    difference between the two.
    """
    histories = {
        "ordered-tail": (
            schema_before("pgu928_syrd83_director_edit.sql"),
            {"pgu928_syrd83_director_edit.sql", MIGRATION_PATH.name},
        ),
        "pgu928-tenant": (t.SCHEMA_PATH.read_text(), {MIGRATION_PATH.name}),
    }
    for label, (schema, applying) in histories.items():
        db = f"syrd92_{label.replace('-', '_')}"
        admin = t.conninfo(cluster.socket_dir, cluster.port, db)
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", db])
        t.psql(admin, schema)
        try:
            t.create_roles(admin)
        except AssertionError as exc:
            if "already exists" not in str(exc):
                raise
        document = copy.deepcopy(degraded(CANONICAL))
        document["project"] = "cerulean"
        document.setdefault("reassign", {})
        document.setdefault("remove_stages", [])
        seed_stored_document(admin, document)
        pre_record_migrations(admin, applying=applying)

        run_migration_runner(cluster, db)
        # Grants come after, because the older schema has nothing to grant on
        # until the migrations that add it have run -- which is the ordering a
        # real upgrade uses.
        t.psql(admin, t.RBAC_PATH.read_text())
        assert_current_floor_and_director_edit(admin, label)
        stored = json.loads(t.psql(admin, "SELECT document::text FROM ticket_board.workflow_configuration WHERE singleton;"))
        assert parking_stage_names(stored) == {"backlog"}, (label, stored["stages"])
        active_stages = {s["name"] for s in stored["stages"] if not s["terminal"] and s["name"] != "backlog"}
        assert {x["from"] for x in stored["transitions"] if x["action"] == "defer"} == active_stages, label

        revision = t.psql(admin, "SELECT revision::text FROM ticket_board.workflow_configuration WHERE singleton;")
        applied = t.psql(admin, "SELECT count(*)::text FROM ticket_board.schema_migrations;")
        run_migration_runner(cluster, db)
        assert t.psql(admin, "SELECT revision::text FROM ticket_board.workflow_configuration WHERE singleton;") == revision, label
        assert t.psql(admin, "SELECT count(*)::text FROM ticket_board.schema_migrations;") == applied, label
        assert_current_floor_and_director_edit(admin, f"{label} (reapplied)")


def run_deferral_checks(app, admin: str) -> None:
    """What a deferral does, on the repaired board."""
    t.seed_postgres_ticket(admin, "PGU-10", title="Triage work", state="analysis", assignee="director")
    t.seed_postgres_ticket(admin, "PGU-11", title="Future work", state="draft", assignee="director")

    # --- analysis -> backlog, the SYRD-40 shape ---------------------------
    deferred = app.perform_workflow_action("PGU-10", "defer", {}, caller_role="director")
    assert deferred["state"] == "backlog", deferred
    row = ticket_row(admin, "PGU-10")
    assert row["assignee"] == "unassigned", row
    assert row["parked"] is True, row
    assert row["queued_for_assignee"] == "", row
    assert row["title"] == "Triage work", row
    # Nobody is holding it, so nothing highlights it as current work.
    assert deferred["active_work_highlight"] is False, deferred
    assert deferred["active_work_owner_role"] == "", deferred

    # --- draft -> backlog, the SYRD-37 shape, without cancelling ----------
    parked_draft = app.perform_workflow_action("PGU-11", "defer", {}, caller_role="director")
    assert parked_draft["state"] == "backlog", parked_draft
    draft_row = ticket_row(admin, "PGU-11")
    assert draft_row["assignee"] == "unassigned" and draft_row["parked"] is True, draft_row

    # --- deferring a review keeps the review's own record -----------------
    #
    # The control the Director gets here is deferral, not judgement. A ticket
    # parked out of a review keeps every sign-off already given, its gates and
    # its comments; only who holds it changes. That is exactly why a review is
    # not exempt from the floor: nothing about the defer touches the review.
    t.seed_postgres_ticket(
        admin,
        "PGU-13",
        title="Half-judged",
        state="audit",
        assignee="audit",
        audit_signoff=True,
        commit_exempt=True,
    )
    comments_before = t.psql(
        admin, "SELECT count(*) FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-13';"
    )
    review_row = ticket_row(admin, "PGU-13")
    parked_review = app.perform_workflow_action("PGU-13", "defer", {}, caller_role="director")
    assert parked_review["state"] == "backlog", parked_review
    after_review = ticket_row(admin, "PGU-13")
    assert after_review["assignee"] == "unassigned" and after_review["parked"] is True, after_review
    assert after_review["audit_signoff"] is True, after_review
    assert after_review["title"] == review_row["title"], after_review
    assert (
        t.psql(admin, "SELECT count(*) FROM ticket_board.ticket_comments WHERE ticket_id = 'PGU-13';")
        == comments_before
    )
    for flag in ("inspector_signoff", "user_signoff"):
        raised = t.psql(admin, f"SELECT {flag}::text FROM ticket_board.tickets WHERE id = 'PGU-13';")
        assert raised == "false", (flag, raised)

    # --- deferring implementation work releases the reservation -----------
    #
    # Serial focus reserves the one ticket an implementer is on. Putting that
    # ticket down has to hand the slot back, or the implementer stays blocked by
    # work nobody is doing -- the cost that made cancel the only usable exit.
    t.seed_postgres_ticket(admin, "PGU-12", title="App work", state="in_progress", assignee="app")
    assert t.psql(admin, "SELECT ticket_board.ticket_current_reserved_ticket('app');") == "PGU-12"
    parked_impl = app.perform_workflow_action("PGU-12", "defer", {}, caller_role="director")
    assert parked_impl["state"] == "backlog", parked_impl
    impl_row = ticket_row(admin, "PGU-12")
    assert impl_row["assignee"] == "unassigned" and impl_row["parked"] is True, impl_row
    assert impl_row["queued_for_assignee"] == "", impl_row
    assert t.psql(admin, "SELECT coalesce(ticket_board.ticket_current_reserved_ticket('app'),'');") == ""

    # --- revival is an ordinary action, and unparks --------------------
    revived = app.perform_workflow_action("PGU-10", "route", {}, caller_role="director")
    assert revived["state"] == "analysis", revived
    revived_row = ticket_row(admin, "PGU-10")
    assert revived_row["parked"] is False, revived_row
    assert revived_row["assignee"] == "director", revived_row

    # --- nobody else may defer -------------------------------------------
    for caller in ("app", "ops", "main", "audit", "inspector", "user"):
        before = ticket_row(admin, "PGU-10")
        rejected(lambda caller=caller: app.perform_workflow_action("PGU-10", "defer", {}, caller_role=caller))
        assert ticket_row(admin, "PGU-10") == before, (caller, ticket_row(admin, "PGU-10"))

    run_socket_checks(app, admin)


def run_socket_checks(app, admin: str) -> None:
    """The same two answers over the real Unix socket, through the real handler.

    The reproduction on the ticket is a CLI call, and the CLI reaches the board
    this way. Whether a role may defer is decided by the peer the kernel
    reports, so the authorization under test has to be the socket's, not a
    caller_role a fixture passes in.
    """
    import http.client
    import socket as socket_module
    import tempfile

    class UnixConnection(http.client.HTTPConnection):
        def __init__(self, path: str) -> None:
            super().__init__("localhost")
            self._path = path

        def connect(self) -> None:
            connection = socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM)
            connection.connect(self._path)
            self.sock = connection

    def call(socket_path: Path, ticket_id: str) -> tuple[int, str]:
        connection = UnixConnection(str(socket_path))
        try:
            connection.request(
                "POST",
                f"/api/tickets/{ticket_id}/actions/defer",
                body="{}",
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            return response.status, response.read().decode("utf-8", errors="replace")
        finally:
            connection.close()

    def serve(role: str, tmp: str):
        socket_path = Path(tmp) / f"board-{role}.sock"
        server = t.TicketBoardUnixServer(
            socket_path,
            app,
            events=t.TicketBoardEventHub(app),
            director_notifier=t.QuietNotifier(),
            role_authority=t.local_role_authority_as(role),
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return socket_path, server, thread

    t.seed_postgres_ticket(admin, "PGU-20", title="Socket triage", state="analysis", assignee="director")

    with tempfile.TemporaryDirectory(prefix="syrd92-defer-socket.") as tmp:
        for role in ("app", "ops", "audit", "inspector", "user"):
            socket_path, server, thread = serve(role, tmp)
            try:
                before = ticket_row(admin, "PGU-20")
                status, body = call(socket_path, "PGU-20")
                assert status in (400, 403), (role, status, body)
                assert "cannot call defer" in body or "cannot call" in body, (role, body)
                assert ticket_row(admin, "PGU-20") == before, (role, ticket_row(admin, "PGU-20"))
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)

        socket_path, server, thread = serve("director", tmp)
        try:
            status, body = call(socket_path, "PGU-20")
            assert status == 200, (status, body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    parked = ticket_row(admin, "PGU-20")
    assert parked["state"] == "backlog", parked
    assert parked["assignee"] == "unassigned" and parked["parked"] is True, parked


if __name__ == "__main__":
    raise SystemExit(main())
