#!/usr/bin/env python3
"""SYRD-175: repairing a running tenant's repository boundary, and only that.

SYRD-171 made the generated boundary correct. It did not give an already
registered tenant any way to receive it. `switchyard upgrade` regenerates
artifacts and runs unit and deploy steps without applying the packet;
`resume-provision` reads an active board, a live listener and an exported
release as a finished recovery and never looks at the boundary at all. The one
path that applies the repair is the whole first-provisioning packet -- which
deploys a release, replays the schema, seeds a workflow, applies RBAC, installs
and reloads units and starts sessions. For syrd, which is serving, that is not
a repair; it is everything the rollout was told not to replay.

So the phase is lifted out of root's own installed packet, between the markers
the packet writes around it, and every line is checked against the shapes a
boundary phase is made of before anything runs. Nothing is re-rendered beside
it and nothing is read from the tenant: the commands are the bytes root
installed, and the paths in them are root's.

The kernel decides what was actually granted: a user namespace with the
invoking account's subuid and subgid ranges mapped gives real uids and real
groups, `setfacl` names them and `setpriv` becomes them.
"""

from __future__ import annotations

import grp
import json
import os
import shutil
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
    render_operator_commands,
)
from resume_provision_test import SEAM, namespaces_available  # noqa: E402

PROJECT = "demo"
OWNER_UID = 1500
SERVICE_UID = 1600
SOCKET_GID = 1900
OPERATOR = SimpleNamespace(name="an-operator", uid=1000, source="pkexec", known=True)
BY_SUDO = SimpleNamespace(name="an-operator", uid=1000, source="sudo", known=True)
SECRET = "the tenant's source, across every role and every ticket\n"

#: Everything a boundary repair must never do to a tenant that is serving.
FORBIDDEN = (
    "ticket-board-service.sh",
    "psql",
    "systemctl",
    "ticket-board-migrate",
    "rbac.sql",
    "schema.sql",
    "-workflow.sql",
    "loginctl",
    "curl",
    "useradd",
    "ticket-board-register-runtime",
)


def provisioning():
    from scripts.ticket_board import project_provision

    return project_provision


def require_tools() -> None:
    for tool in ("setfacl", "getfacl", "unshare", "setpriv", "install", "chmod", "find"):
        assert shutil.which(tool), f"{tool} is required; this suite must not silently skip"


# --------------------------------------------------------------------------
# what the phase is, and what it is not
# --------------------------------------------------------------------------


def live_plan(**overrides):
    """A plan shaped like syrd's: shared account, commit store of its own."""
    arguments = {
        "project": "syrd",
        "owner_user": "switchyard-agent",
        "owner_home": Path("/home/switchyard-agent"),
        "commit_git_dir": "/home/switchyard-agent/syrd-source-cache.git",
    }
    arguments.update(overrides)
    return build_plan(**arguments)


def test_the_phase_is_fenced_and_carries_only_a_boundary() -> None:
    """Acceptance 4, read off the packet: the repair cannot be anything else."""
    packet = render_operator_commands(live_plan())
    phase, problem = provisioning().repository_boundary_phase(packet)

    assert not problem, problem
    assert phase, packet[:400]
    joined = "\n".join(phase)
    for forbidden in FORBIDDEN:
        assert forbidden not in joined, (forbidden, joined)
    # And it is the SYRD-171 repair, named surface by surface.
    assert "/home/switchyard-agent/syrd-worktrees" in joined
    assert "-x g:syrd-roles" in joined
    assert "control.git" in joined
    # The fence is in the packet, once, in order.
    begin = provisioning().REPOSITORY_BOUNDARY_BEGIN
    end = provisioning().REPOSITORY_BOUNDARY_END
    assert packet.count(begin) == 1 and packet.count(end) == 1
    assert packet.index(begin) < packet.index(end)


def test_a_packet_from_before_the_phase_existed_is_refused() -> None:
    """Acceptance 5: stale artifacts fail closed rather than repairing nothing."""
    phase, problem = provisioning().repository_boundary_phase("#!/usr/bin/env bash\nsudo true\n")
    assert phase == [] and "generated before that phase existed" in problem, problem


