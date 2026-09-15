#!/usr/bin/env python3
"""SYRD-160: replaying the base schema over a board that has its own workflow.

SYRD-146 resumed the preserved partial testing tenant. The regenerated packet
passed its confinement and authority gates and then died in testing rollout
journal 0022, replaying the base Board SQL:

    ERROR:  new row for relation "workflow_stages" violates check constraint
            "workflow_stages_name_check"
    DETAIL:  Failing row contains (backlog, Backlog, 1, {}, null, null, null, f).

The constraint was right. A provisioned project replaces the built-in stages
with its own and narrows `workflow_stages_name_check` to the names it declares;
testing's board has nine stages and `backlog` is not one of them. `schema.sql`
seeded the built-in eleven unconditionally, so the supported recovery could not
run twice.

The quieter half of the same defect is what happens where the names DO match.
The seed's `ON CONFLICT DO UPDATE` would have reset the labels, ranks and
owner_roles of every stage a project happens to share with the built-in set --
testing's `in_progress` is owned by `main` and `ops`, the built-in one by five
roles -- so a board that replayed successfully would have had its workflow
silently rewritten. That is covered here too, because it raises no error at all.

So the seed now establishes a workflow for a board that has none and leaves a
configured board alone; changes to the built-in workflow reach existing boards
through migrations, which is how `dat` arrived. Nothing is dropped, widened or
relaxed: the tests below insert `backlog` directly afterwards and require the
constraint to refuse it.

Every case runs the real files against a real cluster -- `schema.sql`, the real
migration runner, the generated project workflow SQL and `rbac.sql`, in the
order the operator packet runs them -- because the defect was an interaction
between them that no rendering check could see.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

from scripts.ticket_board.project_provision import (  # noqa: E402
    build_plan,
    render_workflow_sql,
)

SCHEMA = (ROOT / "scripts" / "ticket_board" / "schema.sql").read_text(encoding="utf-8")
RBAC = (ROOT / "scripts" / "ticket_board" / "rbac.sql").read_text(encoding="utf-8")
MIGRATE = ROOT / "scripts" / "ticket-board-migrate"

#: The tenant this regression is about, with the role set its plan records.
PROJECT = "testing"
TENANT = "testing-agent"
IMPLEMENTERS = ("main", "ops")


def project_workflow_sql() -> str:
    plan = build_plan(project=PROJECT, owner_user=TENANT, implementer_roles=IMPLEMENTERS)
    return render_workflow_sql(plan)


def database(cluster, name: str) -> str:
    admin = t.conninfo(cluster.socket_dir, cluster.port, name)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", name])
    # The service and listener roles the packet's database SQL creates before
    # any of this runs; the migrations grant to them by name. Cluster-wide, so
    # the second database in this cluster finds them already there.
    for role in ("ticket_board_service", "ticket_board_listener"):
        t.psql(
            admin,
            f"DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') "
            f"THEN CREATE ROLE {role} LOGIN; END IF; END $$;",
        )
    return admin


def migrate(cluster, name: str) -> None:
    url = f"postgresql://postgres@/{name}?host={cluster.socket_dir}&port={cluster.port}"
    done = subprocess.run(
        [str(MIGRATE)],
        env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": url},
        capture_output=True,
        text=True,
    )
    assert done.returncode == 0, (done.stdout[-2000:], done.stderr[-2000:])


def provisioning_pass(cluster, name: str, admin: str) -> None:
    """The packet's database phases, in the order the packet runs them."""
    t.psql(admin, SCHEMA)
    migrate(cluster, name)
    t.psql(admin, project_workflow_sql())
    t.psql(admin, RBAC)


def workflow_state(admin: str) -> tuple[str, str, str]:
    return (
        t.psql(
            admin,
            "SELECT name, display_label, rank, owner_roles, entry_gate_field, gate_skip_to, "
            "exit_signoff_field, is_terminal FROM ticket_board.workflow_stages ORDER BY rank",
        ),
        t.psql(
            admin,
            "SELECT from_stage, to_stage, action_name, allowed_roles, owner_scoped, "
            "director_override FROM ticket_board.workflow_transitions ORDER BY 1, 2, 3",
        ),
        t.psql(
            admin,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'workflow_stages_name_check'",
        ),
    )


