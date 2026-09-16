#!/usr/bin/env python3
"""SYRD-164: replaying the one-time workflow seed on a board that is running.

The c383655 rollout for SYRD-146 ran the regenerated operator packet for an
EXISTING tenant, exactly as the supported recovery describes. Units, ACLs,
database and migrations all applied, and then the packet ran the project
workflow SQL, which refused:

    psql:<stdin>:11: ERROR:  project workflow seed must run before tickets exist

recorded at /var/lib/switchyard/rollout/syrd/0050-20260916T005058Z. The refusal
was right. That file deletes the stages and transitions a board has and
installs the project's own, which is what a board being brought up needs and
the last thing a running one does: its tickets sit in those stages, its roles
hold runtime assignments against them, and its history names transitions that
would be deleted and re-created underneath it.

So the packet claimed every phase was re-runnable while carrying one phase that
was not, and a registered tenant could not be upgraded through the supported
path at all.

The boundary is explicit now. An established board -- one with tickets, or one
carrying a declared workflow document -- is left exactly as it is and the run
continues to the phases that are repairs. The guard is untouched and still
there; nothing in the supported path reaches it, which is what these cases
check by reaching it deliberately.

Everything here runs the real files against a real cluster, in the order the
packet runs them.
"""

from __future__ import annotations

import json
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

from scripts.ticket_board.project_provision import build_plan, render_workflow_sql  # noqa: E402

SCHEMA = (ROOT / "scripts" / "ticket_board" / "schema.sql").read_text(encoding="utf-8")
RBAC = (ROOT / "scripts" / "ticket_board" / "rbac.sql").read_text(encoding="utf-8")
MIGRATE = ROOT / "scripts" / "ticket-board-migrate"

PROJECT = "testing"
TENANT = "testing-agent"
IMPLEMENTERS = ("main", "ops")

#: The guard this ticket says not to weaken, quoted so a change to it fails here.
SEED_GUARD = "RAISE EXCEPTION 'project workflow seed must run before tickets exist'"


def workflow_sql() -> str:
    plan = build_plan(project=PROJECT, owner_user=TENANT, implementer_roles=IMPLEMENTERS)
    return render_workflow_sql(plan)


def database(cluster, name: str) -> str:
    admin = t.conninfo(cluster.socket_dir, cluster.port, name)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", name])
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


def packet_database_phases(cluster, name: str, admin: str) -> None:
    """The packet's database phases, in the order the packet runs them."""
    t.psql(admin, SCHEMA)
    migrate(cluster, name)
    t.psql(admin, workflow_sql())
    t.psql(admin, RBAC)


def board_state(admin: str) -> dict[str, str]:
    """Everything a replay must leave exactly as it was."""
    return {
        "stages": t.psql(
            admin,
            "SELECT name, display_label, rank, owner_roles, entry_gate_field, gate_skip_to, "
            "exit_signoff_field, is_terminal FROM ticket_board.workflow_stages ORDER BY rank",
        ),
        "transitions": t.psql(
            admin,
            "SELECT from_stage, to_stage, action_name, allowed_roles, owner_scoped, "
            "director_override FROM ticket_board.workflow_transitions ORDER BY 1, 2, 3",
        ),
        "tickets": t.psql(
            admin,
            "SELECT id, state, assignee, title FROM ticket_board.tickets ORDER BY id",
        ),
        "comments": t.psql(
            admin,
            "SELECT ticket_id, position, who, text FROM ticket_board.ticket_comments "
            "ORDER BY ticket_id, position",
        ),
        "runtimes": t.psql(
            admin,
            "SELECT role, runtime, actual_target, generation FROM "
            "ticket_board.role_runtime_assignments ORDER BY role",
        ),
        "constraint": t.psql(
            admin,
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'workflow_stages_name_check'",
        ),
    }