def test_a_phase_that_grew_something_else_is_refused() -> None:
    """The fence says where; the shapes say what. A deploy inside it is not run."""
    packet = render_operator_commands(live_plan())
    begin = provisioning().REPOSITORY_BOUNDARY_BEGIN
    smuggled = packet.replace(
        begin, begin + "\nsudo systemctl restart syrd-ticket-board.service", 1
    )
    phase, problem = provisioning().repository_boundary_phase(smuggled)
    assert phase == [], phase
    assert "not part of one" in problem and "systemctl" in problem, problem


def test_a_second_command_riding_on_a_boundary_line_is_refused() -> None:
    """The fence says where the phase is; the grammar says what a line may be.

    Each of these keeps a shape the checker allows at the front and hangs
    something else off the back, so a prefix test alone would run all of them.
    """
    packet = render_operator_commands(live_plan())
    begin = provisioning().REPOSITORY_BOUNDARY_BEGIN
    smuggled = (
        "sudo setfacl -m u:boardsvc:rx '/tmp/x'; sudo systemctl restart syrd-ticket-board",
        "sudo setfacl -m u:boardsvc:rx '/tmp/x' && psql -c 'drop schema public cascade'",
        "sudo install -d $(curl -s http://127.0.0.1/payload)",
        "sudo find /tmp -name x -exec sh -c 'loginctl terminate-user switchyard-agent' \\;",
        "fi; sudo systemctl daemon-reload",
        "if [ -d '/tmp' ]; then sudo systemctl restart syrd-ticket-board; fi",
        "sudo gpasswd -a boardsvc 'syrd-repo' >/etc/shadow",
        "sudo setfacl -m u:boardsvc:rx '/tmp/x",
    )
    for line in smuggled:
        phase, problem = provisioning().repository_boundary_phase(
            packet.replace(begin, f"{begin}\n{line}", 1)
        )
        assert phase == [], (line, phase)
        assert "not part of one" in problem, (line, problem)


def test_an_unprivileged_caller_is_told_how_this_is_run() -> None:
    said: list[str] = []
    status = launcher.switchyard_repair_boundary_command(
        PROJECT, euid_getter=lambda: 1000, print_func=said.append
    )
    assert status == 1
    assert any("pkexec switchyard repair-boundary" in line for line in said), said


def test_a_run_nobody_authorized_is_refused() -> None:
    for operator in (BY_SUDO, SimpleNamespace(name="", uid=None, source="", known=False)):
        said: list[str] = []
        status = launcher.switchyard_repair_boundary_command(
            PROJECT,
            euid_getter=lambda: 0,
            operator_resolver=lambda: operator,
            print_func=said.append,
        )
        assert status == 1, said
        assert any("authorized as one" in line for line in said), said


def test_a_plan_that_names_no_tenant_is_not_read_as_a_closed_boundary() -> None:
    """Readiness must not take silence for proof.

    `recovery_readiness_problems` is called with whatever plan its caller has,
    and one that carries only a slug names no worktree base and no home. The
    check cannot run on it -- and "it did not run" is not "it is closed", which
    is the whole failure this ticket exists to stop. It says so instead, and
    readiness carries that objection like any other.
    """
    objections = launcher.repository_boundary_problems(SimpleNamespace(project="syrd"))
    assert objections, "a plan that names nothing was read as a closed boundary"
    assert all("was not checked" in objection for objection in objections), objections
    assert all("owner_home" in objection for objection in objections), objections

    problems = readiness(SimpleNamespace(project="syrd"))
    carried = [line for line in problems if "was not checked" in line]
    assert carried, problems
    assert all("repair-boundary" in line for line in carried), carried


def test_the_command_is_discoverable_and_says_what_it_does_not_do() -> None:
    assert "repair-boundary" in launcher.SWITCHYARD_COMMANDS
    assert "repair-boundary" in launcher.switchyard_help_text()
    parser = launcher._build_switchyard_repair_boundary_parser()
    assert parser.parse_args([PROJECT]).apply is False
    assert parser.parse_args([PROJECT, "--apply"]).apply is True
    help_text = " ".join(parser.format_help().split())
    for promised in ("no deploy", "no schema", "no session startup", "rollout journal"):
        assert promised in help_text, (promised, help_text)


