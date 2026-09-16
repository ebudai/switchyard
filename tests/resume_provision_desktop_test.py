#!/usr/bin/env python3
"""SYRD-158: a recovery that verified desktop access it had never installed.

Testing journal 0014 ran the supported continuation. It installed release
1755eec, regenerated the artifacts, registered the preserved tenant -- and then
stopped:

    switchyard: desktop setup incomplete before launch: switchyard desktop
    access: Desktop access is not ready before launch: [Errno 13] Permission
    denied: '/run/user/1000/switchyard-desktop-1000-1013-testing.json'

Journal 0015 read the host back. The approved policy was intact and root-owned;
`/run/user/1000` carried `user:1001:--x` and `user:1006:--x` and nothing for
uid 1013; the Wayland socket carried `user:1001:rw-` and `user:1006:rw-` and
nothing for uid 1013; and the GUI owner's persistent service did not exist at
all. Nothing had ever installed the grant, so there was no receipt for the
tenant to read and no ACL letting it reach the directory the receipt is in.

`switchyard new` installs the scoped grant and then verifies it. A launch only
ever verifies. The recovery went straight to launching, so it asked the tenant
to prove access nobody had given it.

This suite is that state and what the recovery now does with it: the install
comes first, from the policy this host's root-owned approval record covers and
not from whatever the tenant's own configuration says, and roles start only
after it succeeds. The grant machinery is exercised against real ACLs on real
files, because "preserves the other tenants' entries" is not a claim worth
making against a mock.
"""

from __future__ import annotations

import json
import os
import pwd
import stat
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts import desktop_access as desktop  # noqa: E402
from scripts import team_launcher as launcher  # noqa: E402
from resume_provision_test import SEAM, SLUG, TENANT, namespaces_available  # noqa: E402
from resume_provision_continuation_test import (  # noqa: E402
    OWNER_UID,
    fixture,
    generated_config,
    live_sessions,
    owner_patches,
    quiet,
)

GUI_USER = "a-desktop-owner"
GUI_UID = 7100
TENANT_UID = 7113


def approval(tmp: Path, *, gui_user: str = GUI_USER) -> Path:
    """This host's root-owned record that a desktop owner approved access."""
    path = tmp / "desktop-approval.json"
    path.write_text(
        json.dumps(
            {
                "gui_user": gui_user,
                "approved_by": gui_user,
                "approved_at": "2026-09-14T12:43:33Z",
                "reference": f"{gui_user} approved Switchyard desktop access on this host",
            }
        ),
        encoding="utf-8",
    )
    return path


def wayland_policy(*, gui_user: str = GUI_USER, tenant: str = TENANT, project: str = SLUG) -> dict:
    return {
        "mode": "wayland",
        "project": project,
        "tenant_user": tenant,
        "gui_user": gui_user,
        "wayland_display": "auto",
        "consent": {
            "approved": True,
            "at": "2026-09-15T12:24:03Z",
            "by": gui_user,
            "reference": f"{gui_user} approved Switchyard desktop access on this host",
        },
    }


# --------------------------------------------------------------------------
# which policy root is willing to install
# --------------------------------------------------------------------------


def test_the_approved_policy_is_the_one_this_host_recorded() -> None:
    """The tenant's configuration may carry the grant; it may not choose it.

    A tenant that edits its own configuration is asking for access, and asking
    is not authorization. The account whose session may be reached comes from
    the root-owned approval record, and a configuration naming a different one
    is refused rather than reconciled.
    """
    with tempfile.TemporaryDirectory(prefix="syrd158-policy.") as tmp:
        tmp_path = Path(tmp)
        plan, _home, _config_path, config = generated_config(tmp_path)
        record = approval(tmp_path)

        # The state journal 0015 recorded: an approved policy, intact.
        approved = replace(config, desktop_access=wayland_policy())
        policy, objections = launcher.approved_desktop_policy(plan, approved, approval_path=record)
        assert objections == [], objections
        assert policy is not None and policy["gui_user"] == GUI_USER
        assert policy["tenant_user"] == TENANT and policy["project"] == SLUG

        # Somebody else's desktop, asked for by the account every role runs as.
        elsewhere = replace(config, desktop_access=wayland_policy(gui_user="somebody-else"))
        policy, objections = launcher.approved_desktop_policy(plan, elsewhere, approval_path=record)
        assert policy is None
        assert any("somebody-else" in line and GUI_USER in line for line in objections), objections
        assert any("not the tenant's to answer" in line for line in objections), objections

        # A grant on a host that never recorded an approval at all.
        policy, objections = launcher.approved_desktop_policy(
            plan, approved, approval_path=tmp_path / "nothing-here.json"
        )
        assert policy is None
        assert any("no recorded desktop approval" in line for line in objections), objections

        # Headless asks for nothing, so there is nothing to install.
        headless = replace(config, desktop_access={"mode": "headless"})
        assert launcher.approved_desktop_policy(plan, headless, approval_path=record) == (None, [])

        # And a project with no policy on a host with an approval gets the one
        # provisioning would have generated -- no prompt, no new consent.
        bare = replace(config, desktop_access=None)
        policy, objections = launcher.approved_desktop_policy(plan, bare, approval_path=record)
        assert objections == [] and policy is not None, objections
        assert policy["gui_user"] == GUI_USER and policy["mode"] == "wayland"
        assert policy["consent"]["at"] == "2026-09-14T12:43:33Z"
        # Generated from the record, so generating it again says the same thing.
        again, _ = launcher.approved_desktop_policy(plan, bare, approval_path=record)
        assert desktop.digest(again) == desktop.digest(policy)


