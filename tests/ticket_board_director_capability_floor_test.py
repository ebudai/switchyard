#!/usr/bin/env python3
"""SYRD-82: a declared workflow cannot disarm the Director, or arm it to approve.

A tenant configures its own pipeline. What it does not get to do is leave the
project without a controller, or make the controller a reviewer of its own work.
Both halves are refused by the API validator and by PostgreSQL independently,
because a caller that reaches the database directly is exactly the caller the
Python layer cannot speak for.

The floor has one definition. This suite holds the four things that must agree
to it -- the Python validator, the PL/pgSQL validator, the role discriminator
`onboarding_readiness` finds the director with, and the Director skill -- so a
capability added to the floor cannot reach only some of them.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from scripts import onboarding_readiness  # noqa: E402
from scripts.ticket_board.workflow_config import (  # noqa: E402
    DIRECTOR_CONTROL_CAPABILITIES,
    DIRECTOR_IDENTIFYING_CAPABILITIES,
    DIRECTOR_ROLE,
    validate,
)

SCHEMA_PATH = ROOT / "scripts" / "ticket_board" / "schema.sql"
SKILL_PATH = ROOT / "skills" / "switchyard-director" / "SKILL.md"
EXAMPLE = ROOT / "examples" / "workflows" / "inspection.json"
FLOOR_START = "<!-- switchyard-director-floor:start -->"
FLOOR_END = "<!-- switchyard-director-floor:end -->"


def base_document() -> dict:
    """A document that is accepted today, so a refusal below is about one edit."""
    return json.loads(EXAMPLE.read_text(encoding="utf-8"))


def director_of(document: dict) -> dict:
    return next(r for r in document["roles"] if r["name"] == DIRECTOR_ROLE)


def refused(document: dict, reason: str) -> str:
    try:
        validate(document)
    except Exception as exc:  # the validator raises ValueError with the reason
        assert reason in str(exc), (reason, str(exc))
        return str(exc)
    raise AssertionError(f"expected a refusal mentioning {reason!r}")


def test_the_example_document_is_accepted_unchanged() -> None:
    """Every refusal below is one edit away from something that works."""
    validate(base_document())


def test_stripping_any_control_capability_is_refused() -> None:
    """One at a time, so each is shown to be load-bearing on its own."""
    for capability in sorted(DIRECTOR_CONTROL_CAPABILITIES):
        document = base_document()
        director = director_of(document)
        assert capability in director["capabilities"], (capability, director)
        director["capabilities"] = [c for c in director["capabilities"] if c != capability]
        message = refused(document, "director must keep its control capabilities")
        assert capability in message, (capability, message)


def test_granting_the_director_sign_off_authority_is_refused() -> None:
    """An `approve` transition is what writes its stage's sign-off flag."""
    document = base_document()
    approvals = [t for t in document["transitions"] if t["primitive"] == "approve"]
    assert approvals, "the fixture has no approval to hand over"
    approvals[0]["actors"] = sorted(set(approvals[0]["actors"]) | {DIRECTOR_ROLE})
    message = refused(document, "director must not be granted sign-off authority")
    assert approvals[0]["action"] in message, message


def test_a_stage_the_director_cannot_leave_is_refused() -> None:
    """Queue, defer and cancel are transitions, so the floor is the shape."""
    document = base_document()
    stages = {s["name"]: s for s in document["stages"]}
    victim = next(
        s["from"]
        for s in document["transitions"]
        if DIRECTOR_ROLE in s["actors"] and not stages[s["from"]]["terminal"]
    )
    for transition in document["transitions"]:
        if transition["from"] == victim and DIRECTOR_ROLE in transition["actors"]:
            others = [a for a in transition["actors"] if a != DIRECTOR_ROLE]
            # A transition needs at least one actor, so hand it to somebody else
            # rather than emptying it: this must fail on the floor, not on the
            # unrelated "transition needs actors" rule.
            transition["actors"] = others or ["audit"]
    message = refused(document, "director must be able to move work out of every stage")
    assert victim in message, message