def per_role_plan(**overrides):
    """syrd's shape once it is on per-role accounts."""
    from dataclasses import replace

    from scripts.ticket_board.project_provision import roles_group_name

    plan = live_plan(**overrides)
    accounts = (("director", "syrd-director"), ("main", "syrd-main"))
    return replace(
        plan,
        role_accounts=accounts,
        roles_group=roles_group_name(plan.project),
        role_worktrees=tuple(
            (role, f"{plan.owner_home}/{plan.project}-worktrees/{role}") for role, _ in accounts
        ),
    )


def sudo_recorder(root: Path) -> tuple[dict[str, str], Path]:
    """A PATH where `sudo` records its argv instead of running anything.

    Nothing else is stubbed: `getent`, `sh` and the quoting are the host's, so
    what these cases read back is what the shell actually made of the packet's
    line.
    """
    stubs = root / "bin"
    stubs.mkdir(parents=True, exist_ok=True)
    log = root / "argv"
    (stubs / "sudo").write_text(
        "#!/bin/sh\nfor argument in \"$@\"; do printf '%s\\n' \"$argument\" "
        f">>{log}; done\nprintf '%s\\n' '--' >>{log}\n",
        encoding="utf-8",
    )
    (stubs / "sudo").chmod(0o755)
    environment = {**os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}"}
    return environment, log


def run_the_phase(plan, root: Path) -> list[str]:
    """Run the phase the way the repair does, with `sudo` only recording."""
    phase, problem = provisioning().repository_boundary_phase(render_operator_commands(plan))
    assert not problem, problem
    environment, log = sudo_recorder(root)
    for statement in provisioning().repository_boundary_statements(phase):
        done = subprocess.run(
            ["sh", "-c", "\n".join(statement)], env=environment, text=True, capture_output=True
        )
        assert done.returncode == 0, (statement, done.stderr)
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def test_a_per_role_tenant_is_granted_its_repository_group_before_the_retirement() -> None:
    """Acceptance 2 and 6: the roles keep their access across the change."""
    from scripts.ticket_board.project_provision import (
        repository_group_name,
        roles_group_name,
    )

    plan = per_role_plan()
    phase, problem = provisioning().repository_boundary_phase(render_operator_commands(plan))
    assert not problem, problem
    joined = "\n".join(phase)
    for forbidden in FORBIDDEN:
        assert forbidden not in joined, (forbidden, joined)

    repository = repository_group_name(plan.project)
    grants = [index for index, line in enumerate(phase) if f"g:{repository}" in line]
    retirements = [
        index for index, line in enumerate(phase) if f"-x g:{roles_group_name(plan.project)}" in line
    ]
    assert grants, joined
    assert retirements, joined
    # First grant, then retire: no window in which a role's worktree is reachable
    # by neither group.
    assert max(grants) < min(retirements), (grants, retirements, joined)


def test_a_host_without_the_legacy_group_retires_nothing() -> None:
    """Acceptance 6: a tenant that never had the group is not an error."""
    with tempfile.TemporaryDirectory(prefix="syrd175-nogroup.") as tmp:
        root = Path(tmp)
        plan = live_plan(project="nohostgroup", owner_home=root / "home")
        (root / "home" / f"{plan.project}-worktrees").mkdir(parents=True)
        recorded = run_the_phase(plan, root)

    from scripts.ticket_board.project_provision import roles_group_name

    assert subprocess.run(
        ["getent", "group", roles_group_name(plan.project)], capture_output=True
    ).returncode != 0, "this case needs a group the host does not have"
    # The install and the grants ran; not one removal did.
    assert any("install" in line for line in recorded), recorded
    assert not [line for line in recorded if line == "-x"], recorded


