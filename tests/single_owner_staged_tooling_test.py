#!/usr/bin/env python3
"""A tenant with no per-role accounts still gets the bundle its panes run.

Fresh CachyOS/KDE provisioning of `test` on release 3620184 opened a six-pane
Konsole layout in which every tab exited 1 with

    sudo: /usr/local/lib/switchyard/test/switchyard-display-attach: command not found

while `/var/lib/switchyard/rollout/test/0001-.../result.json` recorded exit 0
and `/usr/local/lib/switchyard/test` held only control-grant.json and the board
unit. `role_runtime_command(plan)` returned "" whenever a plan declared no role
accounts -- and the call that stages the whole root-owned bundle sat inside that
test, so a modern single-owner tenant staged nothing at all: not the display
helper, not the tenant control client, not the privileged action boundary
(SYRD-249).

These cases drive the real renderers and the real verifier against a staging
root in a temporary directory. Nothing here touches /usr/local/lib/switchyard
or the tenant on this host.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "tests") not in sys.path:
    sys.path.insert(0, str(ROOT / "tests"))

from scripts import team_launcher as launcher  # noqa: E402
from scripts.ticket_board.project_provision import (  # noqa: E402
    ROLE_STAGED_EXECUTABLES,
    build_plan,
    render_operator_commands,
    role_runtime_command,
    role_tooling_staging_commands,
    role_tooling_staging_dir,
    staged_role_tooling_problems,
)
from standalone_test_runner import run_module_tests  # noqa: E402
from team_launcher_test_helpers import _staging_sudo_shim  # noqa: E402

#: The one a pane runs first, and the one the live report named.
DISPLAY_HELPER = "switchyard-display-attach"


def single_owner_plan(project: str = "probe"):
    """What `switchyard new` builds today: one owner, no per-role accounts."""
    return build_plan(
        project=project, project_name=project.title(), owner_user=f"{project}-agent",
        owner_home=Path(f"/home/{project}-agent"), source_repo=ROOT,
    )


def test_a_single_owner_plan_declares_no_role_accounts() -> None:
    """The precondition, stated: this is the shape the skip path was reached by."""
    plan = single_owner_plan()
    assert plan.role_accounts == (), plan.role_accounts
    assert plan.roles_group == "probe-agent", plan.roles_group


def test_the_generated_runtime_step_stages_the_whole_bundle() -> None:
    plan = single_owner_plan()
    rendered = role_runtime_command(plan)
    assert rendered.strip(), "a single-owner tenant rendered no runtime step at all"
    for name in ROLE_STAGED_EXECUTABLES:
        assert name in rendered, f"{name} is not staged: {rendered[:400]}"
    assert DISPLAY_HELPER in rendered, rendered[:400]
    # The bounded privileged boundary ships in the same pass, and a tenant that
    # skipped staging never got that either.
    assert "switchyard-privileged-helper" in rendered, rendered[:400]


def test_the_operator_script_itself_carries_it() -> None:
    """Through the generated artifact, which is what actually runs."""
    plan = single_owner_plan()
    operator = render_operator_commands(plan)
    assert DISPLAY_HELPER in operator, operator[:400]
    # The tenant's own staged directory, wherever this host puts it: importing
    # the shared test helpers redirects that root into a sandbox, which is what
    # keeps suites off /usr/local/lib.
    assert role_tooling_staging_dir(plan.project) in operator, operator[:400]
    assert "no per-role runtime preparation" not in operator, (
        "the script still claims there is nothing to prepare"
    )


def test_staging_never_reaches_into_the_owner_home() -> None:
    """SYRD-39's property, which this must not spend to fix SYRD-249."""
    plan = single_owner_plan()
    rendered = role_runtime_command(plan)
    assert plan.owner_home not in rendered, rendered
    assert f"sudo -u '{plan.owner_user}'" not in rendered, rendered


