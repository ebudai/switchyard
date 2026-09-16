#!/usr/bin/env python3
"""SYRD-166: adopting an existing tenant's declared workflow, on purpose.

SYRD-165 gave root its own copy of a project's declared workflow and made
regeneration read that and nothing else. A project provisioned before root kept
one has no such copy, and its document lives in exactly the file root refuses to
trust on its own: the tenant's generated plan, writable by the account every
role runs as. Those projects are safe -- their packets seed nothing rather than
seeding the default workflow over them -- and stuck.

Adoption is how they stop being stuck, and it is deliberately not automatic.
The document decides which roles exist and what each of them may call, so what
makes this safe is not any single check:

  - the tenant's file and the workflow the running board is enforcing have to
    say the same thing, and every line where they differ is printed;
  - a human has to authorize the run through Polkit, so there is somebody to
    record it against rather than a script that inherited root;
  - nothing is written without `--apply`, and what was shown and decided goes
    into the rollout journal either way;
  - the file is read the way root reads anything it did not write: by fd, no
    symlink at any component, owned by the project owner or root, unwritable by
    anybody else.

The privileged half runs inside a user namespace, where this process is uid 0
and can own the files root would own.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board.project_provision import (  # noqa: E402
    build_plan,
    render_workflow_sql,
    write_artifacts,
)
from resume_provision_test import SEAM, SLUG, TENANT, namespaces_available  # noqa: E402
from resume_provision_continuation_test import fixture, owner_patches, quiet  # noqa: E402
from root_workflow_record_test import tenant_identity  # noqa: E402

JOURNAL_ENV = "SWITCHYARD_ROLLOUT_JOURNAL_ROOT"
OPERATOR = SimpleNamespace(name="an-operator", uid=1000, source="pkexec", known=True)
SUDO_ONLY = SimpleNamespace(name="an-operator", uid=1000, source="sudo", known=True)
NOBODY = SimpleNamespace(name="", uid=None, source="", known=False)


def declared_document(project: str = SLUG) -> dict:
    """A real declared workflow: the example this repository ships, re-projected."""
    raw = json.loads((ROOT / "examples" / "workflows" / "inspection.json").read_text())
    source = str(raw.get("project") or "")
    document = json.loads(json.dumps(raw).replace(f'"{source}-', f'"{project}-'))
    document["project"] = project
    return document


def board(document: dict | None, problem: str = ""):
    return lambda _config: (document, problem or ("" if document is not None else "the board is running no declared workflow"))


# --------------------------------------------------------------------------
# the gates, which need nothing owned
# --------------------------------------------------------------------------


def test_an_unprivileged_caller_is_told_how_this_is_run() -> None:
    said: list[str] = []
    status = launcher.switchyard_adopt_workflow_command(
        SLUG, euid_getter=lambda: 1000, print_func=said.append
    )
    assert status == 1
    assert any(f"pkexec switchyard adopt-workflow {SLUG}" in line for line in said), said
    assert not any("uid" in line for line in said), said


def test_a_run_nobody_authorized_is_refused() -> None:
    """Acceptance: through Polkit, or not at all.

    Root alone is not authorization. A script that inherited root, or a sudo
    shell somebody left open, has no person to record the decision against --
    and the decision is the whole point of this command.
    """
    for operator in (SUDO_ONLY, NOBODY):
        said: list[str] = []
        status = launcher.switchyard_adopt_workflow_command(
            SLUG,
            euid_getter=lambda: 0,
            operator_resolver=lambda: operator,
            print_func=said.append,
        )
        assert status == 1, said
        assert any("authorized as one" in line for line in said), said
        assert any("pkexec switchyard adopt-workflow" in line for line in said), said


def test_the_command_is_discoverable_and_says_what_it_does() -> None:
    assert "adopt-workflow" in launcher.SWITCHYARD_COMMANDS
    assert "adopt-workflow" in launcher.switchyard_help_text()
    parser = launcher._build_switchyard_adopt_workflow_parser()
    args = parser.parse_args([SLUG])
    assert args.project == SLUG and args.apply is False and args.despite_board == ""
    assert parser.parse_args([SLUG, "--apply"]).apply is True
    assert parser.parse_args([SLUG, "--despite-board", "why"]).despite_board == "why"
    help_text = parser.format_help()
    assert "Polkit" in help_text and "rollout journal" in help_text, help_text


# --------------------------------------------------------------------------
# the privileged half
# --------------------------------------------------------------------------


def adopting_fixture(root: Path):
    """A tenant provisioned before root kept a workflow record."""
    release, home, installed, plan, config_path = fixture(root)
    declared = declared_document()
    tenant_plan = build_plan(
        project=SLUG,
        owner_user=TENANT,
        owner_home=home,
        source_repo=release,
        control_user="an-operator",
        workflow=declared,
    )
    # The tenant's own generated copies, as provisioning leaves them: the
    # configuration beside a plan.json that carries the declared workflow.
    with owner_patches(home):
        write_artifacts(tenant_plan, config_path.parent, enable_owner_linger=False)
    return release, home, installed, tenant_plan, config_path


def adopt(root: Path, home: Path, **kwargs) -> tuple[int, list[str]]:
    said: list[str] = []
    registry = root / "registry"
    registry.mkdir(exist_ok=True)
    with owner_patches(home):
        status = launcher.switchyard_adopt_workflow_command(
            SLUG,
            euid_getter=lambda: 0,
            operator_resolver=lambda: kwargs.pop("operator", OPERATOR),
            registry_dir=registry,
            print_func=said.append,
            **kwargs,
        )
    return status, said


def journal_entries(root: Path) -> list[dict]:
    """Each recorded attempt, as its own result document rather than the index.

    The index says an attempt happened; the result says what it decided and who
    authorized it, which is what this ticket asks to be kept.
    """
    from scripts.ticket_board.rollout_journal import attempts

    records = []
    for entry in attempts(SLUG, root=root / "journal"):
        directory = Path(entry["directory"])
        result = directory / "result.json"
        records.append(
            {**entry, **(json.loads(result.read_text(encoding="utf-8")) if result.is_file() else {})}
        )
    return records


def privileged_cases() -> int:
    checks = 0
    with tempfile.TemporaryDirectory(prefix="syrd166-privileged.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        os.environ[SEAM] = str(root)
        os.environ[JOURNAL_ENV] = str(root / "journal")
        # The authorization the command requires, as the journal reads it too:
        # inside this namespace the invoking human resolves to uid 0, and both
        # the command and the record it writes see the same mechanism.
        os.environ["PKEXEC_UID"] = "0"
        os.environ.pop("SUDO_USER", None)
        release, home, installed, tenant_plan, config_path = adopting_fixture(root)
        declared = tenant_plan.workflow
        record = launcher.workflow_record_path(SLUG)

        # 1. Nothing is recorded yet, which is the state this command exists for.
        assert not record.exists(), record
        with owner_patches(home), tenant_identity(home, owner=TENANT):
            rebuilt, problem = launcher.reconstruct_privileged_baseline(
                SLUG, config_path.parent, {"workflow": declared, "port": tenant_plan.port},
                source_repo=release,
            )
        assert rebuilt is not None, problem
        assert rebuilt.workflow_seed == "declared-unverified", rebuilt.workflow_seed
        checks += 1

        # 2. A dry run shows everything and changes nothing.
        status, said = adopt(root, home, board_reader=board(declared))
        assert status == 0, said
        assert not record.exists(), "a dry run must write nothing"
        joined = "\n".join(said)
        assert "proposed digest" in joined and "the board holds" in joined, joined[:400]
        assert '"schema": "switchyard.workflow.v1"' in joined, "the document itself is shown"
        assert "dry run; nothing was written" in joined, joined[-400:]
        assert "--apply" in joined
        checks += 1

        # 3. And the dry run is in the journal, with what it showed.
        entries = journal_entries(root)
        assert entries, "the decision has to be kept"
        assert entries[-1]["detail"] == "dry-run", entries[-1]
        assert entries[-1]["operator"] == OPERATOR.name, entries[-1]
        assert entries[-1]["operator_source"] == "pkexec", entries[-1]
        checks += 1

        # 4. Applying it writes root's own copy, and it reads back.
        status, said = adopt(root, home, apply=True, board_reader=board(declared))
        assert status == 0, said
        assert record.is_file(), record
        info = os.stat(record)
        assert info.st_uid == 0, info.st_uid
        # Root's own record of who this tenant's roles are and what they may
        # call is not something the accounts it describes get to read (SYRD-176).
        assert stat.S_IMODE(info.st_mode) == 0o600, oct(info.st_mode)
        stored, stored_problem = launcher.recorded_declared_workflow(SLUG)
        assert stored is not None, stored_problem
        assert stored == declared
        assert any("recorded" in line and OPERATOR.name in line for line in said), said
        assert journal_entries(root)[-1]["detail"] == "adopted"
        checks += 1

        # 5. Idempotent: again, and it says so without touching anything.
        before = (record.read_bytes(), record.stat().st_ino)
        status, said = adopt(root, home, apply=True, board_reader=board(declared))
        assert status == 0, said
        assert any("already holds" in line for line in said), said
        assert (record.read_bytes(), record.stat().st_ino) == before
        checks += 1

        # 6. What SYRD-165 promised, now true for this tenant: regeneration
        #    rebuilds the declared workflow from root's record, byte for byte.
        with owner_patches(home), tenant_identity(home, owner=TENANT):
            rebuilt, problem = launcher.reconstruct_privileged_baseline(
                SLUG, config_path.parent, {"workflow": declared, "port": tenant_plan.port},
                source_repo=release,
            )
        assert rebuilt is not None, problem
        assert rebuilt.workflow == declared
        assert render_workflow_sql(rebuilt) == render_workflow_sql(tenant_plan)
        assert "apply_declared_workflow" in render_workflow_sql(rebuilt)
        checks += 1

        # 7. A board running something else is a refusal, and the difference is
        #    shown rather than summarised.
        record.unlink()
        elsewhere = json.loads(json.dumps(declared))
        elsewhere["roles"][1]["capabilities"].append("resolve_publication")
        status, said = adopt(root, home, apply=True, board_reader=board(elsewhere))
        assert status == 1, said
        assert not record.exists(), "a refused adoption writes nothing"
        joined = "\n".join(said)
        assert "not the same" in joined and "resolve_publication" in joined, joined[-600:]
        assert "--despite-board" in joined
        assert journal_entries(root)[-1]["detail"] == "refused: board disagreement"
        checks += 1

        # 8. A board running nothing at all is the same refusal.
        status, said = adopt(root, home, apply=True, board_reader=board(None))
        assert status == 1, said
        assert any("no declared workflow" in line for line in said), said
        assert not record.exists()
        checks += 1

        # 9. The narrated recovery: the same disagreement, adopted on a stated
        #    reason, and the reason is in the journal with everything else.
        status, said = adopt(
            root, home, apply=True, board_reader=board(elsewhere),
            despite_board="the board lost its configuration in the incident",
        )
        assert status == 0, said
        assert record.is_file()
        assert any("adopting despite the board" in line for line in said), said
        stdout = (
            Path(journal_entries(root)[-1]["directory"]) / "stdout.log"
            if "directory" in journal_entries(root)[-1]
            else None
        )
        if stdout is not None and stdout.is_file():
            assert "lost its configuration" in stdout.read_text(encoding="utf-8")
        checks += 1

        # 10. A tenant plan anybody could rewrite is not evidence of anything.
        record.unlink()
        tenant_plan_path = config_path.parent / "plan.json"
        tenant_plan_path.chmod(0o664)
        status, said = adopt(root, home, apply=True, board_reader=board(declared))
        assert status == 1, said
        assert any("group or beyond can write" in line for line in said), said
        assert not record.exists()
        tenant_plan_path.chmod(0o644)
        checks += 1

        # 11. Nor is a symlink to one.
        original = tenant_plan_path.read_bytes()
        planted = root / "planted-plan.json"
        planted.write_bytes(original)
        tenant_plan_path.unlink()
        tenant_plan_path.symlink_to(planted)
        status, said = adopt(root, home, apply=True, board_reader=board(declared))
        assert status == 1, said
        assert any("symlink" in line for line in said), said
        assert not record.exists()
        tenant_plan_path.unlink()
        tenant_plan_path.write_bytes(original)
        checks += 1

        # 12. A document for another project, and one this release will not
        #     accept, are each refused by name.
        def rewrite(change) -> None:
            data = json.loads(original.decode("utf-8"))
            change(data)
            tenant_plan_path.write_text(json.dumps(data), encoding="utf-8")

        rewrite(lambda data: data["workflow"].update({"project": "somebody-else"}))
        status, said = adopt(root, home, apply=True, board_reader=board(declared))
        assert status == 1 and any("not 'testing'" in line for line in said), said

        rewrite(lambda data: data["workflow"].update({"roles": []}))
        status, said = adopt(root, home, apply=True, board_reader=board(declared))
        assert status == 1 and any("will not accept" in line for line in said), said

        rewrite(lambda data: data.pop("workflow"))
        status, said = adopt(root, home, apply=True, board_reader=board(declared))
        assert status == 1 and any("declares no workflow" in line for line in said), said

        tenant_plan_path.write_bytes(original)
        assert not record.exists(), "none of those may have written anything"
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
        print(f"adopt_workflow_test: privileged child ran {checks} checks")
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
        print("adopt_workflow_test: user namespaces unavailable; privileged half skipped")
    print(f"adopt_workflow_test: {checks} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
