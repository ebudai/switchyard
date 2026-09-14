#!/usr/bin/env python3
"""SYRD-141: the role field that says what a pane is called.

The label a person reads on a presentation pane is a tenant's to choose, so it
is a field of the workflow document -- and like every other string this schema
hands to a terminal, it is checked before it can get there. Both validators
check it, because they are independent implementations of the same document and
a rule only one of them knows is a rule a tenant can get past.

What is NOT here is the default. A role with no label of its own reads
`<Role> Developer` when the document declares it an implementer and its own
name otherwise; that is a presentation decision made in the projection, not a
rule about what a valid document is, and it is proved where it is made.
"""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from tmux_bus_isolation import isolate_tmux_bus  # noqa: E402

isolate_tmux_bus()

import ticket_board_write_api_test as t  # noqa: E402
from temporary_cluster import temporary_cluster  # noqa: E402

from scripts.ticket_board.workflow_config import validate  # noqa: E402

MIGRATION = "pgu941_syrd141_presentation_label.sql"
CANONICAL = json.loads((ROOT / "examples/workflows/inspection.json").read_text(encoding="utf-8"))


def document(**labels: Any) -> dict[str, Any]:
    cfg = copy.deepcopy(CANONICAL)
    cfg["project"] = "pgu"
    for role in cfg["roles"]:
        if role.get("target"):
            role["target"] = f"pgu-{role['name']}:0.0"
        role.pop("presentation_label", None)
        if role["name"] in labels:
            role["presentation_label"] = labels[role["name"]]
    return cfg


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, text=True, capture_output=True, check=True
    ).stdout


def schema_before_this_change() -> str:
    """schema.sql as it stood before this migration joined the tree."""
    adding = git(
        "log", "--format=%H", "--diff-filter=A", "--",
        f"scripts/ticket_board/migrations/{MIGRATION}",
    ).strip().splitlines()
    ref = f"{adding[-1]}^" if adding else "HEAD"
    return git("show", f"{ref}:scripts/ticket_board/schema.sql")


def refused(call: Any, expected: str) -> None:
    try:
        call()
    except Exception as exc:
        assert expected in str(exc), (expected, str(exc))
        return
    raise AssertionError(f"expected a refusal containing {expected!r}")


def sql_validate(admin: str, cfg: dict[str, Any]) -> None:
    t.psql(
        admin,
        "SELECT ticket_board.validate_declared_workflow('"
        + json.dumps(cfg).replace("'", "''")
        + "'::jsonb);",
    )


def run_checks(cluster: Any) -> int:
    checks = 0
    dbname = "syrd141_labels"
    admin = t.conninfo(cluster.socket_dir, cluster.port, dbname)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    t.psql(admin, t.SCHEMA_PATH.read_text(encoding="utf-8"))

    # Accepted by both.
    for accepted in (document(), document(main="Main Developer", ops="Ops")):
        validate(copy.deepcopy(accepted), project="pgu")
        sql_validate(admin, accepted)
    # Including the shipped example exactly as it stands, which carries one.
    validate(copy.deepcopy(CANONICAL), project=CANONICAL["project"])
    sql_validate(admin, CANONICAL)
    assert any(
        role.get("presentation_label") for role in CANONICAL["roles"]
    ), "the example should show the field it documents"
    checks += 1

    # And refused by both, naming the role, for every way of not being a label.
    for bad in ("", "   ", "two\nlines", 7, True, None, ["Main"]):
        broken = document()
        for role in broken["roles"]:
            if role["name"] == "main":
                role["presentation_label"] = bad
        refused(
            lambda broken=broken: validate(copy.deepcopy(broken), project="pgu"),
            "presentation_label must be non-empty single-line text: main",
        )
        refused(
            lambda broken=broken: sql_validate(admin, broken),
            "presentation_label must be non-empty single-line text: main",
        )
    checks += 1
    return checks


def run_upgrade_checks(cluster: Any) -> int:
    """A board that predates the field, brought up by the migration alone."""
    dbname = "syrd141_upgrade"
    admin = t.conninfo(cluster.socket_dir, cluster.port, dbname)
    t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", dbname])
    t.psql(admin, schema_before_this_change())

    labelled = document(main="Main Developer")
    # Before: the old validator knows the field but not the rule, or does not
    # know the field at all. Either way it is the migration that makes the two
    # validators agree, so what is asserted is the state after it.
    migration = (ROOT / "scripts/ticket_board/migrations" / MIGRATION).read_text(encoding="utf-8")
    t.psql(admin, migration)
    t.psql(admin, migration)  # idempotent: a runner may apply it twice
    sql_validate(admin, labelled)

    broken = document(main="")
    refused(
        lambda: sql_validate(admin, broken),
        "presentation_label must be non-empty single-line text: main",
    )
    return 1


def main() -> int:
    if not (shutil.which("initdb") and shutil.which("psql")):
        print("presentation_label_schema_test: no PostgreSQL binaries; skipped")
        return 0
    checks = 0
    with temporary_cluster(prefix="syrd141-labels-", shutdown="immediate") as cluster:
        checks += run_checks(cluster)
        checks += run_upgrade_checks(cluster)
    print(f"presentation_label_schema_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