def test_a_headless_host_with_a_headless_tenant_installs_nothing() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd158-headless.") as tmp:
        tmp_path = Path(tmp)
        plan, _home, _config_path, config = generated_config(tmp_path)
        bare = replace(config, desktop_access=None)
        assert launcher.approved_desktop_policy(
            plan, bare, approval_path=tmp_path / "nothing-here.json"
        ) == (None, [])


# --------------------------------------------------------------------------
# the receipt, the uid in its name, and the grants around it
# --------------------------------------------------------------------------


def _accounts():
    """A GUI owner and a tenant with distinct uids, as the two of them are."""
    people = {
        GUI_USER: SimpleNamespace(pw_name=GUI_USER, pw_uid=GUI_UID, pw_gid=GUI_UID, pw_dir="/nonexistent"),
        TENANT: SimpleNamespace(pw_name=TENANT, pw_uid=TENANT_UID, pw_gid=TENANT_UID, pw_dir="/nonexistent"),
    }
    return patch.object(desktop.pwd, "getpwnam", side_effect=lambda name: people[name])


def test_the_receipt_is_named_for_the_recovered_tenant() -> None:
    """Not for whoever is running the recovery.

    The live receipt is `switchyard-desktop-1000-1013-testing.json`: the GUI
    owner's uid, the tenant's uid, the project. A recovery driven by root must
    still name the tenant it is recovering, or it writes a receipt nobody reads
    and grants a uid nobody asked about.
    """
    with _accounts():
        _gui, _tenant, name = desktop.names(wayland_policy())
    assert name == f"switchyard-desktop-{GUI_UID}-{TENANT_UID}-{SLUG}"
    _prefix, gui_field, tenant_field, project_field = name.rsplit("-", 3)
    assert (gui_field, tenant_field, project_field) == (str(GUI_UID), str(TENANT_UID), SLUG)
    assert os.getuid() not in {int(gui_field), int(tenant_field)}


def test_a_receipt_anybody_could_rewrite_is_refused() -> None:
    """Readiness is a claim about a file, so the file has to be protected.

    The tenant reads this receipt to learn which socket it may use. A receipt
    the tenant -- or anyone else -- could write is a receipt that says whatever
    the reader wants, so `verify` refuses one that is not the GUI owner's own
    and unwritable by everybody else.
    """
    with tempfile.TemporaryDirectory(prefix="syrd158-receipt.") as tmp:
        tmp_path = Path(tmp)
        gui_runtime = tmp_path / "gui-runtime"
        tenant_runtime = tmp_path / "tenant-runtime"
        gui_runtime.mkdir()
        tenant_runtime.mkdir()
        policy = wayland_policy()
        with _accounts():
            _gui, _tenant, name = desktop.names(policy)
        receipt = gui_runtime / (name + ".json")
        receipt.write_text(json.dumps({"policy_digest": "no"}), encoding="utf-8")

        runtimes = {GUI_USER: gui_runtime, TENANT: tenant_runtime}

        def attempt() -> str:
            with _accounts():
                with patch.object(desktop.os, "geteuid", lambda: TENANT_UID):
                    with patch.object(desktop, "runtime_for", lambda user: runtimes[user.pw_name]):
                        try:
                            desktop.verify(policy)
                        except desktop.DesktopAccessError as exc:
                            return str(exc)
            raise AssertionError("verify accepted a receipt it should have refused")

        # Owned by this process, which is not the GUI owner in this fixture.
        assert "not protected by the GUI owner" in attempt()

        # Owned by the GUI owner but writable by the world is the same problem.
        with patch.object(desktop.os, "lstat", lambda path: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o646, st_uid=GUI_UID
        )):
            assert "not protected by the GUI owner" in attempt()

        # Protected, but describing a policy that is not this one.
        with patch.object(desktop.os, "lstat", lambda path: SimpleNamespace(
            st_mode=stat.S_IFREG | 0o644, st_uid=GUI_UID
        )):
            assert "has not been installed by its GUI owner" in attempt()


