#!/usr/bin/env python3
"""SYRD-654: a fresh tenant's declared workflow is seeded after the grants that let its seeder reach the schema.

Live on PIE, rollout attempt 0001-20261023T092946Z, and again on SYRD-642's
clean-host proof (SYRD-660): the root packet ran `<project>-workflow.sql`
before `rbac.sql`. A declared workflow's seed does `SET LOCAL ROLE
ticket_board_service` and calls `ticket_board.apply_declared_workflow`, and
that role reaches schema `ticket_board` only through rbac.sql's grants:

    psql:<stdin>:5: ERROR:  permission denied for schema ticket_board

The operator granted the schema by hand and replayed the packet.

Here the packet's own database phase runs as the packet prints it: the lines
between `systemd-tmpfiles --create` and `systemctl daemon-reload`, in their
rendered order, each through bash, against an empty throwaway PostgreSQL
cluster. They run the real rendered `<project>-database.sql` and
`<project>-workflow.sql` from `write_artifacts`, the tree's `schema.sql` and
`rbac.sql`, and the real `ticket-board-migrate`. Only `sudo` is stood in, by a
PATH stub that runs its command as this user (`-u postgres` becomes the
cluster's own superuser through PGUSER). The plan's admin URL is pointed at
the cluster, the one host-specific value in it. No grant is made by hand.
The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "ce520e912fd04c0c9c2db6070d6f7e3950f4b78d"  # main before SYRD-654
CHECKS = 0
SUDO = '#!/bin/bash\n# The packet\'s `sudo` and `sudo -u postgres`, as this user.\nif [ "$1" = "-u" ]; then shift 2; fi\nexec "$@"\n'
#: Every object's privileges in the board's schema, and the schema's own.
ACL_SNAPSHOT = """
SELECT coalesce(json_agg(row ORDER BY row), '[]')::text FROM (
  SELECT 'schema ' || nspname || ' ' || coalesce(nspacl::text, '') AS row FROM pg_namespace WHERE nspname = 'ticket_board'
  UNION ALL
  SELECT 'rel ' || c.relname || ' ' || coalesce(c.relacl::text, '') FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace WHERE n.nspname = 'ticket_board'
  UNION ALL
  SELECT 'fn ' || p.oid::regprocedure::text || ' ' || coalesce(p.proacl::text, '') FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'ticket_board'
) acl;
"""


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


def declared_document(root: Path, project: str) -> dict:
    """The example declared workflow, moved onto this test's project."""
    raw = (root / "examples" / "workflows" / "inspection.json").read_text(encoding="utf-8")
    source = json.loads(raw)["project"]
    return json.loads(raw.replace(f'"{source}-', f'"{project}-').replace(f'"project": "{source}"', f'"project": "{project}"'))


def role_names(document: dict) -> list[str]:
    return sorted(str(role.get("name") or role.get("role") or "") for role in document.get("roles") or [])