def test_paths_with_spaces_and_metacharacters_reach_the_tools_whole() -> None:
    """Acceptance 6: the shell must not get a vote on which path is repaired."""
    with tempfile.TemporaryDirectory(prefix="syrd175-odd.") as tmp:
        root = Path(tmp)
        home = root / "home" / "a tenant's home $(touch pwned) ; rm -rf ."
        plan = live_plan(owner_home=home, commit_git_dir=str(home / "syrd-source-cache.git"))
        base = f"{home}/{plan.project}-worktrees"
        Path(base).mkdir(parents=True)
        recorded = run_the_phase(plan, root)

        assert base in recorded, recorded
        assert not (Path.cwd() / "pwned").exists() and not (root / "pwned").exists()
        assert Path(base).is_dir(), "the base was renamed or removed by the shell"
        # Every path the tools were handed is inside this tenant's home.
        for argument in recorded:
            if argument.startswith("/"):
                assert str(root) in argument, (argument, recorded)


# --------------------------------------------------------------------------
# the repair, against a real tree
# --------------------------------------------------------------------------


def fixture(root: Path) -> tuple[Path, Path, Path, Any]:
    """syrd's legacy shape: a 0755 worktree base the socket group can enter,
    and a control repository it can write, under a home only traversal reaches.
    """
    home = root / "home" / "tenant"
    base = home / f"{PROJECT}-worktrees"
    control = home / ".local" / "state" / "switchyard" / "projects" / PROJECT / "control.git"
    for name in ("director", "main", "SYRD-42"):
        (base / name).mkdir(parents=True)
        (base / name / "source.py").write_text(SECRET, encoding="utf-8")
    (control / "objects").mkdir(parents=True)
    store = home / f"{PROJECT}-source-cache.git"
    (store / "objects").mkdir(parents=True)
    for path in (root, root / "home"):
        os.chmod(path, 0o711)
    for path in home.rglob("*"):
        os.chown(path, OWNER_UID, OWNER_UID)
    for path in (home, base, control, control.parent, store):
        os.chown(path, OWNER_UID, OWNER_UID)
    os.chmod(home, 0o710)
    os.chmod(base, 0o755)
    for child in base.iterdir():
        os.chmod(child, 0o755)

    plan = build_plan(
        project=PROJECT,
        owner_user=str(OWNER_UID),
        owner_home=home,
        service_user=str(SERVICE_UID),
        project_repository=home / "Projects" / PROJECT,
        commit_git_dir=str(store),
    )
    installed = launcher.install_privileged_artifacts(
        plan, launcher.render_privileged_artifacts(plan)
    )
    return home, base, control, plan


def grant_socket_group(home: Path, base: Path, control: Path) -> None:
    for path in (home, base):
        run(["setfacl", "-m", f"g:{SOCKET_GID}:--x", str(path)])
    run(["setfacl", "-R", "-m", f"g:{SOCKET_GID}:rwX", str(control)])
    run(["setfacl", "-R", "-m", f"d:g:{SOCKET_GID}:rwX", str(control)])


def run(args: list[str]) -> subprocess.CompletedProcess[str]:
    done = subprocess.run(args, capture_output=True, text=True)
    assert done.returncode == 0, (args, done.stdout, done.stderr)
    return done


def as_service(argv: list[str]) -> bool:
    return subprocess.run(
        ["setpriv", f"--reuid={SERVICE_UID}", f"--regid={SERVICE_UID}",
         f"--groups={SERVICE_UID},{SOCKET_GID}", *argv],
        capture_output=True,
    ).returncode == 0


def repair(slug: str, **kwargs) -> tuple[int, list[str]]:
    said: list[str] = []
    operator = kwargs.pop("operator", OPERATOR)
    status = launcher.switchyard_repair_boundary_command(
        slug,
        euid_getter=lambda: 0,
        operator_resolver=lambda: operator,
        print_func=said.append,
        **kwargs,
    )
    return status, said


def journal_entries(root: Path, slug: str) -> list[dict]:
    from scripts.ticket_board.rollout_journal import attempts

    records = []
    for entry in attempts(slug, root=root / "journal"):
        result = Path(entry["directory"]) / "result.json"
        records.append(
            {**entry, **(json.loads(result.read_text(encoding="utf-8")) if result.is_file() else {})}
        )
    return records