def test_a_grant_leaves_every_other_tenant_where_it_was() -> None:
    """Real ACLs on a real file, because that is what the claim is about.

    Journal 0015 shows `/run/user/1000` carrying `user:1001:--x` and
    `user:1006:--x` -- two other tenants, one of them this project. Adding uid
    1013 must not disturb either of them, and must not widen anything through
    the mask.
    """
    if not foreign_uid_acls_available():
        # A user namespace maps one uid, so an entry for somebody else cannot
        # be written there at all. This case belongs to the half of the suite
        # that runs as an ordinary account, where those uids are real.
        return
    with tempfile.TemporaryDirectory(prefix="syrd158-acl.") as tmp:
        runtime = Path(tmp) / "runtime"
        runtime.mkdir(mode=0o710)
        subprocess.run(["setfacl", "-m", "u:1001:--x,u:1006:--x", str(runtime)], check=True)
        before = desktop.acl_map(desktop.read_acl(runtime))
        assert before["user:1001"] == "--x" and before["user:1006"] == "--x"

        desktop.grant(runtime, TENANT_UID, "--x")

        after = desktop.acl_map(desktop.read_acl(runtime))
        assert after[f"user:{TENANT_UID}"] == "--x", after
        for peer in ("user:1001", "user:1006"):
            assert after[peer] == before[peer], (peer, before, after)
        # Effective access, not just the entry: a widened mask would show here.
        assert desktop.effective_acl(runtime, 1001) == 1
        assert desktop.effective_acl(runtime, 1006) == 1
        assert desktop.effective_acl(runtime, TENANT_UID) == 1

        # A socket the other tenants can read and write keeps exactly that.
        socket_path = Path(tmp) / "wayland-0"
        socket_path.touch(mode=0o775)
        subprocess.run(["setfacl", "-m", "u:1001:rw-,u:1006:rw-", str(socket_path)], check=True)
        desktop.grant(socket_path, TENANT_UID, "rw-")
        assert desktop.effective_acl(socket_path, 1001) == 6
        assert desktop.effective_acl(socket_path, 1006) == 6
        assert desktop.effective_acl(socket_path, TENANT_UID) == 6


def foreign_uid_acls_available() -> bool:
    """Whether this process can write an ACL entry for another account."""
    import shutil

    if not shutil.which("setfacl") or not shutil.which("getfacl"):
        return False
    with tempfile.TemporaryDirectory(prefix="syrd158-acl-probe.") as tmp:
        probe = Path(tmp) / "probe"
        probe.touch()
        done = subprocess.run(
            ["setfacl", "-m", "u:1001:r--", str(probe)], capture_output=True, text=True
        )
        return done.returncode == 0


# --------------------------------------------------------------------------
# the continuation, inside a user namespace
# --------------------------------------------------------------------------