def scenario(root: Path, work: Path) -> dict:
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(root / "tests"))
    from scripts.ticket_board import project_provision as provision
    from scripts.ticket_board.provision_operator_packet import render_operator_commands
    from temporary_cluster import temporary_cluster

    stubs = work / "stubs"
    stubs.mkdir()
    (stubs / "sudo").write_text(SUDO, encoding="utf-8")
    (stubs / "sudo").chmod(0o755)
    def harness(cluster):
        def admin(database: str) -> str:
            return f"postgresql:///{database}?host={cluster.socket_dir}&port={cluster.port}&user=postgres"

        def sql(database: str, statement: str) -> str:
            done = subprocess.run(["psql", "-X", "-qAt", "-v", "ON_ERROR_STOP=1", admin(database), "-c", statement],
                                  capture_output=True, text=True)
            assert done.returncode == 0, done.stderr
            return done.stdout.strip()

        def packet(project: str, *, declared: bool) -> tuple[object, list[str], Path]:
            base = Path(tempfile.mkdtemp(dir=work, prefix=f"{project}."))
            board_root = base / "board"
            board_root.mkdir()
            (board_root / "current").symlink_to(root)
            plan = provision.build_plan(project=project, owner_user=f"{project}-agent", board_root=board_root,
                                        workflow=declared_document(root, project) if declared else None)
            plan = replace(plan, admin_database_url=admin(plan.database))
            provision_dir = base / "provision"
            provision.write_artifacts(plan, provision_dir)
            lines = render_operator_commands(plan).splitlines()
            first = next(i for i, line in enumerate(lines) if line.startswith("sudo systemd-tmpfiles --create"))
            last = next(i for i, line in enumerate(lines) if line.startswith("sudo systemctl daemon-reload"))
            return plan, lines[first + 1:last], provision_dir

        def kind(line: str) -> str:
            for name in ("-database.sql", "/schema.sql", "ticket-board-migrate", "-workflow.sql", "/rbac.sql"):
                if name in line:
                    return name.strip("/-").removesuffix(".sql")
            return "other: " + line[:80]

        def run_lines(lines: list[str], provision_dir: Path) -> list[dict]:
            env = {"PATH": f"{stubs}:/usr/bin:/bin", "HOME": str(work), "LANG": "C.UTF-8",
                   "PGHOST": str(cluster.socket_dir), "PGPORT": str(cluster.port), "PGUSER": "postgres"}
            steps = []
            for line in lines:
                done = subprocess.run(["bash", "-c", f"set -euo pipefail\nprovision_dir={provision_dir}\n{line}"],
                                      capture_output=True, text=True, env=env, timeout=600)
                steps.append({"kind": kind(line), "code": done.returncode,
                              "error": "\n".join(l for l in done.stderr.splitlines() if "ERROR" in l)[:400]})
                if done.returncode != 0:
                    break
            return steps

        def state(plan) -> dict:
            db = plan.database
            exists = sql("postgres", f"SELECT count(*) FROM pg_database WHERE datname = '{db}'") == "1"
            if not exists:
                return {"database": False}
            configured = sql(db, "SELECT to_regclass('ticket_board.workflow_configuration') IS NOT NULL")
            document = sql(db, "SELECT coalesce((SELECT document::text FROM ticket_board.workflow_configuration "
                               "ORDER BY revision DESC LIMIT 1), '')") if configured == "t" else ""
            return {"database": True, "acl": sql(db, ACL_SNAPSHOT),
                    "service_usage": sql(db, "SELECT has_schema_privilege('ticket_board_service', 'ticket_board', 'USAGE')"),
                    "revisions": int(sql(db, "SELECT count(*) FROM ticket_board.workflow_revisions")) if configured == "t" else 0,
                    "roles": role_names(json.loads(document)) if document else [],
                    "stages": sql(db, "SELECT string_agg(name, ',' ORDER BY rank) FROM ticket_board.workflow_stages")}

        return packet, kind, run_lines, state

    out: dict = {}
    # Each case on its own empty cluster, so each database is the host's first, as a fresh tenant's is.
    with temporary_cluster(prefix="s654.", shutdown="immediate") as cluster:
        packet, kind, run_lines, state = harness(cluster)
        # 1. A declared workflow on an empty cluster, the whole phase in its rendered order.
        plan, lines, provision_dir = packet("s654", declared=True)
        out["order"] = [kind(line) for line in lines]
        out["fresh"] = {"steps": run_lines(lines, provision_dir), **state(plan)}
        out["declared_roles"] = role_names(declared_document(root, "s654"))
        # 2. The same packet replayed whole, as an operator replays it after any interruption.
        out["replayed"] = {"steps": run_lines(lines, provision_dir), **state(plan)}
    with temporary_cluster(prefix="s654.", shutdown="immediate") as cluster:
        packet, kind, run_lines, state = harness(cluster)
        # 3. Interrupted before the seed (everything up to it ran), then replayed whole.
        plan2, lines2, provision_dir2 = packet("s654", declared=True)
        kinds = [kind(line) for line in lines2]
        seed = kinds.index("workflow") if "workflow" in kinds else len(kinds)  # a packet with no seed is a check's to fail
        out["interrupted"] = {"steps": run_lines(lines2[:seed], provision_dir2), **state(plan2)}
        out["resumed"] = {"steps": run_lines(lines2, provision_dir2), **state(plan2)}
    with temporary_cluster(prefix="s654.", shutdown="immediate") as cluster:
        packet, kind, run_lines, state = harness(cluster)
        # 4. The default workflow, which takes the same phase without a declared document.
        plan3, lines3, provision_dir3 = packet("s654", declared=False)
        out["default"] = {"order": [kind(line) for line in lines3], "steps": run_lines(lines3, provision_dir3),
                          **state(plan3)}
    with temporary_cluster(prefix="s654.", shutdown="immediate") as cluster:
        packet, kind, run_lines, state = harness(cluster)
        # 5. The reference for least privilege: the same phase with no seed at all, rbac.sql's grants alone.
        plan4, lines4, provision_dir4 = packet("s654", declared=True)
        out["no_seed"] = {"steps": run_lines([l for l in lines4 if kind(l) != "workflow"], provision_dir4),
                          **state(plan4)}
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests"],
                             check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(root: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd654.") as tmp:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX", "PG"))}
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(root), tmp],
                               capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=1800)
        assert child.returncode == 0, child.stderr[-3000:]
        return json.loads(child.stdout.strip().splitlines()[-1])


