#!/usr/bin/env python3
"""SYRD-155: finishing a recovery whose privileged packet already succeeded.

Testing journal 0011 ran the regenerated root-owned operator packet and it
worked. The board came up, the listener came up -- and
`/etc/switchyard/projects/testing.json` was still absent and uid 1013 had no
tmux server at all. The packet is the privileged half of `switchyard new`; the
registration and the role startup belonged to the `switchyard new` process that
had exited long before, and no supported command performed them. So the project
with a live board was one no ordinary command could name.

What that costs is not only convenience. `resume-provision` reported success
after regenerating artifacts, which made a recovery that had finished nothing
look finished, and the only way anybody found out was by looking for a tmux
server that was not there.

This suite is that shape, and what the recovery now does with it: the packet is
read back rather than assumed, the generated configuration is registered only
after root has checked it against its own record, the roles start through the
ordinary launcher path, and success is reported only when the project can be
named, its services answer and every role it declares has a live session
registered with the board. The privileged half runs inside a user namespace,
where this process is uid 0 and can own the files root would own.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from scripts import team_launcher as launcher  # noqa: E402
from resume_provision_test import (  # noqa: E402
    SEAM,
    SLUG,
    TENANT,
    fixture as provisioned_fixture,
    namespaces_available,
)

#: Inside the namespace there is one uid, and it is the one that owns every
#: file this fixture writes -- so it is the uid the project owner has here.
#: What the product checks is that the configuration belongs to the account
#: root is acting for; a different account is exercised by saying the owner is
#: somebody else while the file stays as it is.
OWNER_UID = 0


def quiet(*_args, **_kwargs) -> None:
    return None


# --------------------------------------------------------------------------
# what can be decided without owning anything
# --------------------------------------------------------------------------


def _plan(tmp: Path):
    from scripts.ticket_board.project_provision import build_plan

    return build_plan(
        project=SLUG, owner_user=TENANT, owner_home=tmp / "home" / TENANT,
        source_repo=ROOT, service_user="boardsvc",
    )


def generated_config(tmp: Path):
    """The configuration `switchyard new` writes, for a tenant living in tmp.

    Written by the product's own generator rather than by hand, so what root
    checks here is the document provisioning actually produces. The owner's
    home is answered from the fixture while it is generated and read, because
    the generator asks passwd where the account lives and this account's files
    are here.
    """
    plan = _plan(tmp)
    home = tmp / "home" / TENANT
    provision = home / "Projects" / SLUG / ".switchyard" / "provision"
    provision.mkdir(parents=True, exist_ok=True)
    with owner_patches(home):
        config_path = launcher.write_new_project_launcher_artifacts(
            plan, provision, repository=home / "Projects" / SLUG, print_func=quiet
        )
        config = launcher.load_project_config(SLUG, config_path)
    return plan, home, config_path, config


def test_an_unfinished_packet_is_named_step_by_step() -> None:
    """Completion is read from what the packet installs, not from a marker.

    A marker says a script reached its last line. What the rest of the recovery
    depends on is whether the units, the release and the board are actually
    there, so each missing one is named with the path that would hold it.
    """
    with tempfile.TemporaryDirectory(prefix="syrd155-completion.") as tmp:
        plan = _plan(Path(tmp))
        nothing = launcher.privileged_packet_completion(
            plan,
            exists=lambda _path: False,
            system_unit_active=lambda _unit: False,
            owner_unit_active=lambda _unit: False,
            board_answers=lambda: False,
        )
        assert not nothing.done
        joined = "\n".join(nothing.problems)
        for expected in (
            plan.board_unit, plan.tmpfiles_name, plan.polkit_name, plan.listener_unit,
            plan.board_current, f"port {plan.port}",
        ):
            assert expected in joined, (expected, joined)

        everything = launcher.privileged_packet_completion(
            plan,
            exists=lambda _path: True,
            system_unit_active=lambda _unit: True,
            owner_unit_active=lambda _unit: True,
            board_answers=lambda: True,
        )
        assert everything.done, everything.problems

        # One thing missing is still not finished, and says only that thing.
        listening_only = launcher.privileged_packet_completion(
            plan,
            exists=lambda _path: True,
            system_unit_active=lambda _unit: True,
            owner_unit_active=lambda _unit: True,
            board_answers=lambda: False,
        )
        assert listening_only.problems == (f"the board does not answer on port {plan.port}",)


def test_a_configuration_that_disagrees_with_root_is_not_registerable() -> None:
    """Every field the registry entry sends a later command to act on.

    The entry is a pointer. Following it decides which account runs the roles,
    which board they talk to and which tree they work in, so a configuration
    that answers those differently from root's own record is not a
    configuration root may point at -- whatever made it say so.
    """
    from dataclasses import replace

    with tempfile.TemporaryDirectory(prefix="syrd155-conflict.") as tmp:
        plan, _home, _config_path, config = generated_config(Path(tmp))
        assert launcher.tenant_config_conflicts(plan, config) == []

        for field, value, expected in (
            ("run_as_user", "somebody-else", "run_as_user"),
            ("ticket_prefix", "OTHER", "ticket_prefix"),
            ("board_socket", "/run/elsewhere.sock", "board_socket"),
            ("board_url", "http://127.0.0.1:1/", "board_url"),
            ("repository", Path("/tmp/not-in-the-home"), "repository"),
            ("session_dir", Path("/tmp/not-in-the-home/sessions"), "session_dir"),
        ):
            conflicts = launcher.tenant_config_conflicts(plan, replace(config, **{field: value}))
            assert any(expected in line for line in conflicts), (field, conflicts)

        # A role root never provisioned is a pane root never agreed to start.
        smuggled = replace(config, roles=[*config.roles, replace(config.roles[0], role="intruder")])
        assert any("intruder" in line for line in launcher.tenant_config_conflicts(plan, smuggled))


def test_roles_start_as_the_project_owner_when_somebody_else_drives_them() -> None:
    """The ordinary launcher path, which is the point of using it.

    The recovery runs as root, and root starting panes would be a project run
    by the wrong account. `pane_command_args` is what the launcher builds for
    every role, and it selects the owner when the caller is not the owner.
    """
    with tempfile.TemporaryDirectory(prefix="syrd155-owner.") as tmp:
        _plan_unused, _home, config_path, config = generated_config(Path(tmp))
        args = launcher.pane_command_args(
            SLUG, config.roles[0], config_path=config_path, mode="attach-or-start",
            script_path=ROOT / "scripts" / "team-launcher", run_as_user=TENANT,
        )
        assert args[:4] == ["sudo", "-u", TENANT, "-H"], args
        assert TENANT != launcher.current_user_name()


def test_the_command_offers_the_continuation_it_performs() -> None:
    parser = launcher._build_switchyard_resume_provision_parser()
    assert parser.parse_args([SLUG]).config_path is None
    assert parser.parse_args([SLUG, "--config", "/x/y.json"]).config_path == Path("/x/y.json")
    help_text = parser.format_help()
    assert "register" in help_text and "roles" in help_text, help_text


# --------------------------------------------------------------------------
# the privileged half, inside a user namespace
# --------------------------------------------------------------------------


def fixture(root: Path):
    """The live shape: a finished packet, no registry entry, no sessions."""
    release, home, installed, plan = provisioned_fixture(root)
    project_dir = home / "Projects" / SLUG
    provision = project_dir / ".switchyard" / "provision"
    provision.mkdir(parents=True, exist_ok=True)
    with owner_patches(home):
        config_path = launcher.write_new_project_launcher_artifacts(
            plan, provision, repository=project_dir, print_func=quiet
        )
    return release, home, installed, plan, config_path


def owner_patches(home: Path, *, uid: int = OWNER_UID):
    from contextlib import ExitStack
    from types import SimpleNamespace

    stack = ExitStack()
    stack.enter_context(patch.object(launcher, "uid_for_user", lambda name: uid if name == TENANT else None))
    stack.enter_context(patch.object(launcher, "home_dir_for_user", lambda name: home if name == TENANT else None))
    real = launcher.pwd.getpwuid
    stack.enter_context(
        patch.object(
            launcher.pwd, "getpwuid",
            side_effect=lambda value: SimpleNamespace(pw_gid=uid) if value == uid else real(value),
        )
    )
    return stack


def live_sessions(config) -> tuple[list[str], list]:
    """What a started project looks like to the two things readiness reads."""
    commands = [f"agy TICKET_BOARD_PANE_TARGET={role.target}" for role in config.roles]
    statuses = [
        launcher.LaunchSessionRecordStatus(role=role.role, target=role.target, session_id=f"sid-{role.role}")
        for role in config.roles
    ]
    return commands, statuses


def privileged_cases() -> int:
    checks = 0
    with tempfile.TemporaryDirectory(prefix="syrd155-privileged.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        os.environ[SEAM] = str(root)
        registry = root / "registry"
        registry.mkdir()
        release, home, installed, plan, config_path = fixture(root)
        with owner_patches(home):
            config = launcher.load_project_config(SLUG, config_path)
        commands, statuses = live_sessions(config)
        entry = registry / f"{SLUG}.json"

        # 0. The defect itself, stated so that it can be run against the tree
        #    that has it. Nothing here is new API: the command is called the way
        #    the live recovery called it, and the only claim is that reporting
        #    success and having finished are the same thing. On the tree this
        #    ticket fixes, this call returns 0 with nothing registered, which is
        #    exactly what testing journal 0011 produced.
        said: list[str] = []
        with owner_patches(home):
            with patch.object(launcher, "launch_project", lambda *a, **k: 0):
                status = launcher.switchyard_resume_provision_command(
                    SLUG, source_repo=release, registry_dir=registry,
                    euid_getter=lambda: 0, print_func=said.append,
                )
        assert not (status == 0 and not entry.exists()), (
            "resume-provision reported success while the project was still unregistered: "
            + "; ".join(said)
        )
        checks += 1

        finished = launcher.PacketCompletion(())
        unfinished = launcher.PacketCompletion(("the board service is not running",))
        launches: list[dict] = []

        def launcher_that(result: int):
            def fake_launch(cfg, *, config_path, mode, **kwargs):
                launches.append({"project": cfg.project, "config_path": config_path, "mode": mode})
                return result

            return fake_launch

        def resume(*, packet=finished, launch=0, **kwargs):
            said: list[str] = []
            with owner_patches(home, uid=kwargs.pop("owner_uid", OWNER_UID)):
                with patch.object(launcher, "launch_project", launcher_that(launch)):
                    status = launcher.switchyard_resume_provision_command(
                        SLUG,
                        source_repo=release,
                        registry_dir=registry,
                        euid_getter=lambda: 0,
                        completion_reader=lambda _plan: packet,
                        process_commands=kwargs.pop("process_commands", commands),
                        session_statuses=kwargs.pop("session_statuses", statuses),
                        print_func=said.append,
                        **kwargs,
                    )
            return status, said

        # 1. The live shape: a packet that has not run finishes nothing, and
        #    says so with a status that is not success. Reporting zero here is
        #    what made journal 0011 look like a completed recovery.
        status, said = resume(packet=unfinished)
        assert status == 1, said
        assert any("has not finished its privileged packet" in line for line in said), said
        assert any("operator-commands.sh" in line for line in said), said
        assert not entry.exists(), "nothing may be registered before the packet has run"
        assert not launches, launches
        checks += 1

        # 2. Packet succeeded, the `switchyard new` process long gone: this is
        #    the state journal 0011 left. Running the command finishes it.
        status, said = resume()
        assert status == 0, said
        assert entry.exists(), said
        registered = json.loads(entry.read_text(encoding="utf-8"))
        assert registered["slug"] == SLUG and registered["config_path"] == str(config_path), registered
        assert launches and launches[-1]["mode"] == "start", launches
        assert launches[-1]["config_path"] == config_path, launches
        assert any("all 1" in line or "role(s) have live sessions" in line for line in said), said
        checks += 1

        # 3. The verified configuration is remembered where only root can
        #    rewrite it, because where the checkout lives is the one thing root
        #    cannot regenerate from its own record.
        record = launcher.tenant_config_record_path(SLUG)
        assert record.is_file() and os.stat(record).st_uid == 0, record
        assert launcher.recorded_tenant_config_path(SLUG) == config_path
        checks += 1

        # 4. Run it again: registered once, started again, still finished. The
        #    registry entry is not rewritten, by inode.
        before = (entry.read_bytes(), entry.stat().st_ino)
        status, said = resume()
        assert status == 0, said
        assert any("already registered" in line for line in said), said
        assert (entry.read_bytes(), entry.stat().st_ino) == before
        checks += 1

        # 5. Readiness is proven, not assumed: a role with no pane and no
        #    session record is a recovery that is not finished, and it names
        #    the role rather than reporting success.
        status, said = resume(process_commands=[], session_statuses=[])
        assert status == 1, said
        assert any("has no running pane" in line for line in said), said
        assert any("has not registered a runtime session" in line for line in said), said
        assert any("resume-provision" in line and "again" in line for line in said), said
        assert entry.exists(), "a registration already made is not undone by an unready launch"
        checks += 1

        # 6. Role startup that fails keeps what was already done, says what to
        #    do, and does not report success.
        entry.unlink()
        status, said = resume(launch=1)
        assert status == 1, said
        assert entry.exists(), "the registration is a completed phase and stands"
        assert any("starting its roles did not succeed" in line for line in said), said
        assert any("phases already done are not repeated" in line for line in said), said
        # And the retry finishes it, which is what restartable means.
        status, said = resume()
        assert status == 0, said
        checks += 1

        # 7. A configuration that disagrees with root's record is refused, and
        #    nothing is registered from it.
        entry.unlink()
        original = config_path.read_bytes()

        def tamper(**changes) -> tuple[int, list[str]]:
            altered = {**json.loads(original.decode("utf-8")), **changes}
            config_path.write_text(json.dumps(altered, indent=2), encoding="utf-8")
            try:
                return resume()
            finally:
                config_path.write_bytes(original)

        # A field the loader accepts and only root's record contradicts: this
        # is the check being made here rather than a validation that happens to
        # fire first.
        status, said = tamper(ticket_prefix="OTHER")
        assert status == 1, said
        assert any("ticket_prefix" in line for line in said), said
        assert any("refusing to register" in line for line in said), said
        assert not entry.exists(), "a configuration root refused is not registered"

        # And the one that decides which account everything afterwards runs as.
        status, said = tamper(run_as_user="somebody-else")
        assert status == 1, said
        assert any("refusing to register" in line for line in said), said
        assert not entry.exists()
        checks += 1

        # 8. A configuration that belongs to neither the account root is acting
        #    for nor root is not this project's configuration, whoever wrote it.
        #    Asked of the check itself, because inside this namespace root is
        #    the only uid there is: the two accounts it will accept are named
        #    here instead, and the file belongs to neither.
        with owner_patches(home):
            with patch.object(launcher, "expected_privileged_uid", lambda: 4243):
                found, config_for, problems = launcher.verified_tenant_config(
                    plan, SLUG, owner_uid=4242
                )
        assert found is None and config_for is None, found
        assert any("rather than by uid 4242, 4243" in line for line in problems), problems
        assert not entry.exists()
        checks += 1

        # 9. A registry entry pointing somewhere else is a conflict root does
        #    not resolve on its own.
        entry.write_text(
            json.dumps({"schema": launcher.SWITCHYARD_REGISTRY_SCHEMA, "slug": SLUG,
                        "name": SLUG, "config_path": "/somewhere/else.json"}),
            encoding="utf-8",
        )
        status, said = resume()
        assert status == 1, said
        assert any("rather than the configuration root verified" in line for line in said), said
        assert json.loads(entry.read_text(encoding="utf-8"))["config_path"] == "/somewhere/else.json"
        checks += 1

        # 10. Nothing the tenant owns was rewritten by any of the above.
        assert config_path.read_bytes() == original
        assert (home / "Projects" / SLUG / "README.md").read_text(encoding="utf-8") == "# testing\n"
        assert (home / ".claude" / "credentials.json").read_text(encoding="utf-8") == '{"token": "seeded"}\n'
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
        print(f"resume_provision_continuation_test: privileged child ran {checks} checks")
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
        print("resume_provision_continuation_test: user namespaces unavailable; privileged half skipped")
    print(f"resume_provision_continuation_test: {checks} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
