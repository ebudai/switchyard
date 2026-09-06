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
    project_roles,
    projection_files,
    prepare_role,
    assign_projection_owner,
)
from scripts.workflow_manage import apply_files
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
            child = os.fork()
            if child == 0:
                try:
                    os.setgid(identity.pw_gid)
                    os.setuid(identity.pw_uid)
                    replacement = output / "plan.tmp"
                    replacement.write_bytes((output / "plan.json").read_bytes())
                    replacement.replace(output / "plan.json")
                    (output / "workflow.json").write_text(policy.read_text())
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
