#!/usr/bin/env python3
"""Privileged desktop-policy upgrade with real isolated tenant filesystem access."""

import json
import os
from pathlib import Path
import pwd
import stat
import subprocess
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from scripts import desktop_access as desktop
from scripts import team_launcher as launcher
from team_launcher_test_helpers import FakeRunner


def metadata(path):
    info = path.stat()
    return info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode), info.st_ino


def tenant_access(owner, controls, protected):
    child = os.fork()
    if child == 0:
        try:
            os.setgroups([])
            os.setgid(owner.pw_gid)
            os.setuid(owner.pw_uid)
            for path in controls:
                with path.open("r+") as stream:
                    data = stream.read()
                    json.loads(data)
                    stream.seek(0)
                    stream.write(data)
                    stream.truncate()
            try:
                protected.read_bytes()
            except PermissionError:
                pass
            else:
                raise AssertionError("tenant could read protected GUI receipt")
        except BaseException:
            import traceback

            traceback.print_exc()
            os._exit(1)
        os._exit(0)
    assert os.waitpid(child, 0)[1] == 0


def ownership_cases(owner_name):
    assert os.geteuid() == 0
    owner = pwd.getpwnam(owner_name)
    assert owner.pw_uid != 0
    with tempfile.TemporaryDirectory(prefix="desktop-policy-owner-") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        for initially_root in (False, True):
            output = root / str(initially_root) / "provision"
            output.mkdir(parents=True)
            policy_file = output / "desktop-policy.json"
            policy_file.write_text(json.dumps({"mode": "headless"}))
            plan = launcher.build_plan(
                project="cerulean",
                owner_user=owner_name,
                owner_home=root / "owner-home",
                source_repo=ROOT,
            )
            launcher.write_artifacts(plan, output, enable_owner_linger=False)
            config_path = launcher.write_new_project_launcher_artifacts(
                plan,
                output,
                repository=root / "repository",
                print_func=lambda text: None,
            )
            os.chown(output, owner.pw_uid, owner.pw_gid)
            output.chmod(0o700)
            controls = [config_path, policy_file]
            modes = {
                config_path: 0o600,
                policy_file: 0o600 if initially_root else 0o640,
            }
            for path in controls:
                os.chown(
                    path,
                    0 if initially_root else owner.pw_uid,
                    0 if initially_root else owner.pw_gid,
                )
                path.chmod(modes[path])
            protected = output / "gui-receipt.json"
            protected.write_text("protected GUI evidence")
            protected.chmod(0o600)
            unrelated = output / "unrelated"
            unrelated.mkdir()
            sentinel = unrelated / "keep"
            sentinel.write_text("unrelated file")
            sentinel.chmod(0o600)
            before = {
                p: (metadata(p), p.read_bytes() if p.is_file() else None)
                for p in [protected, unrelated, sentinel]
            }
            parent_before = metadata(output)
            chosen = root / "wayland.json"
            chosen.write_text(
                json.dumps(
                    {
                        "mode": "wayland",
                        "gui_user": "root",
                        "tenant_user": owner_name,
                        "project": "cerulean",
                        "wayland_display": "auto",
                        "consent": {
                            "approved": True,
                            "by": "root",
                            "at": "2026-09-06T06:17:27Z",
                            "reference": "isolated namespace test consent",
                        },
                    }
                )
            )
            host = FakeRunner()
            replacements = []
            actual_replace = Path.replace

            def replace(source, target):
                target = Path(target)
                if target in controls:
                    # Inspect the actual temporary inode before publication.
                    assert metadata(source)[:3] == (
                        owner.pw_uid,
                        owner.pw_gid,
                        modes[target],
                    )
                    replacements.append(target)
                return actual_replace(source, target)

            def runner(args, **kwargs):
                if "verify" in args:
                    return subprocess.CompletedProcess(
                        args,
                        0,
                        json.dumps(
                            {
                                "WAYLAND_DISPLAY": str(root / "gui-runtime/wayland-0"),
                                "XDG_RUNTIME_DIR": str(root / "tenant-runtime"),
                                "DBUS_SESSION_BUS_ADDRESS": "unix:path="
                                + str(root / "tenant-runtime/bus"),
                            }
                        ),
                        "",
                    )
                if args[0] == "chown":
                    assert len(args) == 3 and Path(args[-1]).is_relative_to(root)
                    return subprocess.run(args, **kwargs)
                return host(args, **kwargs)

            # Actual full upgrade entry and generated layout/runtime paths.
            # Only external GUI install/readiness and host commands are simulated.
            with patch.object(desktop, "install"), patch.object(
                desktop, "uninstall"
            ), patch.object(Path, "replace", replace):
                for choice in [Path("headless"), Path("headless"), chosen, chosen]:
                    config = launcher.load_project_config("cerulean", config_path)
                    assert (
                        launcher.upgrade_project_command(
                            config,
                            config_path=config_path,
                            desktop_policy=choice,
                            source_repo=ROOT,
                            runner=runner,
                            print_func=lambda text: None,
                        )
                        == 0
                    )
                    for path in controls:
                        assert metadata(path)[:3] == (
                            owner.pw_uid,
                            owner.pw_gid,
                            modes[path],
                        )
                    tenant_access(owner, controls, protected)
                    assert metadata(output) == parent_before
                    assert before == {
                        p: (metadata(p), p.read_bytes() if p.is_file() else None)
                        for p in before
                    }
                assert (
                    replacements.count(config_path)
                    == replacements.count(policy_file)
                    == 4
                )
                # A dry-run must preserve bytes, ownership, modes and inodes.
                snapshots = {p: (metadata(p), p.read_bytes()) for p in controls}
                launcher.upgrade_project_command(
                    launcher.load_project_config("cerulean", config_path),
                    config_path=config_path,
                    desktop_policy=Path("headless"),
                    dry_run=True,
                    source_repo=ROOT,
                    runner=runner,
                    print_func=lambda text: None,
                )
                assert snapshots == {p: (metadata(p), p.read_bytes()) for p in controls}

            # Ownership failure must not publish a replacement or leak its temp file.
            snapshots = {p: (metadata(p), p.read_bytes()) for p in controls}
            with patch.object(
                os, "fchown", side_effect=PermissionError("test ownership denied")
            ), patch.object(desktop, "install"), patch.object(desktop, "uninstall"):
                try:
                    launcher.configure_project_desktop(
                        launcher.load_project_config("cerulean", config_path),
                        config_path=config_path,
                        policy_path=chosen,
                        runner=runner,
                    )
                except PermissionError:
                    pass
                else:
                    raise AssertionError("ownership failure was ignored")
            assert snapshots == {p: (metadata(p), p.read_bytes()) for p in controls}
            assert not list(output.glob(".*.tmp"))
            # A policy file absent in an older installation starts private and
            # tenant-owned too; no existing metadata is required for the repair.
            policy_file.unlink()
            with patch.object(desktop, "install"), patch.object(desktop, "uninstall"):
                launcher.configure_project_desktop(
                    launcher.load_project_config("cerulean", config_path),
                    config_path=config_path,
                    policy_path=Path("headless"),
                    runner=runner,
                )
            assert metadata(policy_file)[:3] == (owner.pw_uid, owner.pw_gid, 0o600)
            tenant_access(owner, controls, protected)


def main():
    owner = pwd.getpwuid(os.geteuid()).pw_name if os.geteuid() else "www-data"
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--ownership-child",
        owner,
    ]
    if os.geteuid() != 0:
        command = ["unshare", "--user", "--map-auto", "--map-root-user", *command]
    subprocess.run(command, check=True)
    print(
        "desktop policy ownership: real privileged repeated upgrades, atomic owner/mode, tenant access, protected files, dry-run and failure cleanup passed"
    )


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--ownership-child":
        ownership_cases(sys.argv[2])
    else:
        main()
