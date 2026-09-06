#!/usr/bin/env python3
"""Portable role projection and exported-release provisioning without live tenants."""

import copy
import importlib.machinery
import importlib.util
import json
import os
import pwd
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.ticket_board.workflow_config import validate
from scripts.workflow_launcher import (
    board_unit_role_account_gap,
    project_roles,
    projection_files,
    prepare_role,
    assign_projection_owner,
)
from scripts import workflow_launcher
from scripts.workflow_manage import apply_files, assert_projection_writable
from scripts import team_launcher as launcher


def rejects(fn):
    try:
        fn()
    except ValueError:
        return
    raise AssertionError("expected invalid configuration rejection")


def provisioning_ownership(owner):
    """Run real file generation/chown in an isolated user namespace."""
    sys.path.insert(0, str(ROOT / "tests"))
    from team_launcher_test_helpers import FakeRunner

    identity = pwd.getpwnam(owner)
    assert os.geteuid() == 0 and identity.pw_uid != 0
    with tempfile.TemporaryDirectory(prefix="workflow-owner-") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        repository = root / "repository"
        repository.mkdir()
        for configured in (False, True):
            output = root / str(configured) / "provision"
            output.mkdir(parents=True)
            artifact = output.parent / "cerulean.project.json"
            artifact.write_text("{}")
            receipt = output / "desktop-receipt.json"
            receipt.write_text("protected receipt")
            unrelated = output / "unrelated" / "keep"
            unrelated.parent.mkdir()
            unrelated.write_text("unrelated")
            policy = root / "workflow.json"
            policy.write_text((ROOT / "examples/workflows/inspection.json").read_text())
            host = FakeRunner()
            assigned = []

            def runner(args, **kwargs):
                if args[0] == "chown":
                    assert len(args) == 3 and args[1] == f"{owner}:{owner}"
                    path = Path(args[-1])
                    assert path.is_relative_to(root)
                    assigned.append(path)
                    return subprocess.run(args, **kwargs)
                # Provisioning/services/auth commands remain simulated. Only
                # scoped chown executes, against disposable namespace files.
                return host(args, **kwargs)

            assert (
                launcher.new_project_command(
                    "cerulean",
                    owner_user=owner,
                    owner_home=root / "owner-home",
                    repository=repository,
                    source_repo=ROOT,
                    output_dir=output,
                    workflow_config=policy if configured else None,
                    execute=True,
                    runner=runner,
                    port_in_use=lambda port: False,
                    socket_exists=lambda path: False,
                    print_func=lambda text: None,
                )
                == 0
            )
            config = output / "cerulean.json"
            raw = json.loads(config.read_text())
            layout = Path(raw["layout"])
            if not layout.is_absolute():
                layout = output / layout
            expected = {output, config, layout, output / "plan.json", artifact}
            if (output / "workflow.json").exists():
                expected.add(output / "workflow.json")
            assert set(assigned) == expected, assigned
            assert all(p.stat().st_uid == identity.pw_uid for p in expected)
            assert all(p.stat().st_gid == identity.pw_gid for p in expected)
            assert receipt.stat().st_uid == unrelated.stat().st_uid == 0
            assert unrelated.parent.stat().st_uid == 0
            assert receipt.read_text() == "protected receipt"
            # The owner can atomically update controls and add its first
            # workflow after privileged setup, without another chown.
            # Provisioning hands the project's .switchyard directory to the
            # tenant with a recursive chown that this fixture simulates, so the
            # artifact's parent is tenant-owned in production. Reproduce that,
            # or the projection below is judged against a shape no real tenant
            # has.
            os.chown(output.parent, identity.pw_uid, identity.pw_gid)
            unit = output / json.loads((output / "plan.json").read_text())["board_unit"]
            assert unit.is_file() and unit.stat().st_uid == 0, unit
            child = os.fork()
            if child == 0:
                try:
                    os.setgid(identity.pw_gid)
                    os.setuid(identity.pw_uid)
                    replacement = output / "plan.tmp"
                    replacement.write_bytes((output / "plan.json").read_bytes())
                    replacement.replace(output / "plan.json")
                    (output / "workflow.json").write_text(policy.read_text())
                    # SYRD-39 x SYRD-36: the tenant, not root, runs role-prompt
                    # and workflow apply. Root installs the board unit, so the
                    # tenant cannot write it -- and must not have to. The whole
                    # projection has to pass the same writability preflight the
                    # apply path runs before it touches the board, and the
                    # role-account table has to be updated by that same
                    # unprivileged write.
                    document = json.loads(policy.read_text())
                    assert not os.access(unit, os.W_OK), unit
                    files = projection_files(config, document)
                    assert unit not in files, sorted(f.name for f in files)
                    assert_projection_writable(files)
                    apply_files(files)
                    written = json.loads((output / "plan.json").read_text())
                    accounts = {role: account for role, account in written["role_accounts"]}
                    assert accounts["inspector"] == "cerulean-inspector", accounts
                    assert len(set(accounts.values())) == len(accounts), accounts
                    assert (
                        json.loads(config.read_text())["roles"]
                        == project_roles(json.loads(config.read_text()), document)["roles"]
                    )
                    # The roles the still-unrefreshed unit cannot resolve are
                    # named, so the operator step is stated rather than found
                    # later as a role that silently cannot write.
                    assert "inspector" in board_unit_role_account_gap(written, output)
                except BaseException:
                    import traceback

                    traceback.print_exc()
                    os._exit(1)
                os._exit(0)
            assert os.waitpid(child, 0)[1] == 0