def test_a_terminal_stage_the_director_cannot_reopen_is_refused() -> None:
    document = base_document()
    stages = {s["name"]: s for s in document["stages"]}
    victim = next(
        t["from"]
        for t in document["transitions"]
        if t["primitive"] == "reopen" and DIRECTOR_ROLE in t["actors"] and stages[t["from"]]["terminal"]
    )
    document["transitions"] = [
        t
        for t in document["transitions"]
        if not (t["from"] == victim and t["primitive"] == "reopen")
    ]
    message = refused(document, "director must be able to reopen every terminal stage")
    assert victim in message, message


def test_a_tenant_that_names_its_moves_differently_still_satisfies_the_floor() -> None:
    """The floor may not depend on what this project happens to call things.

    Every action is renamed. Nothing about the shape changes: the director can
    still leave every stage and reopen every terminal one, so both validators
    must still accept it. A rule that looked for an action called `reopen`, or
    `cancel`, would reject this document while the tenant's director is in fact
    in full control -- and would accept the reverse.
    """
    document = base_document()
    for transition in document["transitions"]:
        transition["action"] = f"tenant_{transition['action']}"
    validate(document)

    if not shutil.which("initdb") or not shutil.which("psql"):
        return
    import ticket_board_write_api_test as api
    from temporary_cluster import temporary_cluster

    payload = json.dumps(document)
    assert "$doc$" not in payload
    with temporary_cluster(prefix="floor-renamed-", shutdown="immediate") as cluster:
        conn = api.conninfo(cluster.socket_dir, cluster.port, "renamed")
        api.run(
            ["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "renamed"]
        )
        api.psql(conn, SCHEMA_PATH.read_text(encoding="utf-8"))
        proc = subprocess.run(
            ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conn],
            input=f"SELECT ticket_board.validate_declared_workflow($doc${payload}$doc$::jsonb);",
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"


def test_the_floor_is_expressed_without_this_project_s_action_names() -> None:
    """A tenant names its own moves; the floor may not depend on ours."""
    source = (ROOT / "scripts" / "ticket_board" / "workflow_config.py").read_text(encoding="utf-8")
    floor_region = source[source.index("director_exits: set[str] = set()") :]
    floor_region = floor_region[: floor_region.index("queue = cfg.setdefault")]
    for action in ("route", "cancel", "defer", "mark_done", "release_draft"):
        assert f'"{action}"' not in floor_region, (action, floor_region)
    assert "reopen" in floor_region, "the reopen rule must key on the primitive"


def test_the_discriminator_is_contained_in_the_floor() -> None:
    """Finding the director by capability must not outlive the guarantee."""
    assert DIRECTOR_IDENTIFYING_CAPABILITIES <= DIRECTOR_CONTROL_CAPABILITIES
    assert onboarding_readiness.DIRECTOR_CAPABILITIES == DIRECTOR_IDENTIFYING_CAPABILITIES
    # And a document that satisfies the floor is always one the discriminator
    # can resolve, which is the property that makes the containment matter.
    role, error = onboarding_readiness.find_director_role(base_document())
    assert (role, error) == (DIRECTOR_ROLE, ""), (role, error)


def test_the_database_names_the_same_floor() -> None:
    """The two validators are independent; drifting apart is the failure mode."""
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    body = schema.split("FUNCTION ticket_board.director_control_capabilities()", 1)[1]
    body = body.split("$$;", 1)[0]
    declared = set(re.findall(r"'([a-z_]+)'", body))
    assert declared == set(DIRECTOR_CONTROL_CAPABILITIES), (
        sorted(declared),
        sorted(DIRECTOR_CONTROL_CAPABILITIES),
    )
    # SYRD-78's control-role discriminator lives in the same file and must stay
    # inside the floor there too, not only in Python.
    control = schema.split("FUNCTION ticket_board.control_capabilities()", 1)[1].split("$$;", 1)[0]
    assert set(re.findall(r"'([a-z_]+)'", control)) <= set(DIRECTOR_CONTROL_CAPABILITIES)


def test_the_director_skill_lists_the_floor_and_cannot_drift() -> None:
    """The skill is what a Director reads; a stale list there is a lie."""
    skill = SKILL_PATH.read_text(encoding="utf-8")
    assert FLOOR_START in skill and FLOOR_END in skill, "the skill has no generated floor block"
    block = skill.split(FLOOR_START, 1)[1].split(FLOOR_END, 1)[0]
    listed = set(re.findall(r"^- `([a-z_]+)`$", block.strip(), flags=re.M))
    assert listed == set(DIRECTOR_CONTROL_CAPABILITIES), (
        sorted(listed),
        sorted(DIRECTOR_CONTROL_CAPABILITIES),
    )
    assert "never approve" in skill, "the skill must state the sign-off prohibition"


def test_postgresql_refuses_the_same_documents_on_its_own() -> None:
    """The Python validator cannot speak for a caller that never runs it.

    Each document below is put straight to `validate_declared_workflow` in a
    real cluster, so what is proved is the database's own refusal rather than
    the API's.
    """
    if not shutil.which("initdb") or not shutil.which("psql"):
        return
    import ticket_board_write_api_test as api
    from temporary_cluster import temporary_cluster

    stripped = base_document()
    director_of(stripped)["capabilities"] = [
        c for c in director_of(stripped)["capabilities"] if c != "merge"
    ]
    approving = base_document()
    approval = next(t for t in approving["transitions"] if t["primitive"] == "approve")
    approval["actors"] = sorted(set(approval["actors"]) | {DIRECTOR_ROLE})
    stages = {s["name"]: s for s in base_document()["stages"]}
    stuck = base_document()
    victim = next(
        t["from"]
        for t in stuck["transitions"]
        if DIRECTOR_ROLE in t["actors"] and not stages[t["from"]]["terminal"]
    )
    for transition in stuck["transitions"]:
        if transition["from"] == victim and DIRECTOR_ROLE in transition["actors"]:
            others = [a for a in transition["actors"] if a != DIRECTOR_ROLE]
            transition["actors"] = others or ["audit"]
    sealed = base_document()
    terminal = next(
        t["from"]
        for t in sealed["transitions"]
        if t["primitive"] == "reopen" and DIRECTOR_ROLE in t["actors"] and stages[t["from"]]["terminal"]
    )
    sealed["transitions"] = [
        t for t in sealed["transitions"] if not (t["from"] == terminal and t["primitive"] == "reopen")
    ]

    cases = [
        (stripped, "director must keep its control capabilities"),
        (approving, "director must not be granted sign-off authority"),
        (stuck, "director must be able to move work out of every stage"),
        (sealed, "director must be able to reopen every terminal stage"),
    ]

    with temporary_cluster(prefix="director-floor-", shutdown="immediate") as cluster:
        conn = api.conninfo(cluster.socket_dir, cluster.port, "floor_test")
        api.run(
            ["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "floor_test"]
        )
        api.psql(conn, SCHEMA_PATH.read_text(encoding="utf-8"))

        def check(document: dict) -> str:
            """The database's own verdict: "" when it accepts, its error when not."""
            payload = json.dumps(document)
            assert "$doc$" not in payload
            proc = subprocess.run(
                ["psql", "-X", "-v", "ON_ERROR_STOP=1", "-tA", conn],
                input=f"SELECT ticket_board.validate_declared_workflow($doc${payload}$doc$::jsonb);",
                capture_output=True,
                text=True,
            )
            return f"{proc.stdout}\n{proc.stderr}".strip() if proc.returncode else ""

        # The unmodified document is accepted, so each refusal below is about
        # the one edit and not about the fixture.
        assert check(base_document()) == "", check(base_document())
        for document, reason in cases:
            message = check(document)
            assert message, f"postgresql accepted a document the floor forbids: {reason}"
            assert reason in message, (reason, message)


def test_the_upgrade_brings_an_existing_tenant_up_to_the_floor() -> None:
    """A tenant configured before this rule must not be locked out by it.

    Its stored document may leave the director short of a capability. Refusing
    its next workflow change for a state its director never chose would be the
    rule punishing the wrong party, so the migration grants what is missing,
    once, and records that it did.

    The floor grows, so the upgrade is a sequence rather than one file: each
    migration knows only the floor of its own release, and a tenant is level
    with today's floor only once it has run all of them, in order. That is what
    is driven here (SYRD-82, SYRD-83).
    """
    if not shutil.which("initdb") or not shutil.which("psql"):
        return
    import ticket_board_write_api_test as api
    from temporary_cluster import temporary_cluster

    # A tenant from before either release: it has neither the capability the
    # floor gained in SYRD-82 nor the one it gained in SYRD-83.
    stale = base_document()
    later_capabilities = ("reassign", "director_edit", "resolve_publication")
    director_of(stale)["capabilities"] = [
        c for c in director_of(stale)["capabilities"] if c not in later_capabilities
    ]
    for role in stale["roles"]:
        role["capabilities"] = [c for c in role["capabilities"] if c != "request_publication"]
    payload = json.dumps(stale)
    assert "$d$" not in payload
    migrations = [
        (ROOT / "scripts/ticket_board/migrations" / name).read_text(encoding="utf-8")
        for name in (
            "pgu927_syrd82_director_capability_floor.sql",
            "pgu928_syrd83_director_edit.sql",
            "pgu929_syrd92_director_defer_backlog.sql",
            "pgu930_syrd93_publication_requests.sql",
        )
    ]

    with temporary_cluster(prefix="floor-upgrade-", shutdown="immediate") as cluster:
        conn = api.conninfo(cluster.socket_dir, cluster.port, "floor_upgrade")
        api.run(
            ["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "floor_upgrade"]
        )
        api.psql(conn, SCHEMA_PATH.read_text(encoding="utf-8"))
        # Seeded the way a real pre-floor tenant's row already is: written when
        # no validator forbade it, so it is in the table without ever having
        # passed today's rules.
        api.psql(
            conn,
            f"""
            INSERT INTO ticket_board.workflow_revisions(actor, document) VALUES ('seed', $d${payload}$d$::jsonb);
            INSERT INTO ticket_board.workflow_configuration(singleton, revision, document)
                 VALUES (true, (SELECT max(revision) FROM ticket_board.workflow_revisions), $d${payload}$d$::jsonb)
            ON CONFLICT (singleton) DO UPDATE SET revision=EXCLUDED.revision, document=EXCLUDED.document;
            INSERT INTO ticket_board.workflow_roles(name, definition)
            SELECT x->>'name', x FROM jsonb_array_elements($d${payload}$d$::jsonb->'roles') x
            ON CONFLICT (name) DO UPDATE SET definition=EXCLUDED.definition;
            """,
        )
        assert "reassign" not in api.psql(
            conn, "SELECT definition->'capabilities' FROM ticket_board.workflow_roles WHERE name='director';"
        )

        for index, migration in enumerate(migrations):
            api.psql(conn, "BEGIN;\n" + migration + "\nCOMMIT;")
            # Each one is idempotent on its own, and running it again must not
            # write a revision nobody asked for.
            reached = api.psql(conn, "SELECT revision FROM ticket_board.workflow_configuration;")
            api.psql(conn, "BEGIN;\n" + migration + "\nCOMMIT;")
            assert api.psql(conn, "SELECT revision FROM ticket_board.workflow_configuration;") == reached
            if index == 0:
                # The release that granted it knew nothing of the later ones.
                assert "director_edit" not in api.psql(
                    conn,
                    "SELECT definition->'capabilities' FROM ticket_board.workflow_roles "
                    "WHERE name='director';",
                )

        granted = api.psql(
            conn, "SELECT definition->'capabilities' FROM ticket_board.workflow_roles WHERE name='director';"
        )
        # The projected role rows are what the database checks per call, so the
        # repair has to reach them and not only the document.
        for capability in sorted(DIRECTOR_CONTROL_CAPABILITIES):
            assert capability in granted, (capability, granted)


def main() -> int:
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
    print(f"ticket_board_director_capability_floor_test: {len(tests)} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