def refused(admin: str, sql: str, expected: str) -> None:
    try:
        t.psql(admin, sql)
    except Exception as exc:  # noqa: BLE001 - psql raises its own type
        assert expected in str(exc), (expected, str(exc)[-400:])
        return
    raise AssertionError(f"expected the database to refuse this, naming {expected!r}: {sql}")


def cases(cluster) -> int:
    checks = 0

    # 1. The live shape: a provisioned testing board, then the resumed packet
    #    running its database phases again. This is journal 0022.
    admin = database(cluster, "syrd160_resumed")
    provisioning_pass(cluster, "syrd160_resumed", admin)
    before = workflow_state(admin)
    assert "backlog" not in before[0], before[0]
    assert "in_progress|Implementation|2|{main,ops}" in before[0], before[0]

    provisioning_pass(cluster, "syrd160_resumed", admin)
    after = workflow_state(admin)
    checks += 1

    # 2. Nothing duplicated, nothing reset, nothing lost -- rows, transitions
    #    and the constraint itself are what they were.
    assert after == before, (before, after)
    assert t.psql(admin, "SELECT count(*) FROM ticket_board.workflow_stages").strip() == "9"
    assert t.psql(admin, "SELECT count(*) FROM ticket_board.workflow_transitions").strip() == "37"
    checks += 1

    # 3. The protection that produced the failure is still in force. It was not
    #    dropped, widened or relaxed to make the replay pass: the row from the
    #    journal is still refused when anything tries to insert it.
    refused(
        admin,
        "INSERT INTO ticket_board.workflow_stages "
        "(name, display_label, rank, owner_roles, entry_gate_field, gate_skip_to, "
        "exit_signoff_field, is_terminal) VALUES "
        "('backlog', 'Backlog', 1, ARRAY[]::text[], NULL, NULL, NULL, false)",
        "workflow_stages_name_check",
    )
    checks += 1

    # 4. A board with no workflow still gets the built-in one, which is what
    #    the seed is for. Eleven stages, `backlog` among them.
    fresh = database(cluster, "syrd160_fresh")
    t.psql(fresh, SCHEMA)
    migrate(cluster, "syrd160_fresh")
    assert t.psql(fresh, "SELECT count(*) FROM ticket_board.workflow_stages").strip() == "11"
    assert (
        t.psql(fresh, "SELECT count(*) FROM ticket_board.workflow_stages WHERE name = 'backlog'").strip()
        == "1"
    )
    builtin_transitions = t.psql(
        fresh, "SELECT count(*) FROM ticket_board.workflow_transitions"
    ).strip()
    assert int(builtin_transitions) > 0, builtin_transitions
    checks += 1

    # 5. Replaying it on that board changes nothing and duplicates nothing.
    built_in_before = workflow_state(fresh)
    t.psql(fresh, SCHEMA)
    assert workflow_state(fresh) == built_in_before
    assert t.psql(fresh, "SELECT count(*) FROM ticket_board.workflow_stages").strip() == "11"
    assert (
        t.psql(fresh, "SELECT count(*) FROM ticket_board.workflow_transitions").strip()
        == builtin_transitions
    )
    checks += 1

    # 6. The quiet half: a board whose stage names DO match the built-in ones
    #    keeps its own owner_roles and labels through a replay. This raises no
    #    error either way, which is exactly why it needs a test -- a board that
    #    replayed "successfully" used to have its workflow rewritten.
    t.psql(
        fresh,
        "UPDATE ticket_board.workflow_stages "
        "SET owner_roles = ARRAY['main']::text[], display_label = 'Build' "
        "WHERE name = 'in_progress'",
    )
    customised = workflow_state(fresh)
    t.psql(fresh, SCHEMA)
    assert workflow_state(fresh) == customised, (customised, workflow_state(fresh))
    assert "in_progress|Build|3|{main}" in workflow_state(fresh)[0], workflow_state(fresh)[0]
    checks += 1

    return checks


def main() -> int:
    with temporary_cluster(prefix="syrd160-workflow-replay-", shutdown="immediate") as cluster:
        checks = cases(cluster)
    print(f"ticket_board_resumed_workflow_replay_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