def main():
    owner = pwd.getpwuid(os.geteuid()).pw_name
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--ownership-child",
        owner,
    ]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", *command]
    else:
        command[-1] = "www-data"
    subprocess.run(command, check=True)
    with tempfile.TemporaryDirectory(prefix="workflow-export-") as tmp:
        root = Path(tmp)
        release = root / "release"
        shutil.copytree(ROOT / "scripts", release / "scripts")
        cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
        onboarding = root / "INSPECTOR_ONBOARDING.md"
        onboarding.write_text(
            "Inspect the actual served release. File reproducible findings. Do not implement fixes.\n"
        )
        next(r for r in cfg["roles"] if r["name"] == "inspector")["onboarding"] = str(
            onboarding
        )
        for enabled in [True, False]:
            document = copy.deepcopy(cfg)
            if not enabled:
                # Disable inspection by data: retire its role and remove its empty stage.
                inspector = next(
                    r for r in document["roles"] if r["name"] == "inspector"
                )
                inspector.update(active=False, slot=None)
                document["remove_stages"] = ["inspection"]
                for role in document["roles"]:
                    if role.get("slot") is not None:
                        role["slot"] -= 1
                document["stages"] = [
                    s for s in document["stages"] if s["name"] != "inspection"
                ]
                document["transitions"] = [
                    tr for tr in document["transitions"] if tr["from"] != "inspection"
                ]
                for tr in document["transitions"]:
                    if tr["to"] == "inspection":
                        tr["to"] = "audit"
            document = validate(document)
            policy = root / f"workflow-{enabled}.json"
            policy.write_text(json.dumps(document))
            output = root / f"provision-{enabled}"
            command = [
                sys.executable,
                str(release / "scripts/ticket_board/project_provision.py"),
                "--project",
                "cerulean",
                "--owner-user",
                "cerulean-worker",
                "--workflow-config",
                str(policy),
                "--output-dir",
                str(output),
            ]
            # Execute the standalone entry point from an export with no .git and unrelated cwd.
            proc = subprocess.run(command, cwd=root, capture_output=True, text=True)
            assert proc.returncode == 0, proc.stderr
            plan = json.loads((output / "plan.json").read_text())
            assert plan["workflow"] == document
            assert (
                "apply_declared_workflow"
                in (output / "cerulean-workflow.sql").read_text()
            )
            assert (
                "DELETE FROM ticket_board.workflow_stages"
                not in (output / "cerulean-workflow.sql").read_text()
            )
            # Full generated launcher uses the declared visible count, not the
            # legacy designer/auditor defaults; metadata survives artifact reuse.
            artifact = output / "cerulean.project.json"
            artifact.write_text("{}")
            provision = launcher._project_board_provision_from_json(
                output / "plan.json"
            )
            generated = launcher.write_new_project_launcher_artifacts(
                provision,
                output,
                repository=root / "project",
                artifact_path=artifact,
                print_func=lambda text: None,
            )
            actual = json.loads(generated.read_text())
            assert sum("slot" in role for role in actual["roles"]) == (
                6 if enabled else 5
            )
            assert json.loads(
                (output / "cerulean-konsole-layout.json").read_text()
            ) == launcher._new_project_layout_payload(6 if enabled else 5)
            assert json.loads(artifact.read_text())["workflow"] == document

            raw = {
                "project": "cerulean",
                "layout": "layout.json",
                "repository": str(root / "project"),
                "worktree_base": str(root / "worktrees"),
                "desktop_access": {"mode": "headless"},
                "roles": [
                    {
                        "role": "designer",
                        "slot": 0,
                        "cli": ["claude", "--model", "sonnet"],
                        "env": {
                            "KEEP": "retained",
                            "SWITCHYARD_DESIGNER_ONBOARDING": "old",
                        },
                        "workdir": str(root / "old-designer"),
                    }
                ],
            }
            projected = project_roles(raw, document)
            roles = {r["role"]: r for r in projected["roles"]}
            assert ("inspector" in roles) == enabled and "designer" not in roles
            assert projected["retired_workflow_roles"]["designer"] == raw["roles"][0]
            assert projected["desktop_access"] == raw["desktop_access"]
            assert project_roles(projected, document) == projected
            if enabled:
                assert roles["inspector"]["cli"] == ["claude", "--model", "sonnet"]
                assert roles["inspector"]["env"]["TICKET_BOARD_ROLE_ONBOARDING"] == str(
                    onboarding
                )
                assert roles["inspector"]["slot"] == 0
                assert "SWITCHYARD_DESIGNER_ONBOARDING" not in roles["inspector"]["env"]
                assert roles["inspector"]["workdir"] == str(
                    root / "worktrees/inspector"
                )
                # SYRD-39: inspector is introduced from the designer TEMPLATE.
                # Deep-copying that template carried the designer's Unix account
                # over, so on an isolated tenant the board -- which resolves
                # authority from the peer uid -- would have read every inspector
                # write as the designer, and the inspector could not have acted
                # as itself at all. Identity is never inherited.
                isolated_raw = copy.deepcopy(raw)
                isolated_raw["roles"][0]["run_as_user"] = "cerulean-designer"
                isolated = project_roles(isolated_raw, document)
                accounts = {
                    r["role"]: r.get("run_as_user") for r in isolated["roles"]
                }
                assert accounts["inspector"] == "cerulean-inspector", accounts
                assert "cerulean-designer" not in accounts.values(), accounts
                assert all(accounts.values()), accounts
                assert len(set(accounts.values())) == len(accounts), accounts
                assert project_roles(isolated, document) == isolated
                # Two roles on one account is refused rather than written: the
                # board refuses both roles when a uid backs two, so writing it
                # would take the colliding roles off the board entirely.
                collided = copy.deepcopy(isolated_raw)
                collided["roles"].append(
                    {"role": "main", "run_as_user": "cerulean-inspector"}
                )
                rejects(lambda: project_roles(collided, document))
                # The declarative addition must also reach the table the board
                # resolves uids through and the trees the rollout hands out.
                iso_dir = root / "isolated-provision"
                shutil.copytree(output, iso_dir)
                iso_plan_path = iso_dir / "plan.json"
                iso_plan = json.loads(iso_plan_path.read_text())
                # A plan generated before the role was declared has no row for it.
                iso_plan["role_accounts"] = [
                    entry
                    for entry in iso_plan["role_accounts"]
                    if entry[0] != "inspector"
                ]
                iso_plan["role_worktrees"] = [
                    [role, str(root / "worktrees" / role)]
                    for role, _account in iso_plan["role_accounts"]
                ]
                iso_plan_path.write_text(json.dumps(iso_plan))
                iso_config = iso_dir / "cerulean.json"
                iso_config.write_text(json.dumps(isolated_raw))
                (iso_dir / "layout.json").write_text("{}")
                iso_files = projection_files(iso_config, document)
                refreshed = json.loads(iso_files[iso_plan_path])
                assert ["inspector", "cerulean-inspector"] in [
                    list(entry) for entry in refreshed["role_accounts"]
                ], refreshed["role_accounts"]
                assert ["inspector", str(root / "worktrees/inspector")] in [
                    list(entry) for entry in refreshed["role_worktrees"]
                ], refreshed["role_worktrees"]
                # The generated systemd unit is NOT part of the projection.
                # Provisioning keeps it out of tenant ownership because root
                # installs it, and this projection runs unprivileged as the
                # tenant: including it would make every role-prompt and workflow
                # apply on a root-provisioned project fail its writability
                # preflight. The refresh is an operator step, and the roles
                # waiting on it are named rather than left to be discovered.
                unit_path = iso_dir / iso_plan["board_unit"]
                assert unit_path.exists()
                assert unit_path not in iso_files, sorted(f.name for f in iso_files)
                assert "inspector=cerulean-inspector" not in unit_path.read_text()
                # ops is declared by the document too and was never in the
                # CLI-derived table the unit was generated from, so both roles
                # are waiting on the same operator refresh.
                assert sorted(
                    workflow_launcher.board_unit_role_account_gap(refreshed, iso_dir)
                ) == ["inspector", "ops"], workflow_launcher.board_unit_role_account_gap(
                    refreshed, iso_dir
                )
            foreign = copy.deepcopy(document)
            next(r for r in foreign["roles"] if r["name"] == "main")[
                "target"
            ] = "foreign-main:0.0"
            rejects(lambda: project_roles(raw, foreign))
            config = output / "cerulean.json"
            config.write_text(json.dumps(raw))
            (output / "layout.json").write_text("{}")
            original = config.read_bytes()
            rejects(lambda: projection_files(config, foreign))
            assert config.read_bytes() == original
            files = projection_files(config, document)
            apply_files(files)
            before = {p: p.read_bytes() for p in files}
            apply_files(projection_files(config, document))
            assert before == {p: p.read_bytes() for p in files}
            assert (
                json.loads(config.read_text())["desktop_access"]
                == raw["desktop_access"]
            )
            loaded = launcher.load_project_config("cerulean", config)
            owned = []
            with patch.object(
                launcher,
                "ensure_owner_file",
                side_effect=lambda c, p, **kw: owned.append(p),
            ):
                assign_projection_owner(loaded, config, runner=lambda *a, **kw: None)
            assert (
                config.parent in owned
                and config in owned
                and output / "plan.json" in owned
            )
            assert all(
                p == config.parent or p in projection_files(config, document)
                for p in owned
            )

            assert ("inspector" in {r.role for r in loaded.roles}) == enabled
            if enabled:
                assert (
                    loaded.roles[0].role == "director"
                )  # identity remains independent of visual slot order
                # Shared prep path sees only inspector and cannot restart another role.
                events = []
                with patch.object(
                    launcher,
                    "ensure_project_worktrees",
                    side_effect=lambda c, **kw: (
                        events.append(("worktree", [r.role for r in c.roles]))
                        or type("Result", (), {"ok": True})()
                    ),
                ), patch.object(
                    launcher,
                    "ensure_generated_project_pane_hooks",
                    side_effect=lambda *a, **kw: events.append(("hooks",)),
                ), patch.object(
                    launcher,
                    "run_switchyard_launch_first_run_auth",
                    side_effect=lambda c, **kw: (
                        events.append(("trust", [r.role for r in c.roles]))
                        or launcher.FirstRunAuthReport({}, [])
                    ),
                ):
                    prepare_role(config, "inspector")
                assert events == [
                    ("worktree", ["inspector"]),
                    ("hooks",),
                    ("trust", ["inspector"]),
                ]
        # Execute the actual SessionStart helper to prove the remit is injected.
        loader = importlib.machinery.SourceFileLoader(
            "workflow_idle_hook", str(release / "scripts/ticket-board-pane-idle-hook")
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        hook = importlib.util.module_from_spec(spec)
        loader.exec_module(hook)
        result = hook._session_start_additional_context_output(
            target="cerulean-inspector:0.0",
            payload={"source": "startup"},
            environ={
                "TICKET_BOARD_PROJECT": "cerulean",
                "TICKET_BOARD_ROLE_ONBOARDING": str(onboarding),
            },
        )
        assert (
            onboarding.read_text().strip()
            in result["hookSpecificOutput"]["additionalContext"]
        )
    print(
        "declarative launcher: exported enabled/disabled provisioning, project target guard, preserved designer/desktop, idempotent projection and SessionStart remit passed"
    )
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--ownership-child":
        provisioning_ownership(sys.argv[2])
    else:
        raise SystemExit(main())