class Staged:
    """A staging root this account owns, filled by the real renderer."""

    def __init__(self, project: str = "probe") -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="syrd249."))
        self.project = project
        self.root = self.tmp / "staging"
        self.root.mkdir()
        self.config = types.SimpleNamespace(project=project)

    @property
    def directory(self) -> Path:
        return self.root / self.project

    def stage(self) -> subprocess.CompletedProcess[str]:
        """Run the rendered commands through this repository's own staging shim.

        The rendered lines install as root:root; a suite is not root, so the
        shim drops exactly what only root could do and nothing else -- the same
        fixture every other staging suite uses (SYRD-62).
        """
        script = "\n".join(
            ["set -euo pipefail"]
            + role_tooling_staging_commands(self.project, str(ROOT), staging_root=self.root)
        )
        environment = dict(os.environ)
        environment["PATH"] = f"{_staging_sudo_shim(self.tmp)}:{environment.get('PATH', '')}"
        return subprocess.run(
            ["bash", "-c", script], capture_output=True, text=True, timeout=300, env=environment
        )

    def problems(self) -> list[str]:
        return staged_role_tooling_problems(
            self.project, str(ROOT), staging_root=self.directory, expect_uid=os.getuid()
        )

    def close(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def test_the_rendered_commands_really_stage_the_tools_and_modes() -> None:
    staged = Staged()
    try:
        result = staged.stage()
        assert result.returncode == 0, (result.stdout[-800:], result.stderr[-800:])
        helper = staged.directory / DISPLAY_HELPER
        assert helper.is_file(), sorted(p.name for p in staged.directory.iterdir())
        assert helper.stat().st_mode & 0o111, oct(helper.stat().st_mode)
        assert not helper.stat().st_mode & 0o022, oct(helper.stat().st_mode)
        # And the package those clients import from their own directory.
        assert (staged.directory / "ticket_board").is_dir()
        assert staged.problems() == [], staged.problems()
    finally:
        staged.close()


def test_a_missing_display_helper_is_caught_before_launch() -> None:
    """What the six inert tabs would have been: one refusal instead."""
    staged = Staged()
    try:
        assert staged.stage().returncode == 0
        (staged.directory / DISPLAY_HELPER).unlink()
        problems = staged.problems()
        assert problems, "a bundle without the display helper passed verification"
        assert any(DISPLAY_HELPER in problem for problem in problems), problems
    finally:
        staged.close()


def test_staging_is_idempotent_and_repairs_in_place() -> None:
    staged = Staged()
    try:
        assert staged.stage().returncode == 0
        before = {entry.name for entry in staged.directory.iterdir()}
        # Something a tenant might have left beside the bundle, which a repair
        # must not take with it.
        keepsake = staged.directory / "control-grant.json"
        keepsake.write_text("{}", encoding="utf-8")
        (staged.directory / DISPLAY_HELPER).unlink()
        assert staged.stage().returncode == 0
        assert staged.problems() == [], staged.problems()
        assert keepsake.is_file(), "a re-stage removed a file that was not its own"
        after = {entry.name for entry in staged.directory.iterdir()}
        assert set(before) <= after, sorted(set(before) - after)
    finally:
        staged.close()


class _Config(types.SimpleNamespace):
    pass


def _ensure(staged: Staged, *, runner=None, said=None, euid: int = 0):
    """The gate, told this fixture's owner and who is asking.

    `expect_uid` is the sandbox override the shipped default exists to avoid:
    production bundles are root's, and these are staged by this suite's own
    account. `euid_getter` says which side of the boundary the call is on --
    root repairs, anybody else reports (SYRD-249 review).
    """
    config = _Config(project=staged.project)
    return launcher.ensure_staged_role_tooling(
        config,
        release_root=ROOT,
        staging_root=staged.root,
        expect_uid=os.getuid(),
        euid_getter=lambda: euid,
        runner=runner or (lambda args, **kw: subprocess.run(args, **kw)),
        print_func=(said.append if said is not None else (lambda _t: None)),
    )


def test_the_launch_gate_repairs_a_missing_bundle_rather_than_refusing() -> None:
    staged = Staged()
    said: list[str] = []
    try:
        assert staged.stage().returncode == 0
        (staged.directory / DISPLAY_HELPER).unlink()

        environment = dict(os.environ)
        environment["PATH"] = f"{_staging_sudo_shim(staged.tmp)}:{environment.get('PATH', '')}"

        def runner(args, **kwargs):
            # The gate shells out exactly as the upgrade does; the shim is on
            # PATH so the root-only flags are dropped and nothing else is.
            return subprocess.run(
                list(args), capture_output=True, text=True, timeout=300, env=environment
            )

        problems = _ensure(staged, runner=runner, said=said)
        assert problems == [], problems
        assert (staged.directory / DISPLAY_HELPER).is_file()
        assert any("restaging it" in line for line in said), said
    finally:
        staged.close()


def test_the_launch_gate_refuses_when_the_repair_cannot_work() -> None:
    """A partial stage stops with something to do, not with inert tabs."""
    staged = Staged()
    said: list[str] = []
    try:
        assert staged.stage().returncode == 0
        (staged.directory / DISPLAY_HELPER).unlink()
        problems = _ensure(
            staged,
            runner=lambda args, **kw: subprocess.CompletedProcess(args, 1, "", "sudo: not allowed"),
            said=said,
        )
        assert problems, "a bundle that could not be repaired was reported as fine"
        assert any("switchyard upgrade" in problem for problem in problems), problems
        assert any("panes would open on tooling that is not there" in p for p in problems), problems
    finally:
        staged.close()


def test_the_shipped_default_expects_root_not_the_reader() -> None:
    """The review finding: the verifier's default is the reader's own uid.

    Every production staged file is root's, and every account that reads one --
    the operator running `switchyard new`, the owner the bridge crosses to --
    is somebody else. A gate that asked for the reader's uid would call every
    healthy tenant broken (SYRD-249 review).
    """
    assert launcher.STAGED_TOOLING_OWNER_UID == 0
    import inspect

    signature = inspect.signature(launcher.ensure_staged_role_tooling)
    assert signature.parameters["expect_uid"].default == 0, signature
    # And the override is a parameter, not a guess about the caller.
    assert "expect_uid" in signature.parameters and "euid_getter" in signature.parameters


def test_a_non_root_caller_reports_and_stages_nothing() -> None:
    """A root-owned bundle, read by somebody who is not root."""
    staged = Staged()
    said: list[str] = []
    ran: list = []
    try:
        assert staged.stage().returncode == 0
        (staged.directory / DISPLAY_HELPER).unlink()
        problems = _ensure(
            staged, euid=1006, said=said,
            runner=lambda args, **kw: ran.append(args) or subprocess.CompletedProcess(args, 0),
        )
        assert problems, "a broken bundle passed for an unprivileged caller"
        assert ran == [], "an unprivileged caller tried to stage root's files"
        assert said == [], said
        assert any("repairing it is root's" in problem for problem in problems), problems
        assert any("switchyard upgrade" in problem for problem in problems), problems
    finally:
        staged.close()


def test_a_healthy_bundle_is_left_alone() -> None:
    staged = Staged()
    said: list[str] = []
    try:
        assert staged.stage().returncode == 0
        ran: list = []
        for euid in (0, 1006):
            ran.clear()
            said.clear()
            problems = _ensure(
                staged, euid=euid, said=said,
                runner=lambda args, **kw: ran.append(args) or subprocess.CompletedProcess(args, 0),
            )
            assert problems == [], (euid, problems)
            assert ran == [], f"a healthy bundle was restaged anyway (euid {euid})"
            assert said == [], (euid, said)
    finally:
        staged.close()


def test_a_missing_display_helper_is_repaired_through_the_authorized_boundary() -> None:
    """Not by the tenant account: through the recorded privileged command.

    `ensure_tenant_control_helper` is the mechanism this repository already
    uses before the control bridge is crossed, and the display helper is one of
    the two programs it covers. What is asserted here is WHO does the repair --
    one recorded privileged step, scoped to this tenant's own directory
    (SYRD-211, SYRD-249 review).
    """
    staged = Staged(project="test")
    commands: list = []
    said: list[str] = []
    try:
        assert staged.stage().returncode == 0
        helper = staged.directory / DISPLAY_HELPER
        source = (ROOT / "scripts" / DISPLAY_HELPER).read_bytes()
        helper.unlink()

        def runner(args, **kwargs):
            # Stage exactly what the recorded command would have, so the check
            # after the repair sees what a real one would leave.
            commands.append(list(args))
            helper.write_bytes(source)
            helper.chmod(0o755)
            return subprocess.CompletedProcess(args, 0, "", "")

        launcher.ensure_tenant_control_helper(
            "test",
            grant={"project": "test", "authorized_user": launcher.current_user_name()},
            release_root=str(ROOT),
            root=staged.root,
            owner_uid=os.getuid(),
            runner=runner,
            print_func=said.append,
        )
        assert len(commands) == 1, commands
        rendered = " ".join(commands[0])
        assert "sudo" in rendered, rendered
        # The recorded privileged path, not a bare shell as the tenant.
        assert "switchyard-record-rollout" in rendered or "record" in rendered, rendered
        assert str(staged.directory) in rendered, rendered
        assert helper.is_file() and helper.stat().st_mode & 0o111
        assert any(DISPLAY_HELPER in line for line in said), said
    finally:
        staged.close()


#: What the live host actually had: a staging directory with the grant and the
#: board unit in it, and none of the bundle (SYRD-249).
OBSERVED_LEFTOVERS = ("control-grant.json", "test-ticket-board.service")


def _observed_state(staged: Staged) -> None:
    """Reduce a staged tenant to exactly what provisioning left on that host."""
    for entry in staged.directory.iterdir():
        if entry.is_dir():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    for name in OBSERVED_LEFTOVERS:
        (staged.directory / name).write_text("{}", encoding="utf-8")


class _TenantControlRoot:
    """Point the staged-bundle path at a fixture, through its own env seam.

    `role_tooling_staging_dir` resolves the root the product uses, not a
    constant a test can reach past it -- which is what keeps suites off
    /usr/local/lib.
    """

    def __init__(self, root: Path, release: Path) -> None:
        self.root = root
        self.release = release

    def __enter__(self):
        self.previous = os.environ.get("SWITCHYARD_TENANT_CONTROL_ROOT")
        self.previous_launcher_root = launcher.TENANT_CONTROL_ROOT
        self.previous_release = launcher.switchyard_shared_install_root
        os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"] = str(self.root)
        launcher.TENANT_CONTROL_ROOT = self.root
        launcher.switchyard_shared_install_root = lambda: self.release
        return self

    def __exit__(self, *exc):
        if self.previous is None:
            os.environ.pop("SWITCHYARD_TENANT_CONTROL_ROOT", None)
        else:
            os.environ["SWITCHYARD_TENANT_CONTROL_ROOT"] = self.previous
        launcher.TENANT_CONTROL_ROOT = self.previous_launcher_root
        launcher.switchyard_shared_install_root = self.previous_release


class _Crossing:
    """The shipped operator start route, with root's half recorded."""

    def __init__(self, staged: Staged) -> None:
        self.staged = staged
        self.commands: list[list[str]] = []
        self.said: list[str] = []
        self.crossed: list[list[str]] = []
        self.repairs: list[str] = []

    def runner(self, args, **kwargs):
        argv = list(args)
        self.commands.append(argv)
        rendered = " ".join(argv)
        if "switchyard-tenant-control" in rendered and "record" not in rendered:
            # The crossing itself: everything before it has already happened.
            self.crossed.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "bash" in argv:
            # The recorded privileged repair. Its command line is asserted by
            # the case; what it WOULD have done is performed here the way the
            # host's privileged staging does, which is the same fixture shape
            # the SYRD-211 repair suite uses.
            self.repairs.append(rendered)
            staged = self.staged.stage()
            return subprocess.CompletedProcess(argv, staged.returncode, staged.stdout, staged.stderr)
        return subprocess.CompletedProcess(argv, 0, "", "")

    @property
    def privileged_repairs(self) -> list[list[str]]:
        return [argv for argv in self.commands if "bash" in argv]


def test_the_observed_state_recovers_through_the_ordinary_start() -> None:
    """The live shape, through `switchyard start`'s own crossing function.

    No manual upgrade: the operator command this ticket is about repairs the
    tenant on its way past, before anything runs as the owner (SYRD-249).
    """
    staged = Staged(project="test")
    try:
        assert staged.stage().returncode == 0
        _observed_state(staged)
        assert sorted(p.name for p in staged.directory.iterdir()) == sorted(OBSERVED_LEFTOVERS)

        crossing = _Crossing(staged)
        crossed_code = None
        with _TenantControlRoot(staged.root, ROOT):
            try:
                launcher._switchyard_exec_through_tenant_control(
                    "test", "start",
                    grant={"project": "test", "authorized_user": launcher.current_user_name(),
                           "owner": launcher.current_user_name()},
                    runner=crossing.runner,
                    exec_func=lambda *a, **k: None,
                    print_func=crossing.said.append,
                    ensure_helper=lambda *a, **k: None,
                )
            except SystemExit as exc:
                # The route ends by handing the bridge's own status back.
                crossed_code = exc.code
        assert crossed_code in (0, None), (crossed_code, crossing.said)

        # The bundle is complete and usable BEFORE the crossing happened.
        assert crossing.crossed, "the operator route never reached the bridge"
        assert crossing.privileged_repairs, "nothing was restaged for a tenant that had nothing"
        # One recorded privileged step, through the rollout recorder, scoped to
        # this tenant -- not a shell the tenant account could have run.
        assert len(crossing.repairs) == 1, crossing.repairs
        assert "sudo" in crossing.repairs[0], crossing.repairs[0]
        assert "record-rollout" in crossing.repairs[0] or "record" in crossing.repairs[0]
        assert str(staged.directory) in crossing.repairs[0], crossing.repairs[0]
        for name in ROLE_STAGED_EXECUTABLES:
            path = staged.directory / name
            assert path.is_file() and path.stat().st_mode & 0o111, name
        assert (staged.directory / "ticket_board").is_dir()
        # And what provisioning had left there is still there.
        for name in OBSERVED_LEFTOVERS:
            assert (staged.directory / name).is_file(), name
        assert any("restaging the bundle" in line for line in crossing.said), crossing.said
    finally:
        staged.close()


def test_a_healthy_tenant_crosses_with_no_privileged_restage() -> None:
    staged = Staged(project="test")
    try:
        assert staged.stage().returncode == 0
        crossing = _Crossing(staged)
        with _TenantControlRoot(staged.root, ROOT):
            problem = launcher.ensure_staged_role_bundle_before_crossing(
                "test", release_root=str(ROOT), root=staged.root,
                expect_uid=os.getuid(),
                runner=crossing.runner, print_func=crossing.said.append,
            )
        assert problem == "", problem
        assert crossing.commands == [], crossing.commands
        assert crossing.said == [], crossing.said
    finally:
        staged.close()


def _older_bundle(staged: Staged) -> None:
    """A complete bundle staged from an earlier release than the host's current."""
    marker = staged.directory / ".switchyard-release.json"
    marker.write_text(
        '{"commit": "0000000000000000000000000000000000000000"}', encoding="utf-8"
    )


def test_a_complete_older_bundle_is_neither_restaged_nor_refused() -> None:
    """The whole of a launch's contract about releases, on both sides of it.

    An older tenant is not a broken one. Its board is pinned to the build it
    was deployed with, and moving its role tooling because the HOST installed
    something newer is what `switchyard upgrade` is for. SYRD-211 said so for
    the two protocol programs; this says it once, for both halves of a launch
    (SYRD-249 review).
    """
    staged = Staged(project="test")
    try:
        assert staged.stage().returncode == 0
        _older_bundle(staged)
        crossing = _Crossing(staged)
        # The operator's half, before it crosses.
        with _TenantControlRoot(staged.root, ROOT):
            problem = launcher.ensure_staged_role_bundle_before_crossing(
                "test", root=staged.root, expect_uid=os.getuid(),
                runner=crossing.runner, print_func=crossing.said.append,
            )
        assert problem == "", problem
        assert crossing.commands == [], "an older bundle bought a privileged step"
        # And the owner's half, on the way back up, which is where the last
        # attempt contradicted itself.
        with _TenantControlRoot(staged.root, ROOT):
            problems = launcher.ensure_staged_role_tooling(
                _Config(project="test"),
                staging_root=staged.root,
                expect_uid=os.getuid(),
                euid_getter=lambda: os.getuid(),
                runner=lambda args, **kw: crossing.commands.append(list(args)),
                print_func=crossing.said.append,
            )
        assert problems == [], problems
        assert crossing.commands == [], crossing.commands
        assert crossing.said == [], crossing.said
    finally:
        staged.close()


class _Units:
    """The systemd halves of a resume, said out loud rather than run.

    This case is about what `resume_tenant` decides about the staged bundle;
    the units either side of that decision are somebody else's contract and
    are stubbed explicitly so the case cannot pass by not reaching them.
    """

    def __enter__(self):
        self.previous = {
            name: getattr(launcher, name)
            for name in ("board_system_unit_is_active", "capture_listener_state")
        }
        launcher.board_system_unit_is_active = lambda *a, **k: True
        launcher.capture_listener_state = lambda *a, **k: "active"
        return self

    def __exit__(self, *exc):
        for name, value in self.previous.items():
            setattr(launcher, name, value)


def test_the_operator_route_reaches_resume_with_an_older_bundle_untouched() -> None:
    """End to end: cross the bridge, resume as the owner, change nothing.

    The route the User actually types for an existing tenant. A complete
    bundle from an older release must neither buy a privileged restage nor be
    refused, on either side of the crossing (SYRD-249 review).
    """
    staged = Staged(project="test")
    try:
        assert staged.stage().returncode == 0
        _older_bundle(staged)
        crossing = _Crossing(staged)
        resumed: list[list[str]] = []
        config = _Config(project="test", run_as_user=launcher.current_user_name())

        def cross(args, **kwargs):
            """Root's half ran; now the owner's half, which is resume_tenant."""
            argv = list(args)
            crossing.commands.append(argv)
            crossing.crossed.append(argv)
            with _TenantControlRoot(staged.root, ROOT), _Units():
                problems = launcher.resume_tenant(
                    config,
                    config_path=staged.tmp / "test.json",
                    runner=lambda inner, **kw: resumed.append(list(inner))
                    or subprocess.CompletedProcess(inner, 0),
                    print_func=crossing.said.append,
                )
            assert problems == [], problems
            return subprocess.CompletedProcess(argv, 0, "", "")

        crossing_runner = crossing.runner

        def runner(args, **kwargs):
            rendered = " ".join(args)
            if "switchyard-tenant-control" in rendered and "record" not in rendered:
                return cross(args, **kwargs)
            return crossing_runner(args, **kwargs)

        crossed_code = None
        with _TenantControlRoot(staged.root, ROOT):
            try:
                launcher._switchyard_exec_through_tenant_control(
                    "test", "start",
                    grant={"project": "test", "authorized_user": launcher.current_user_name(),
                           "owner": launcher.current_user_name()},
                    runner=runner,
                    exec_func=lambda *a, **k: None,
                    print_func=crossing.said.append,
                    ensure_helper=lambda *a, **k: None,
                )
            except SystemExit as exc:
                crossed_code = exc.code
        assert crossed_code in (0, None), (crossed_code, crossing.said)
        assert crossing.crossed, "the route never reached the bridge"
        # Neither half restaged anything, and neither refused.
        assert crossing.privileged_repairs == [], crossing.privileged_repairs
        assert crossing.repairs == [], crossing.repairs
        assert not any("restaging" in line for line in crossing.said), crossing.said
        # And the bundle is exactly as it was: still the older release's.
        marker = json.loads((staged.directory / ".switchyard-release.json").read_text(encoding="utf-8"))
        assert marker["commit"] == "0" * 40, marker
    finally:
        staged.close()


def test_an_older_tenant_is_repaired_from_its_own_release_not_the_hosts() -> None:
    """A file genuinely missing from an older tenant comes back from ITS release.

    Restaging `current` at an ordinary launch would move that tenant's role
    tooling while its board stayed on the build it was deployed with.
    """
    staged = Staged(project="test")
    try:
        assert staged.stage().returncode == 0
        # A pinned release that exists, and is not the host's current.
        install_root = staged.tmp / "opt"
        pinned = install_root / "releases" / ("a" * 40)
        pinned.mkdir(parents=True)
        shutil.copytree(ROOT / "scripts", pinned / "scripts")
        (staged.directory / ".switchyard-release.json").write_text(
            '{"commit": "' + "a" * 40 + '"}', encoding="utf-8"
        )
        (staged.directory / DISPLAY_HELPER).unlink()

        absent, hostile, release = launcher.staged_bundle_launch_problems(
            "test", root=staged.root, install_root=install_root, expect_uid=os.getuid()
        )
        assert hostile == [], hostile
        assert any(DISPLAY_HELPER in problem for problem in absent), absent
        # The tenant's own release, resolved from its own staged marker.
        assert release == pinned, (release, pinned)
        assert launcher.tenant_pinned_release_root(
            "test", root=staged.root, install_root=install_root
        ) == pinned
    finally:
        staged.close()


def test_the_new_flow_checks_before_it_opens_any_window() -> None:
    """Placement, in the source: the gate precedes the launch that opens panes."""
    body = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")
    gate = body.index("staging_problems = ensure_staged_role_tooling(config, runner=runner")
    launch = body.index("launch_result = 0 if launch_deferred else launch_project(")
    assert gate < launch, (gate, launch)
    # And a resumed tenant repairs the same bundle on its way back up.
    resume = body.index("def resume_tenant(")
    resume_gate = body.index("ensure_staged_role_tooling(config, runner=runner", resume)
    board = body.index("if board_system_unit_is_active(config, runner=runner):", resume)
    assert resume_gate < board, (resume_gate, board)


#: Asks the SHIPPED gate about a genuinely root-owned bundle, as a genuinely
#: non-root account. No `expect_uid`, no `euid_getter`: the defaults are the
#: thing under test, and they are what the first attempt at this fix got wrong.
ROOT_OWNED_PROBE = """
import json, os, sys, types
sys.path.insert(0, os.environ["SYRD249_ROOT"])
from scripts import team_launcher as launcher

config = types.SimpleNamespace(project="probe")
ran = []
said = []
problems = launcher.ensure_staged_role_tooling(
    config,
    release_root=__import__("pathlib").Path(os.environ["SYRD249_ROOT"]),
    staging_root=__import__("pathlib").Path(os.environ["SYRD249_STAGING"]),
    runner=lambda args, **kw: ran.append(list(args)) or types.SimpleNamespace(returncode=0, stdout="", stderr=""),
    print_func=said.append,
)
print(json.dumps({"uid": os.getuid(), "problems": problems, "ran": ran, "said": said}))
"""


def _root_child(owner_uid: int) -> None:
    """As root: stage the bundle, then ask the gate as somebody who is not root."""
    assert os.geteuid() == 0
    tmp = Path(tempfile.mkdtemp(prefix="syrd249-root."))
    try:
        # A release this checkout's own account does not own the path to. The
        # non-root reader below cannot traverse a 0710 home, and asking it to
        # would be testing the home's mode rather than the gate.
        release = tmp / "release"
        release.mkdir()
        shutil.copytree(ROOT / "scripts", release / "scripts")
        if (ROOT / "skills").is_dir():
            shutil.copytree(ROOT / "skills", release / "skills")
        for path in (tmp, release):
            os.chmod(path, 0o755)
        subprocess.run(["chmod", "-R", "a+rX", str(release)], check=True)
        staging = tmp / "staging"
        staging.mkdir()
        script = "\n".join(
            ["set -euo pipefail", "sudo() { \"$@\"; }"]
            + role_tooling_staging_commands("probe", str(release), staging_root=staging)
        )
        staged = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=300)
        assert staged.returncode == 0, (staged.stdout[-600:], staged.stderr[-600:])
        directory = staging / "probe"
        helper = directory / DISPLAY_HELPER
        assert helper.stat().st_uid == 0, helper.stat().st_uid
        # Readable by everyone, writable by root alone: that is the shape.
        for path in (staging, directory):
            os.chmod(path, 0o755)

        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp),
            "SYRD249_ROOT": str(release),
            "SYRD249_STAGING": str(staging),
        }

        def ask() -> dict:
            result = subprocess.run(
                ["setpriv", f"--reuid={owner_uid}", f"--regid={owner_uid}", "--clear-groups",
                 sys.executable, "-c", ROOT_OWNED_PROBE],
                env=env, capture_output=True, text=True, timeout=300,
            )
            assert result.returncode == 0, (result.stdout, result.stderr)
            import json as _json

            return _json.loads(result.stdout.strip().splitlines()[-1])

        healthy = ask()
        assert healthy["uid"] == owner_uid, healthy
        assert healthy["problems"] == [], healthy
        assert healthy["ran"] == [], "a non-root reader tried to restage a healthy bundle"
        assert healthy["said"] == [], healthy
        print("  ok   a healthy root-owned bundle is accepted by a non-root caller")

        helper.unlink()
        broken = ask()
        assert broken["problems"], broken
        assert broken["ran"] == [], "a non-root reader tried to stage root's files"
        assert any(DISPLAY_HELPER in problem for problem in broken["problems"]), broken
        assert any("repairing it is root's" in p for p in broken["problems"]), broken
        print("  ok   a missing helper is reported, and nothing is run, as a non-root caller")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_the_shipped_gate_accepts_a_root_owned_bundle_as_a_non_root_caller() -> None:
    """The review's reproduction, as a case: root stages, somebody else reads."""
    if shutil.which("unshare") is None or shutil.which("setpriv") is None:
        return
    result = subprocess.run(
        ["unshare", "--user", "--map-auto", "--map-root-user",
         sys.executable, str(Path(__file__).resolve()), "--root-child"],
        capture_output=True, text=True, timeout=900,
    )
    assert result.returncode == 0, (result.stdout[-1500:], result.stderr[-1500:])
    assert "a healthy root-owned bundle is accepted" in result.stdout, result.stdout
    assert "nothing is run" in result.stdout, result.stdout


def main() -> int:
    run_module_tests(globals())
    count = sum(1 for name in globals() if name.startswith("test_"))
    print(f"single_owner_staged_tooling_test: {count} tests ok")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] == ["--root-child"]:
        # A uid that exists inside the namespace and is not root.
        _root_child(1000)
    else:
        raise SystemExit(main())