def name_the_namespaces_groups(root: Path) -> None:
    """Give this namespace the group name the packet's own lines use.

    The repair's retirement block is guarded by `getent group` and removes the
    entry by name; `getfacl -n` reports gids. Both are host facts, and a fresh
    user namespace has neither -- so the group database is the host's plus one
    line, bind-mounted over /etc/group, and the socket group is a real named
    group with a real gid from there on. Nothing in the repair is told about
    it: getent, setfacl and the launcher all resolve it the ordinary way.
    """
    database = root / "group"
    database.write_text(
        Path("/etc/group").read_text(encoding="utf-8")
        + f"{PROJECT}-roles:x:{SOCKET_GID}:\n",
        encoding="utf-8",
    )
    subprocess.run(["mount", "--make-rprivate", "/"], check=True)
    subprocess.run(["mount", "--bind", str(database), "/etc/group"], check=True)
    assert grp.getgrnam(f"{PROJECT}-roles").gr_gid == SOCKET_GID

    # `sudo` is how the packet spells "as root", and the repair runs the
    # packet's bytes. This namespace is already root and has no working sudo
    # configuration, so the word is given its meaning here and nothing else is
    # stubbed: every command it fronts is the real one.
    stubs = root / "bin"
    stubs.mkdir()
    (stubs / "sudo").write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
    (stubs / "sudo").chmod(0o755)
    os.environ["PATH"] = f"{stubs}{os.pathsep}{os.environ['PATH']}"


def readiness(plan) -> list[str]:
    """Readiness as `resume-provision` asks it, with everything else absent.

    The registry and configuration are deliberately not there: this is about
    which objections readiness raises, and an open boundary has to be one of
    them however the rest of the recovery looks.

    Pane liveness is supplied as empty rather than left to be discovered, and
    the runner refuses anything but `getfacl`. Discovering it would send
    readiness to the host's tmux through `sudo -u <another account>`, which on a
    clean machine is an interactive password prompt and not a test result. The
    refusal is the point as much as the quiet: asking whether a boundary is open
    must read ACLs and nothing else -- no tmux, no board socket, no sudo.
    """
    from runtime_registration_wait_test import config

    def only_reading_acls(args, **kwargs):
        program = Path(str(args[0])).name if isinstance(args, (list, tuple)) else str(args)
        assert program == "getfacl", f"readiness reached the host for {args!r}"
        return subprocess.run(args, **kwargs)

    return launcher.recovery_readiness_problems(
        plan,
        config(),
        Path("/nonexistent/config.json"),
        registry_path=Path("/nonexistent/registry.json"),
        runner=only_reading_acls,
        process_commands=[],
        pane_liveness_states=(),
        completion=launcher.PacketCompletion(),
        registration=launcher.RuntimeRegistrationWait(),
        print_func=lambda _line: None,
    )


