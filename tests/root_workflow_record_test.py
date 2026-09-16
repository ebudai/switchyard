#!/usr/bin/env python3
"""SYRD-165: root's own copy of a project's declared workflow.

SYRD-164 established that the packet root regenerates for syrd takes the
default-project branch even though syrd runs a declared workflow. Root's plan
record carries no document; the tenant's configuration does. A rebuilt baseline
simply dropped the field, so a project that declares its own roles, stages,
transitions and labels was regenerated as though it declared none.

On an established board that is now harmless, because the seed leaves a running
board alone. On a fresh or interrupted one -- no tickets, no workflow document
in the database yet -- the packet would have installed the DEFAULT workflow in
place of the declared one: different roles, different capabilities, different
transitions, and no error anywhere.

The document decides authority, so the copy root acts on has to be root's. It
is written beside the plan when root first generates a project's artifacts,
carries a digest of what it holds, and is read the way root reads its other
authorities -- by fd, refusing a symlink at every component, required to belong
to root. The tenant's copy is never consulted. A record that exists and cannot
be used is a refusal, not a reason to fall back, and a project that declares a
workflow root has no record of seeds nothing at all rather than being given
somebody else's.

The privileged half runs inside a user namespace, where this process is uid 0
and can own the files root would own.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board.project_provision import (  # noqa: E402
    build_plan,
    render_workflow_sql,
)


def record_name() -> str:
    """Imported where it is used, so the cases that need no new API can run
    against a tree that has none of it -- which is how the first one below
    reproduces what it was written against."""
    from scripts.ticket_board import project_provision

    return project_provision.WORKFLOW_RECORD_NAME


def unverified_marker() -> str:
    from scripts.ticket_board import project_provision

    return project_provision.UNVERIFIED_DECLARED_WORKFLOW


def document_digest(document: dict) -> str:
    from scripts.ticket_board import project_provision

    return project_provision.workflow_document_digest(document)


def record_document(raw, *, project: str):
    from scripts.ticket_board import project_provision

    return project_provision.workflow_record_document(raw, project=project)
from resume_provision_test import SEAM, namespaces_available  # noqa: E402

PROJECT = "demo"
TENANT = "demo-agent"

#: A real declared workflow, taken from the example this repository ships and
#: re-projected onto this project, so what is recorded and seeded here is a
#: document the product's own validator accepts rather than one shaped to pass.
#: It declares an `inspector` role and an inspection stage, neither of which the
#: default seed has -- so "the declared one" and "the default one" can never be
#: mistaken for each other.
def _declared_document(project: str = PROJECT) -> dict:
    raw = json.loads((ROOT / "examples" / "workflows" / "inspection.json").read_text())
    source = str(raw.get("project") or "")
    text = json.dumps(raw).replace(f'"{source}-', f'"{project}-')
    document = json.loads(text)
    document["project"] = project
    return document


DECLARED = _declared_document()


#: What the default seed writes and the declared workflow does not: the default
#: seed builds rows itself, the declared one hands the document to the board.
DEFAULT_SEED_MARKER = "INSERT INTO ticket_board.workflow_stages"
DECLARED_SEED_MARKER = "apply_declared_workflow"


def declared_plan(home: Path, **overrides):
    return build_plan(
        project=PROJECT, owner_user=TENANT, owner_home=home, workflow=DECLARED, **overrides
    )


def default_plan(home: Path):
    return build_plan(project=PROJECT, owner_user=TENANT, owner_home=home)


# --------------------------------------------------------------------------
# what can be asked without owning anything
# --------------------------------------------------------------------------


def test_a_fresh_declared_tenant_records_and_seeds_its_own_workflow() -> None:
    """Acceptance: the declared configuration, recorded and seeded."""
    with tempfile.TemporaryDirectory(prefix="syrd165-fresh.") as tmp:
        plan = declared_plan(Path(tmp) / "home")
        rendered = launcher.render_privileged_artifacts(plan)

        assert record_name() in rendered, sorted(rendered)
        record = json.loads(rendered[record_name()].decode("utf-8"))
        assert record["project"] == PROJECT
        assert record["document"] == plan.workflow
        assert record["digest"] == document_digest(plan.workflow)

        seed = rendered[f"{PROJECT}-workflow.sql"].decode("utf-8")
        assert DECLARED_SEED_MARKER in seed, seed[:400]
        assert DEFAULT_SEED_MARKER not in seed, seed[:400]
        # The document the board is handed is this project's, not a summary.
        assert '"name": "audit"' in seed or '"name":"audit"' in seed


def test_a_default_workflow_tenant_is_exactly_what_it_was() -> None:
    """Acceptance: ordinary tenants are untouched by any of this."""
    with tempfile.TemporaryDirectory(prefix="syrd165-default.") as tmp:
        plan = default_plan(Path(tmp) / "home")
        rendered = launcher.render_privileged_artifacts(plan)

        assert record_name() not in rendered, sorted(rendered)
        seed = rendered[f"{PROJECT}-workflow.sql"].decode("utf-8")
        assert DEFAULT_SEED_MARKER in seed
        assert DECLARED_SEED_MARKER not in seed


def test_a_record_is_refused_unless_it_matches_its_own_digest() -> None:
    """Acceptance: tampering fails closed, and says what it found."""
    with tempfile.TemporaryDirectory(prefix="syrd165-digest.") as tmp:
        plan = declared_plan(Path(tmp) / "home")
        raw = json.loads(launcher.render_privileged_artifacts(plan)[record_name()])

        document, problem = record_document(raw, project=PROJECT)
        assert document is not None and not problem, problem

        edited = json.loads(json.dumps(raw))
        edited["document"]["roles"][1]["capabilities"].append("reassign")
        assert record_document(edited, project=PROJECT)[0] is None
        assert "does not match its own digest" in record_document(edited, project=PROJECT)[1]

        # A digest recomputed over the edit is still refused where it matters:
        # the document has to pass the same validation provisioning applies.
        broken = json.loads(json.dumps(raw))
        broken["document"]["roles"] = []
        broken["digest"] = document_digest(broken["document"])
        assert record_document(broken, project=PROJECT)[0] is None
        assert "not usable" in record_document(broken, project=PROJECT)[1]

        # Another project's record is not this project's.
        foreign = json.loads(json.dumps(raw))
        foreign["project"] = "somebody-else"
        assert "names project" in record_document(foreign, project=PROJECT)[1]

        assert "holds no recorded workflow" in record_document(None, project=PROJECT)[1]


def test_a_plan_root_cannot_vouch_for_seeds_nothing() -> None:
    """The refusal in the packet itself: no declared seed, and no default one."""
    with tempfile.TemporaryDirectory(prefix="syrd165-unverified.") as tmp:
        from dataclasses import replace

        plan = replace(
            default_plan(Path(tmp) / "home"), workflow_seed=unverified_marker()
        )
        seed = render_workflow_sql(plan)

        assert DEFAULT_SEED_MARKER not in seed, seed[:400]
        assert DECLARED_SEED_MARKER not in seed, seed[:400]
        assert "root holds no verified copy" in seed, seed[:400]
        # And it names the supported way to give root a copy (SYRD-166).
        assert "adopt-workflow" in seed, seed[:400]


# --------------------------------------------------------------------------
# the privileged half, inside a user namespace
# --------------------------------------------------------------------------


def tenant_identity(home: Path, *, owner: str = TENANT):
    """Inside the namespace every file is root's, so who owns what is answered.

    The product derives the tenant from the provision directory's owner and its
    home from passwd. Neither can be true of a tree this process created as the
    only uid there is, so both are supplied -- and nothing else about the
    rebuild is.
    """
    from contextlib import ExitStack
    from types import SimpleNamespace
    from unittest.mock import patch

    stack = ExitStack()
    stack.enter_context(patch.object(launcher, "_provision_owner", lambda _dir: (owner, "")))
    real = launcher.pwd.getpwnam
    stack.enter_context(
        patch.object(
            launcher.pwd,
            "getpwnam",
            side_effect=lambda name: (
                SimpleNamespace(pw_name=name, pw_uid=0, pw_gid=0, pw_dir=str(home))
                if name == owner
                else real(name)
            ),
        )
    )
    return stack


def privileged_cases() -> int:
    checks = 0
    with tempfile.TemporaryDirectory(prefix="syrd165-privileged.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        os.environ[SEAM] = str(root)
        home = root / "home" / TENANT
        (home / "Projects" / PROJECT / ".switchyard" / "provision").mkdir(parents=True)
        provision_dir = home / "Projects" / PROJECT / ".switchyard" / "provision"
        tenant_declares = {"workflow": DECLARED, "port": 24680}
        tenant_default = {"port": 24680}

        # 0. The divergence, asked of the tree that has it. Nothing here is new
        #    API: a baseline root rebuilds for a project whose tenant document
        #    declares a workflow must not produce a packet that seeds the
        #    DEFAULT one. On the tree this ticket fixes, it does.
        with tenant_identity(home):
            rebuilt, problem = launcher.reconstruct_privileged_baseline(
                PROJECT, provision_dir, tenant_declares, source_repo=ROOT
            )
        assert rebuilt is not None, problem
        seeded = render_workflow_sql(rebuilt)
        assert DEFAULT_SEED_MARKER not in seeded, (
            "a rebuilt baseline for a declared-workflow tenant carried the default seed: "
            + seeded[:300]
        )
        checks += 1

        # 1. And what it does instead: nothing it cannot vouch for.
        assert rebuilt.workflow is None
        assert rebuilt.workflow_seed == unverified_marker()
        assert "root holds no verified copy" in seeded
        checks += 1

        # 2. Root's own record is what it consults. Install one the way root
        #    installs it, and the same rebuild now carries the declared
        #    workflow -- from root's copy, with the tenant document unchanged.
        plan = declared_plan(home)
        installed = launcher.install_privileged_artifacts(
            plan, launcher.render_privileged_artifacts(plan)
        )
        record = installed / record_name()
        assert record.is_file() and os.stat(record).st_uid == 0, record
        recorded, problem = launcher.recorded_declared_workflow(PROJECT)
        assert recorded is not None, problem
        assert recorded == plan.workflow
        checks += 1

        with tenant_identity(home):
            rebuilt, problem = launcher.reconstruct_privileged_baseline(
                PROJECT, provision_dir, tenant_declares, source_repo=ROOT
            )
        assert rebuilt is not None, problem
        assert rebuilt.workflow == plan.workflow, "root's recorded workflow is what it rebuilds with"
        assert any(role["name"] == "inspector" for role in rebuilt.workflow["roles"])
        assert DECLARED_SEED_MARKER in render_workflow_sql(rebuilt)
        checks += 1

        # 3. Byte for byte: regenerating from the record produces the same
        #    workflow SQL as the generation that wrote it.
        assert render_workflow_sql(rebuilt).encode("utf-8") == (
            launcher.render_privileged_artifacts(plan)[f"{PROJECT}-workflow.sql"]
        )
        checks += 1

        # 4. The tenant's copy is not consulted. Say something else in it, and
        #    root still rebuilds from its own.
        smuggled = json.loads(json.dumps(DECLARED))
        smuggled["roles"].append(
            {"name": "intruder", "kind": "implementer", "label": "Intruder", "active": True,
             "capabilities": ["create_ticket", "add_comment", "edit_fields"],
             "runtime": "claude", "slot": None, "target": None, "onboarding": None}
        )
        with tenant_identity(home):
            rebuilt, problem = launcher.reconstruct_privileged_baseline(
                PROJECT, provision_dir, {"workflow": smuggled, "port": 24680}, source_repo=ROOT
            )
        assert rebuilt is not None, problem
        assert rebuilt.workflow == plan.workflow, "root adopted the tenant's document"
        assert not any(role["name"] == "intruder" for role in rebuilt.workflow["roles"])
        checks += 1

        # 5. Tampering with root's own record fails closed: the rebuild refuses
        #    rather than falling back to anything.
        original = record.read_bytes()
        edited = json.loads(original.decode("utf-8"))
        edited["document"]["roles"][1]["capabilities"].append("director_edit")
        record.write_text(json.dumps(edited), encoding="utf-8")
        with tenant_identity(home):
            rebuilt, problem = launcher.reconstruct_privileged_baseline(
                PROJECT, provision_dir, tenant_declares, source_repo=ROOT
            )
        assert rebuilt is None, rebuilt
        assert "does not match its own digest" in problem, problem
        record.write_bytes(original)
        checks += 1

        # 6. A record anybody else could rewrite is not root's record.
        record.chmod(0o664)
        with tenant_identity(home):
            rebuilt, problem = launcher.reconstruct_privileged_baseline(
                PROJECT, provision_dir, tenant_declares, source_repo=ROOT
            )
        assert rebuilt is None, rebuilt
        assert "group or beyond can write" in problem, problem
        record.chmod(0o644)
        checks += 1

        # 7. A default-workflow tenant is unchanged by all of it: no record is
        #    written, and its packet still carries the default seed.
        other = root / "home" / "plain-agent"
        plain = build_plan(project="plain", owner_user="plain-agent", owner_home=other)
        plain_installed = launcher.install_privileged_artifacts(
            plain, launcher.render_privileged_artifacts(plain)
        )
        assert not (plain_installed / record_name()).exists()
        with tenant_identity(other, owner="plain-agent"):
            rebuilt, problem = launcher.reconstruct_privileged_baseline(
                "plain", provision_dir, tenant_default, source_repo=ROOT
            )
        assert rebuilt is not None, problem
        assert rebuilt.workflow is None and rebuilt.workflow_seed == "default-project"
        assert DEFAULT_SEED_MARKER in render_workflow_sql(rebuilt)
        checks += 1
    return checks


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    if "--privileged-child" in sys.argv:
        checks += privileged_cases()
        print(f"root_workflow_record_test: privileged child ran {checks} checks")
        return 0
    if namespaces_available():
        done = subprocess.run(
            ["unshare", "--user", "--map-root-user", sys.executable, __file__, "--privileged-child"],
            text=True, capture_output=True,
        )
        if done.returncode != 0:
            print(done.stdout + done.stderr)
            return 1
        print(done.stdout.strip())
    else:
        print("root_workflow_record_test: user namespaces unavailable; privileged half skipped")
    print(f"root_workflow_record_test: {checks} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