def ok(steps: list[dict]) -> bool:
    return bool(steps) and all(step["code"] == 0 for step in steps)


def before(out: dict) -> None:
    fresh = out["fresh"]
    check(out["order"] == ["database", "schema", "ticket-board-migrate", "workflow", "rbac"],
          f"before: the rendered phase seeds the workflow before rbac.sql: {out['order']}")
    failed = fresh["steps"][-1]
    check(failed["kind"] == "workflow" and failed["code"] != 0
          and "permission denied for schema ticket_board" in failed["error"] and fresh["roles"] == [],
          f"before: on an empty cluster the declared seed fails as on PIE -- permission denied for schema "
          f"ticket_board -- and nothing is seeded: {fresh}")


def candidate(out: dict) -> None:
    check(out["order"] == ["database", "schema", "ticket-board-migrate", "rbac", "workflow"],
          f"the rendered phase grants first and seeds last: {out['order']}")
    fresh = out["fresh"]
    check(ok(fresh["steps"]) and len(fresh["steps"]) == 5 and fresh["roles"] == out["declared_roles"]
          and fresh["revisions"] >= 1 and fresh["service_usage"] == "t",
          f"an empty cluster takes every step of the rendered phase, with no grant by hand, and carries the "
          f"declared workflow: {fresh}")
    check(fresh["acl"] == out["no_seed"]["acl"] and ok(out["no_seed"]["steps"]),
          f"least privilege: every object's privileges are exactly rbac.sql's, as on the same phase with no seed: "
          f"{fresh['acl'] == out['no_seed']['acl']}")
    replayed = out["replayed"]
    check(ok(replayed["steps"]) and replayed["acl"] == fresh["acl"] and replayed["roles"] == fresh["roles"]
          and replayed["stages"] == fresh["stages"],
          f"replayed whole, every step succeeds again and grants, workflow and stages are unchanged: {replayed}")
    interrupted, resumed = out["interrupted"], out["resumed"]
    check(ok(interrupted["steps"]) and interrupted["roles"] == [] and ok(resumed["steps"])
          and resumed["roles"] == out["declared_roles"] and resumed["acl"] == fresh["acl"],
          f"interrupted before the seed and replayed whole, it ends as an uninterrupted run does: {interrupted} {resumed}")
    default = out["default"]
    check(default["order"] == out["order"] and ok(default["steps"]) and default["stages"]
          and default["acl"] == fresh["acl"],
          f"the default workflow takes the same order and seeds its stages, with the same privileges: {default}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), Path(sys.argv[3])), default=str))
        return 0
    if len(sys.argv) == 2 and sys.argv[1] == "--dump":
        print(json.dumps(run(ROOT), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD654_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd654-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"packet_workflow_seed_order_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