def privileged_cases() -> int:
    checks = 0
    with tempfile.TemporaryDirectory(prefix="syrd175-privileged.") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        name_the_namespaces_groups(root)
        os.environ[SEAM] = str(root)
        os.environ["SWITCHYARD_ROLLOUT_JOURNAL_ROOT"] = str(root / "journal")
        home, base, control, plan = fixture(root)
        grant_socket_group(home, base, control)

        def identity(uid: int = OWNER_UID):
            from root_workflow_record_test import tenant_identity

            return tenant_identity(home, owner=str(uid))

        # 1. The boundary is open, and the detection says how.
        with identity():
            open_now = launcher.repository_boundary_problems(plan)
        assert any("anybody on this host can enter" in line for line in open_now), open_now
        assert any("world-readable" in line for line in open_now), open_now
        assert any(f"{PROJECT}-roles" in line for line in open_now), open_now
        assert as_service(["test", "-x", str(base)]), "the fixture must start open"
        assert as_service(["cat", str(base / "main" / "source.py")])
        checks += 1

        # 2. A dry run shows what is open and the exact lines, and changes nothing.
        before = (stat.S_IMODE(base.stat().st_mode), launcher._acl_entries(base, runner=subprocess.run))
        with identity():
            status, said = repair(PROJECT)
        assert status == 0, said
        joined = "\n".join(said)
        assert "is open" in joined and "the repair, taken from" in joined, joined
        assert "dry run; nothing was changed" in joined, joined
        assert (stat.S_IMODE(base.stat().st_mode), launcher._acl_entries(base, runner=subprocess.run)) == before
        assert journal_entries(root, PROJECT)[-1]["detail"] == "dry-run"
        assert journal_entries(root, PROJECT)[-1]["operator_source"] == "pkexec"
        checks += 1

        # 3. Applying it closes both halves, and the kernel agrees.
        with identity():
            status, said = repair(PROJECT, apply=True)
        assert status == 0, said
        with identity():
            assert launcher.repository_boundary_problems(plan) == []
        assert not as_service(["test", "-x", str(base)]), "the service still enters the base"
        assert not as_service(["cat", str(base / "main" / "source.py")])
        assert not as_service(["touch", str(control / "objects" / "planted")])
        assert stat.S_IMODE(base.stat().st_mode) == 0o750
        assert all(
            stat.S_IMODE(child.stat().st_mode) & 0o007 == 0 for child in base.iterdir()
        )
        assert journal_entries(root, PROJECT)[-1]["detail"] == "repaired"
        checks += 1

        # 4. What must keep working: the owner's own tree, and the service's
        #    named read of the commit store.
        store = home / f"{PROJECT}-source-cache.git"
        assert as_service(["ls", str(store / "objects")]), "the commit store read was lost"
        assert not as_service(["touch", str(store / "objects" / "planted")])
        assert subprocess.run(
            ["setpriv", f"--reuid={OWNER_UID}", f"--regid={OWNER_UID}", "--clear-groups",
             "cat", str(base / "main" / "source.py")],
            capture_output=True,
        ).returncode == 0
        checks += 1

        # 5. Idempotent: again, and it finds nothing to do and runs nothing.
        with identity():
            status, said = repair(PROJECT, apply=True)
        assert status == 0, said
        assert any("already closed" in line for line in said), said
        # Nothing to do means nothing recorded either: a repair that did not
        # happen is not an entry in the journal.
        assert journal_entries(root, PROJECT)[-1]["detail"] == "repaired"
        checks += 1

        # 6. A tenant that was already correct is not touched at all -- the
        #    testing half of this acceptance.
        correct = root / "home" / "correct"
        correct_base = correct / "other-worktrees"
        (correct_base / "main").mkdir(parents=True)
        for path in (correct, correct_base, correct_base / "main"):
            os.chown(path, OWNER_UID, OWNER_UID)
            os.chmod(path, 0o750)
        # Correct now includes what the base passes on: a base that closes its
        # own trees and lets the next one be created world-readable is not
        # closed, it is closed until somebody runs `git worktree add`
        # (SYRD-181).
        run(["setfacl", "-m", provisioning().INHERITED_WORKTREE_CLOSURE, str(correct_base)])
        other = build_plan(
            project="other",
            owner_user=str(OWNER_UID),
            owner_home=correct,
            service_user=str(SERVICE_UID),
        )
        with identity():
            assert launcher.repository_boundary_problems(other) == []
        checks += 1

        # 7. Partial prior repair: the mode closed, the group entry left. It is
        #    still open, and the repair finishes the job.
        run(["setfacl", "-m", f"g:{SOCKET_GID}:--x", str(base)])
        with identity():
            partial = launcher.repository_boundary_problems(plan)
        assert len(partial) == 1 and f"{PROJECT}-roles" in partial[0], partial
        with identity():
            status, said = repair(PROJECT, apply=True)
        assert status == 0, said
        with identity():
            assert launcher.repository_boundary_problems(plan) == []
        checks += 1

        # 8. Stale artifacts fail closed with what to run.
        packet = root / PROJECT / "operator-commands.sh"
        original = packet.read_bytes()
        packet.write_text("#!/usr/bin/env bash\nsudo true\n", encoding="utf-8")
        run(["setfacl", "-m", f"g:{SOCKET_GID}:--x", str(base)])
        with identity():
            status, said = repair(PROJECT, apply=True)
        assert status == 1, said
        assert any("switchyard upgrade" in line for line in said), said
        with identity():
            assert any(f"{PROJECT}-roles" in line for line in launcher.repository_boundary_problems(plan))
        packet.write_bytes(original)
        checks += 1

        # 9. A packet root does not control is not a repair root will run.
        packet.chmod(0o666)
        with identity():
            status, said = repair(PROJECT, apply=True)
        assert status == 1, said
        assert any("does not control" in line or "can write" in line for line in said), said
        packet.chmod(0o755)
        checks += 1

        # 10. And then the real one finishes what case 8 and 9 left open.
        with identity():
            status, said = repair(PROJECT, apply=True)
        assert status == 0, said
        with identity():
            assert launcher.repository_boundary_problems(plan) == []
        checks += 1

        # 11. Readiness cannot call a recovery finished while this is open,
        #     and it names the command that closes it.
        run(["setfacl", "-m", f"g:{SOCKET_GID}:--x", str(base)])
        with identity():
            problems = readiness(plan)
        boundary = [line for line in problems if f"{PROJECT}-roles" in line]
        assert boundary, problems
        assert all(
            f"pkexec switchyard repair-boundary {PROJECT} --apply" in line for line in boundary
        ), boundary
        with identity():
            repair(PROJECT, apply=True)
        with identity():
            assert not [line for line in readiness(plan) if f"{PROJECT}-roles" in line]
        checks += 1

        # 12. The board being unavailable is not an obstacle: this repair never
        #     asks it anything. (Acceptance 6, and the reason 4 holds.)
        run(["setfacl", "-m", f"g:{SOCKET_GID}:--x", str(base)])
        dead = {"TICKET_BOARD_URL": "http://127.0.0.1:1", "TICKET_BOARD_SOCKET": str(root / "gone.sock")}
        previous = {name: os.environ.get(name) for name in dead}
        os.environ.update(dead)
        try:
            with identity():
                status, said = repair(PROJECT, apply=True)
        finally:
            for name, value in previous.items():
                os.environ.pop(name, None) if value is None else os.environ.update({name: value})
        assert status == 0, said
        assert not (root / "gone.sock").exists()
        with identity():
            assert launcher.repository_boundary_problems(plan) == []
        checks += 1

        # 13. The tenant's own copies decide nothing. A packet and a plan in the
        #     tenant's home, naming another tree and another command, are not
        #     what root reads. (Acceptance 5.)
        planted = home / "operator-commands.sh"
        planted.write_text(
            "\n".join(
                [
                    "#!/usr/bin/env bash",
                    provisioning().REPOSITORY_BOUNDARY_BEGIN,
                    f"sudo install -d -m 0777 '{root}/pwned'",
                    provisioning().REPOSITORY_BOUNDARY_END,
                    "",
                ]
            ),
            encoding="utf-8",
        )
        os.chown(planted, OWNER_UID, OWNER_UID)
        (home / "provision.json").write_text(
            json.dumps({"owner_home": str(root / "pwned"), "project": PROJECT}), encoding="utf-8"
        )
        os.chown(home / "provision.json", OWNER_UID, OWNER_UID)
        run(["setfacl", "-m", f"g:{SOCKET_GID}:--x", str(base)])
        with identity():
            status, said = repair(PROJECT, apply=True)
        assert status == 0, said
        assert not (root / "pwned").exists(), "the tenant's packet chose what ran"
        assert any(str(root / PROJECT / "operator-commands.sh") in line for line in said), said
        with identity():
            assert launcher.repository_boundary_problems(plan) == []
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
        print(f"repository_boundary_repair_test: privileged child ran {checks} checks")
        return 0
    require_tools()
    if namespaces_available():
        done = subprocess.run(
            ["unshare", "--user", "--map-auto", "--map-root-user", "--mount", sys.executable,
             str(Path(__file__).resolve()), "--privileged-child"],
            text=True, capture_output=True,
        )
        if done.returncode != 0:
            print(done.stdout + done.stderr)
            return 1
        print(done.stdout.strip())
    else:
        print("repository_boundary_repair_test: user namespaces unavailable; privileged half skipped")
    print(f"repository_boundary_repair_test: {checks} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