def establish(admin: str, commit_hash: str) -> None:
    """Make it a board somebody is using: a ticket, a comment, a live runtime."""
    t.seed_postgres_ticket(
        admin,
        "TESTING-1",
        title="a ticket that already exists",
        state="in_progress",
        assignee="main",
    )
    t.psql(
        admin,
        "INSERT INTO ticket_board.ticket_comments "
        "(ticket_id, position, ts, ts_text, who, text, source_json) VALUES "
        "('TESTING-1', 1, now(), '2026-09-16T00:50:00+00:00', 'director', "
        "'routed for implementation', "
        "'{\"who\": \"director\", \"ts\": \"2026-09-16T00:50:00+00:00\", "
        "\"text\": \"routed for implementation\"}'::jsonb)",
    )
    t.psql(
        admin,
        "INSERT INTO ticket_board.workflow_roles (name, definition) VALUES "
        "('main', '{\"name\": \"main\", \"kind\": \"implementer\"}'::jsonb) "
        "ON CONFLICT (name) DO NOTHING",
    )
    t.psql(
        admin,
        "INSERT INTO ticket_board.role_runtime_assignments "
        "(role, runtime, actual_target, worktree, session_dir, process_pid, "
        " process_start_time, process_uid) VALUES "
        "('main', 'claude', 'testing-main:0.0', '/home/testing-agent/worktrees/main', "
        " '/home/testing-agent/.local/state/testing-ticket-board/pane-sessions', 4242, 99, 1013)",
    )


def refused(admin: str, sql: str, expected: str) -> None:
    try:
        t.psql(admin, sql)
    except Exception as exc:  # noqa: BLE001 - psql raises its own type
        assert expected in str(exc), (expected, str(exc)[-400:])
        return
    raise AssertionError(f"expected the database to refuse this, naming {expected!r}")


def cases(cluster) -> int:
    checks = 0
    commit_hash = t.main_commit()

    # 1. A board being brought up: the seed is the initial seed, and it runs.
    admin = database(cluster, "syrd164_running")
    packet_database_phases(cluster, "syrd164_running", admin)
    assert t.psql(admin, "SELECT count(*) FROM ticket_board.workflow_stages").strip() == "9"
    assert t.psql(admin, "SELECT count(*) FROM ticket_board.workflow_transitions").strip() == "37"
    checks += 1

    # 2. Then somebody uses it: a ticket in a stage, a comment, a live runtime
    #    assignment. This is the shape journal 0050 met.
    establish(admin, commit_hash)
    before = board_state(admin)
    assert "TESTING-1|in_progress|main" in before["tickets"], before["tickets"]
    assert "main|claude|testing-main:0.0" in before["runtimes"], before["runtimes"]
    checks += 1

    # 3. The supported packet runs again, whole, and finishes.
    packet_database_phases(cluster, "syrd164_running", admin)
    after = board_state(admin)
    checks += 1

    # 4. And it changed nothing it was not supposed to: the workflow rows, the
    #    ticket, its history, the runtime assignment and the constraint are all
    #    exactly what they were.
    assert after == before, [key for key in before if before[key] != after[key]]
    checks += 1

    # 5. A third run is the same answer again -- there is no state here that
    #    only survives one replay.
    packet_database_phases(cluster, "syrd164_running", admin)
    assert board_state(admin) == before
    checks += 1

    # 6. The guard was not weakened. It is still in the rendered file, and it
    #    still refuses: run the same SQL with the boundary removed against this
    #    board, and the thing standing between it and the seed speaks.
    rendered = workflow_sql()
    assert SEED_GUARD in rendered, rendered[:600]
    # Reached by dropping the boundary's early return, so what runs is this
    # file's own guard rather than a rewritten copy of it. Independent of how
    # the boundary decides -- that is case 7's question, not this one.
    reaches_the_guard = rendered.replace("        RETURN;\n", "", 1)
    assert reaches_the_guard != rendered, "the boundary no longer returns; this case must be updated"
    refused(admin, reaches_the_guard, "project workflow seed must run before tickets exist")
    assert board_state(admin) == before, "a refused seed must change nothing"
    checks += 1

    # 7. A board with a declared workflow and no tickets is established too.
    #    Replaying the default seed over a declared workflow would replace it
    #    silently, which raises nothing at all.
    declared = database(cluster, "syrd164_declared")
    packet_database_phases(cluster, "syrd164_declared", declared)
    t.psql(
        declared,
        "INSERT INTO ticket_board.workflow_configuration (singleton, revision, document) "
        "VALUES (true, 1, '{\"schema\": \"switchyard.workflow.v1\"}'::jsonb)",
    )
    t.psql(
        declared,
        "UPDATE ticket_board.workflow_stages SET display_label = 'Build' WHERE name = 'in_progress'",
    )
    declared_before = board_state(declared)
    t.psql(declared, workflow_sql())
    assert board_state(declared) == declared_before, "a declared workflow is not reseeded"
    assert "in_progress|Build" in board_state(declared)["stages"]
    checks += 1

    return checks


def main() -> int:
    with temporary_cluster(prefix="syrd164-workflow-seed-", shutdown="immediate") as cluster:
        checks = cases(cluster)
    print(f"workflow_seed_replay_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