def privileged_cases() -> int:
    checks = 0
    with tempfile.TemporaryDirectory(prefix="syrd158-privileged.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        os.environ[SEAM] = str(root)
        registry = root / "registry"
        registry.mkdir()
        release, home, _installed, plan, config_path = fixture(root)
        record = approval(root)

        # The state journal 0015 recorded: an approved policy on disk, and no
        # receipt, service or ACL anywhere.
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        payload["desktop_access"] = wayland_policy()
        config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with owner_patches(home):
            config = launcher.load_project_config(SLUG, config_path)
        commands, statuses = live_sessions(config)
        entry = registry / f"{SLUG}.json"

        order: list[str] = []
        installs: list[dict] = []

        def desktop_installer(cfg, *, config_path, helper, runner):
            order.append("desktop")
            installs.append({"policy": cfg.desktop_access, "helper": helper, "config_path": config_path})
            return cfg

        def failing_installer(cfg, **kwargs):
            order.append("desktop")
            raise SystemExit("switchyard: desktop setup incomplete before launch: no Wayland session")

        def fake_launch(cfg, *, config_path, mode, **kwargs):
            order.append("launch")
            return 0

        def resume(*, installer=desktop_installer, approval_path=None, launch=fake_launch, **kwargs):
            said: list[str] = []
            with owner_patches(home):
                with patch.object(launcher, "launch_project", launch):
                    status = launcher.switchyard_resume_provision_command(
                        SLUG,
                        source_repo=release,
                        registry_dir=registry,
                        euid_getter=lambda: 0,
                        completion_reader=lambda _plan: launcher.PacketCompletion(()),
                        desktop_approval_path=record if approval_path is None else approval_path,
                        # Runtime registration is the panes' own asynchronous
                        # work; these cases are about the desktop install that
                        # precedes them (SYRD-162).
                        registration=launcher.RuntimeRegistrationWait(),
                        desktop_installer=installer,
                        process_commands=kwargs.pop("process_commands", commands),
                        session_statuses=kwargs.pop("session_statuses", statuses),
                        print_func=said.append,
                        **kwargs,
                    )
            return status, said

        # 0. The defect itself, asked of the tree that has it. Nothing here is
        #    new API: the approval record and both steps are module attributes
        #    that already existed, and the only claim is that a recovery does
        #    not ask a tenant to prove access nobody installed. On the tree this
        #    ticket fixes, the launch is reached and the install never is --
        #    which is journal 0014.
        recorded = {
            "gui_user": GUI_USER, "approved_by": GUI_USER,
            "approved_at": "2026-09-14T12:43:33Z", "reference": "recorded approval",
        }
        said: list[str] = []
        with owner_patches(home):
            with patch.object(launcher, "read_host_desktop_approval", lambda _path=None: recorded):
                with patch.object(launcher, "configure_project_desktop", desktop_installer):
                    with patch.object(launcher, "launch_project", fake_launch):
                        launcher.switchyard_resume_provision_command(
                            SLUG, source_repo=release, registry_dir=registry,
                            euid_getter=lambda: 0,
                            completion_reader=lambda _plan: launcher.PacketCompletion(()),
                            process_commands=commands, session_statuses=statuses,
                            print_func=said.append,
                        )
        assert "desktop" in order, (
            "the roles were launched without installing the desktop access they need: "
            + "; ".join(said)
        )
        assert order.index("desktop") < order.index("launch"), order
        order.clear()
        installs.clear()
        checks += 1

        # 1. The interrupted state, finished: the desktop install happens, and
        #    it happens before anything launches.
        status, said = resume()
        assert status == 0, said
        assert order == ["desktop", "launch"], order
        assert entry.exists(), said
        checks += 1

        # 2. What was installed is the approved policy, from the release being
        #    resumed from rather than from whatever code is running.
        assert installs[-1]["policy"] == wayland_policy(), installs[-1]
        assert installs[-1]["helper"] == release / "scripts" / "desktop_access.py", installs[-1]
        assert installs[-1]["config_path"] == config_path, installs[-1]
        assert any("scoped access to" in line and GUI_USER in line for line in said), said
        checks += 1

        # 3. Idempotent: again, and the same policy is installed again. The
        #    install itself is re-runnable, and so is everything around it.
        order.clear()
        status, said = resume()
        assert status == 0, said
        assert order == ["desktop", "launch"], order
        assert installs[-1]["policy"] == installs[0]["policy"]
        checks += 1

        # 4. Roles start only after desktop access is in place. An install that
        #    fails stops the recovery before the launch, keeps the registration
        #    it already made, and says how to retry.
        order.clear()
        status, said = resume(installer=failing_installer)
        assert status == 1, said
        assert order == ["desktop"], "the launch must not be reached"
        assert entry.exists(), "a registration already made is not undone"
        assert any("could not be installed" in line for line in said), said
        assert any("resume-provision" in line and "again" in line for line in said), said
        checks += 1

        # 5. A tenant that edits its own configuration to name another desktop
        #    is refused, and nothing is installed or launched for it.
        order.clear()
        payload["desktop_access"] = wayland_policy(gui_user="somebody-else")
        config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        status, said = resume()
        assert status == 1, said
        assert order == [], "neither the install nor the launch may be reached"
        assert any("not the tenant's to answer" in line for line in said), said
        checks += 1

        # 6. And a host that recorded no approval grants nothing, however the
        #    tenant's configuration is written.
        order.clear()
        payload["desktop_access"] = wayland_policy()
        config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        status, said = resume(approval_path=root / "no-approval-here.json")
        assert status == 1, said
        assert order == [], order
        assert any("no recorded desktop approval" in line for line in said), said
        checks += 1

        # 7. A headless tenant needs no desktop install and still launches.
        order.clear()
        payload["desktop_access"] = {"mode": "headless"}
        config_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        with owner_patches(home):
            headless_config = launcher.load_project_config(SLUG, config_path)
        headless_commands, headless_statuses = live_sessions(headless_config)
        status, said = resume(process_commands=headless_commands, session_statuses=headless_statuses)
        assert status == 0, said
        assert order == ["launch"], order
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
        print(f"resume_provision_desktop_test: privileged child ran {checks} checks")
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
        print("resume_provision_desktop_test: user namespaces unavailable; privileged half skipped")
    print(f"resume_provision_desktop_test: {checks} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
