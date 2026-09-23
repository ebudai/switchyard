#!/usr/bin/env python3
"""SYRD-112: what the front door decides before anything privileged happens.

The failure this comes from was three minutes of silence: no prompt, no child
process, nothing written down, and the operator learned only that it had not
worked. Every case here is about the opposite property -- that the answer to
"will this be allowed, what would it run, and how do I get back" is available
*before* privilege is requested, in bounded time, to the person running it.

So the recurring assertion in this file is not only "it refused" but "and it
refused without asking for privilege": a runner that must never be called, and
a `pkexec` that must never be reached.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import polkit_preflight as pp  # noqa: E402
from scripts.ticket_board import privileged_actions as pa  # noqa: E402
from scripts.ticket_board import privileged_front_door as fd  # noqa: E402
from scripts.ticket_board import privileged_helper as ph  # noqa: E402
from scripts.ticket_board import privileged_install as pi  # noqa: E402
from scripts.ticket_board import privileged_operations as po  # noqa: E402

CHECKS = 0
COMMIT = "0123456789abcdef0123456789abcdef01234567"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Recorder:
    """A runner that fails the test simply by being called."""

    def __init__(self, returncode: int = 0, raises: BaseException | None = None) -> None:
        self.calls: list[tuple] = []
        self.returncode = returncode
        self.raises = raises

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(list(argv), self.returncode)


def fake_identity(pid: int = 4242, start_time: int = 99, uid: int = 1006):
    return lambda: ph.CallerIdentity(pid, start_time, uid)


def authorized(action_id: str, **_kwargs) -> pp.PolkitDecision:
    return pp.PolkitDecision(pp.AUTHORIZED, action_id, "")


def refused(action_id: str, **_kwargs) -> pp.PolkitDecision:
    return pp.PolkitDecision(pp.REFUSED, action_id, "")


def not_registered(action_id: str, **_kwargs) -> pp.PolkitDecision:
    return pp.PolkitDecision(pp.NOT_REGISTERED, action_id, "not registered")


def clean_install(*_args, **_kwargs) -> list[str]:
    return []


def plan(action: str = "deploy-release", values: dict | None = None, **kwargs):
    kwargs.setdefault("decide", authorized)
    kwargs.setdefault("identity", fake_identity())
    kwargs.setdefault("verify", clean_install)
    kwargs.setdefault("build_command", lambda name, vals: ["/opt/switchyard/current/switchyard", name])
    return fd.plan_action("mefp", action, {"commit": COMMIT} if values is None else values, **kwargs)


# -- the dry run ------------------------------------------------------------


def test_a_dry_run_reports_the_whole_request_and_asks_for_nothing() -> None:
    printed: list[str] = []
    runner = Recorder()
    code = fd.privileged_action_command(
        "mefp",
        "deploy-release",
        {"commit": COMMIT},
        dry_run=True,
        print_func=printed.append,
        decide=authorized,
        identity=fake_identity(),
        verify=clean_install,
        rollback_commands=lambda project: [f"sudo ln -sfn /opt/switchyard/releases/old /opt/switchyard/current"],
        build_command=lambda name, vals: ["/opt/switchyard/current/switchyard", "upgrade", "mefp"],
    )
    report = "\n".join(printed)
    check(code == 0, f"a clean dry run succeeds: {code}")
    check(not runner.calls, "and ran nothing")
    # Every clause the acceptance names, each asserted by what it must contain
    # rather than by a substring that the fixture's own text could satisfy.
    check("org.switchyard.privileged.deploy-release" in report, f"the exact action: {report}")
    check(f"commit={COMMIT}" in report, f"the validated arguments: {report}")
    check(str(fd.DEFAULT_POLICY) in report, f"the installed policy: {report}")
    check(str(fd.DEFAULT_HELPER) in report, f"the helper it is bound to: {report}")
    check("/opt/switchyard/current" in report, f"the rollback path: {report}")
    check("would run:" in report, f"and what root would run: {report}")
    check("ready:     yes" in report, report)


def test_a_dry_run_that_cannot_proceed_says_so_and_still_runs_nothing() -> None:
    printed: list[str] = []
    code = fd.privileged_action_command(
        "mefp", "deploy-release", {"commit": COMMIT},
        dry_run=True, print_func=printed.append,
        decide=not_registered, identity=fake_identity(), verify=clean_install,
        build_command=lambda name, vals: ["x"],
    )
    report = "\n".join(printed)
    check(code == 1, f"a dry run that could not proceed is not a success: {code}")
    check("ready:     no" in report, report)
    check("nothing privileged was attempted" in report, report)
    check("switchyard upgrade" in report, f"and says what would install the policy: {report}")


# -- refusing before privilege ----------------------------------------------


def test_an_uncatalogued_action_never_reaches_pkexec() -> None:
    for name in ("run", "deploy-release; rm -rf /", "../deploy-release", ""):
        printed: list[str] = []
        code = fd.privileged_action_command(
            "mefp", name, {"commit": COMMIT}, print_func=printed.append,
            decide=authorized, identity=fake_identity(), verify=clean_install,
        )
        check(code == 1, f"{name!r} was refused: {code}")
        check("not a catalogued action" in "\n".join(printed), "\n".join(printed))


def test_an_injected_argument_never_reaches_pkexec() -> None:
    """The acceptance clause about command, path and environment injection."""
    runner = Recorder()
    for values in (
        {"commit": COMMIT, "env": "LD_PRELOAD=/tmp/x.so"},
        {"commit": COMMIT, "command": "/bin/sh"},
        {"commit": COMMIT, "path": "../../etc"},
        {"commit": "HEAD"},
        {"commit": "$(id)"},
    ):
        printed: list[str] = []
        code = fd.privileged_action_command(
            "mefp", "deploy-release", values, print_func=printed.append,
            decide=authorized, identity=fake_identity(), verify=clean_install,
        )
        check(code == 1, f"{values} was refused: {code}")
        check(not runner.calls, f"and nothing ran for {values}")


def test_a_caller_with_no_pane_is_refused_without_asking_polkit() -> None:
    asked: list[str] = []

    def decide(action_id, **kwargs):
        asked.append(action_id)
        return authorized(action_id)

    def no_pane():
        raise ph.Refused("cannot establish which pane this was run from")

    result = plan(identity=no_pane, decide=decide)
    check(not result.ready, "a caller with no pane cannot proceed")
    check(not asked, "and polkit was never asked about a caller that has no identity")
    check(result.decision.state == pp.UNAVAILABLE, result.decision.state)
    check(any("pane" in problem for problem in result.problems), str(result.problems))


# -- drift ------------------------------------------------------------------


def test_installed_drift_is_reported_before_privilege(tmp: Path | None = None) -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "switchyard"
        policy_dir = Path(raw) / "actions"
        root.mkdir()
        policy_dir.mkdir()
        (root / pi.PACKAGE_NAME).mkdir()
        helper = pi.helper_path(root)
        helper.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
        policy = pi.policy_path(policy_dir)
        policy.write_text("<policyconfig/>\n", encoding="utf-8")

        helper.chmod(0o755)
        policy.chmod(0o644)
        # This process is not root, so an owner check against uid 0 would fail
        # for a reason that has nothing to do with drift. The mode half is what
        # this case is about; ownership has its own case below, driven through
        # a stat result rather than a real file.
        problems = pi.verify_installation(root, policy_dir)
        check(
            all("owned by uid" in problem for problem in problems),
            f"a correct-mode tree reports only ownership here: {problems}",
        )

        helper.chmod(0o757)
        widened = pi.verify_installation(root, policy_dir)
        check(
            any("wider than" in problem and str(helper) in problem for problem in widened),
            f"a world-writable helper is reported: {widened}",
        )

        helper.chmod(0o755)
        policy.chmod(0o646)
        widened_policy = pi.verify_installation(root, policy_dir)
        check(
            any("wider than" in problem and str(policy) in problem for problem in widened_policy),
            f"a world-writable policy is reported: {widened_policy}",
        )


def test_a_symlinked_helper_is_refused_rather_than_followed() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "switchyard"
        policy_dir = Path(raw) / "actions"
        root.mkdir()
        policy_dir.mkdir()
        (root / pi.PACKAGE_NAME).mkdir()
        target = Path(raw) / "elsewhere"
        target.write_text("#!/bin/sh\n", encoding="utf-8")
        target.chmod(0o755)
        pi.helper_path(root).symlink_to(target)
        pi.policy_path(policy_dir).write_text("<policyconfig/>\n", encoding="utf-8")
        problems = pi.verify_installation(root, policy_dir)
        check(
            any("symbolic link" in problem for problem in problems),
            f"the link is refused rather than resolved: {problems}",
        )


def test_a_helper_owned_by_anyone_but_root_is_refused() -> None:
    """The one that matters: root runs it, so its owner chooses what root runs."""
    import os

    real = os.lstat

    def as_owned_by(uid: int, gid: int):
        def lstat(path: Path):
            info = real(ROOT / "scripts" / "ticket_board" / "privileged_helper.py")
            fields = list(info)
            fields[stat.ST_UID] = uid
            fields[stat.ST_GID] = gid
            return os.stat_result(fields)

        return lstat

    problems = pi.verify_installation(lstat=as_owned_by(1006, 1006))
    check(
        any("rather than root:root" in problem for problem in problems),
        f"a tenant-owned helper is refused: {problems}",
    )
    check(
        any("decides what root runs" in problem for problem in problems),
        f"and says why it matters: {problems}",
    )


def test_drift_stops_the_front_door_and_names_the_repair() -> None:
    printed: list[str] = []
    runner = Recorder()
    code = fd.privileged_action_command(
        "mefp", "deploy-release", {"commit": COMMIT}, print_func=printed.append,
        decide=authorized, identity=fake_identity(),
        verify=lambda *_a, **_k: ["/usr/local/lib/switchyard/switchyard-privileged-helper is mode 0777"],
    )
    report = "\n".join(printed)
    check(code == 1, f"drift stops the request: {code}")
    check(not runner.calls, "and nothing ran")
    check("mode 0777" in report, report)
    check("switchyard upgrade mefp" in report, f"and names the repair: {report}")


# -- the shared release, and its recorded gap -------------------------------


def _release_tree(raw: str, commit: str, *, marker_commit: str | None = None) -> str:
    releases = Path(raw) / "releases"
    (releases / commit).mkdir(parents=True)
    marker = releases / commit / po.RELEASE_MARKER_NAME
    marker.write_text(
        '{"commit": "%s"}\n' % (commit if marker_commit is None else marker_commit),
        encoding="utf-8",
    )
    return str(releases)


class release_cache:
    """Point the operations module at a release tree this process can build.

    The ownership rule is real and is asserted on its own below; here it would
    only mean "this suite is not root", which is not what these cases are
    about.
    """

    def __init__(self, releases: str) -> None:
        self.releases = releases

    def __enter__(self):
        self.previous = (po.SHARED_RELEASES, po.TRUSTED_OWNER_UID)
        po.SHARED_RELEASES = self.releases
        po.TRUSTED_OWNER_UID = os.getuid()
        return self

    def __exit__(self, *_exc):
        po.SHARED_RELEASES, po.TRUSTED_OWNER_UID = self.previous
        return False


def test_a_commit_no_trusted_source_holds_is_refused_with_the_packet_to_run() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        releases = _release_tree(raw, COMMIT)
        missing = "f" * 40
        with release_cache(releases):
          try:
            po.trusted_release_root(missing)
            check(False, "an absent release was accepted")
          except po.NoTrustedSource as exc:
            check("no trusted source" in str(exc), str(exc))
            check(missing in str(exc), f"and names the commit: {exc}")
            check("packet" in str(exc), f"and what would produce it: {exc}")


def test_a_release_directory_is_not_its_own_evidence() -> None:
    """A directory named after a commit is a claim; the marker is the evidence."""
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        releases = _release_tree(raw, COMMIT, marker_commit="b" * 40)
        with release_cache(releases):
            try:
                po.trusted_release_root(COMMIT)
                check(False, "a mismatched marker was accepted")
            except po.NoTrustedSource as exc:
                check("records commit" in str(exc), str(exc))
                check("b" * 40 in str(exc), f"and says what it actually records: {exc}")

    with tempfile.TemporaryDirectory() as raw:
        releases = _release_tree(raw, COMMIT)
        (Path(releases) / COMMIT / po.RELEASE_MARKER_NAME).unlink()
        with release_cache(releases):
            try:
                po.trusted_release_root(COMMIT)
                check(False, "an unmarked directory was accepted")
            except po.NoTrustedSource as exc:
                check("records commit nothing" in str(exc), str(exc))


def test_a_symlinked_release_is_refused() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        real = Path(raw) / "real"
        real.mkdir()
        (real / po.RELEASE_MARKER_NAME).write_text('{"commit": "%s"}\n' % COMMIT, encoding="utf-8")
        releases = Path(raw) / "releases"
        releases.mkdir()
        (releases / COMMIT).symlink_to(real)
        with release_cache(str(releases)):
            try:
                po.trusted_release_root(COMMIT)
                check(False, "a symlinked release was accepted")
            except po.NoTrustedSource as exc:
                check("symbolic link" in str(exc), str(exc))


def test_a_trusted_release_resolves_to_roots_own_directory() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        releases = _release_tree(raw, COMMIT)
        with release_cache(releases):
            resolved = po.trusted_release_root(COMMIT)
        check(resolved == f"{releases}/{COMMIT}", resolved)
        check(raw in resolved, "and it is under root's own cache, not the caller's")


def test_the_shared_release_install_runs_a_bounded_activation() -> None:
    """The DAT correction: catalogued, commit-only, and it actually activates.

    An earlier candidate stopped at a durable refusal here. That was correct
    fail-closed behaviour and it was not the ticket: the acceptance is that a
    Director can start the exact-release transaction with one command. The
    activation itself, and its rollback, live in
    `shared_release_activation_test`; what this asserts is the boundary around
    it -- a commit and nothing else, resolved against root's own cache, with
    no path reaching the argv.
    """
    action = pa.action_for("install-shared-release")
    check(
        [name for name, _ in action.arguments] == ["commit"],
        f"a commit and nothing else: {action.arguments}",
    )
    check(action.authentication == pa.AUTH_ADMIN, "and a human authenticates for it")

    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        releases = _release_tree(raw, COMMIT)
        with release_cache(releases):
            command = po.command_for("install-shared-release", {"commit": COMMIT})
        check(command[0] == po.LAUNCHER, f"the pinned launcher: {command}")
        check(
            command[1:] == ["install-shared-release", "--commit", COMMIT],
            f"a real subcommand and a commit: {command}",
        )
        check(
            not any(raw in element for element in command),
            f"and the resolved path never reaches the argv: {command}",
        )

        # A commit no trusted source holds is still refused before privilege.
        with release_cache(releases):
            try:
                po.command_for("install-shared-release", {"commit": "f" * 40})
                check(False, "an unheld commit was accepted")
            except po.NoTrustedSource as exc:
                check("no trusted source" in str(exc), str(exc))


def test_the_shipped_default_demands_root() -> None:
    """The branch that actually runs, exercised without any override.

    Every other trusted-source case points the module at a tree this process
    can write, which means every other case runs with `TRUSTED_OWNER_UID` set
    to something that is not root. That would leave the shipped value -- the
    one the helper uses -- asserted nowhere, so it is asserted here, and the
    rule behind it is driven through a stat result rather than a real file.
    """
    check(po.TRUSTED_OWNER_UID == 0, f"what ships demands root: {po.TRUSTED_OWNER_UID}")
    check(po.SHARED_RELEASES == "/opt/switchyard/releases", po.SHARED_RELEASES)

    real = os.lstat(ROOT / "scripts" / "ticket_board")

    def owned_by(uid: int):
        def lstat(_path):
            fields = list(real)
            fields[stat.ST_UID] = uid
            return os.stat_result(fields)

        return lstat

    try:
        po.trusted_release_root(COMMIT, lstat=owned_by(1006), read_marker=lambda _m: COMMIT)
        check(False, "a release owned by the tenant account was accepted by default")
    except po.NoTrustedSource as exc:
        check("rather than uid 0" in str(exc), str(exc))
    resolved = po.trusted_release_root(COMMIT, lstat=owned_by(0), read_marker=lambda _m: COMMIT)
    check(resolved == f"/opt/switchyard/releases/{COMMIT}", resolved)


def test_the_marker_name_matches_what_the_build_writes() -> None:
    """A marker name that drifted would make every release look untrusted."""
    source = (ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8")
    declared = f'SWITCHYARD_RELEASE_MARKER_NAME = "{po.RELEASE_MARKER_NAME}"'
    check(declared in source, f"the build writes {po.RELEASE_MARKER_NAME}")


def test_the_uncatalogued_half_is_recorded_with_its_reason() -> None:
    names = {name for name, _ in pa.UNCATALOGUED_BY_DESIGN}
    check("build-shared-release" in names, f"the build half is named: {names}")
    for _name, reason in pa.UNCATALOGUED_BY_DESIGN:
        check("checkout" in reason, f"and says why: {reason}")


# -- the operation table ----------------------------------------------------


def test_every_catalogued_action_has_exactly_one_operation() -> None:
    catalogued = {action.name for action in pa.CATALOGUE}
    wired = set(po.OPERATIONS)
    check(
        catalogued == wired,
        f"catalogue and operations agree; only in catalogue: {catalogued - wired}, "
        f"only wired: {wired - catalogued}",
    )


def test_every_operation_runs_the_pinned_launcher_and_nothing_else() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as raw:
        releases = _release_tree(raw, COMMIT)
        with release_cache(releases):
            for action in pa.CATALOGUE:
                values = {}
                for name, _ in action.arguments:
                    values[name] = "mefp" if name == "project" else COMMIT
                try:
                    command = po.command_for(action.name, values)
                except po.NotExecutableYet:
                    continue
                check(command[0] == po.LAUNCHER, f"{action.name} runs {command[0]}")
                check(
                    command[0].startswith("/opt/switchyard/"),
                    f"{action.name} runs from root's own tree: {command[0]}",
                )
                for element in command[1:]:
                    check(
                        element in values.values()
                        or element.startswith("--")
                        or element in {"upgrade", "repair-boundary", "--apply",
                                       "install-shared-release"}
                        or element.startswith(releases),
                        f"{action.name} passes {element!r}, which is neither a literal "
                        "from the operations table nor a validated value",
                    )


def test_an_action_with_no_operation_is_a_refusal_not_a_no_op() -> None:
    try:
        po.command_for("not-wired", {})
        check(False, "an unwired action silently did nothing")
    except KeyError as exc:
        check("no privileged operation" in str(exc), str(exc))


def test_a_projectless_action_must_ask_a_human() -> None:
    """The pairing the helper relies on, asserted over the whole table.

    An action with no project cannot be proved against a tenant's board, so the
    helper does not try. That is only safe while every such action requires a
    human to authenticate -- so a future projectless action added as
    pre-authorized fails here rather than quietly skipping the identity check.
    """
    for action in pa.CATALOGUE:
        names = {name for name, _ in action.arguments}
        if "project" not in names:
            check(
                action.authentication == pa.AUTH_ADMIN,
                f"{action.name} names no project and so must ask a human, but is "
                f"{action.authentication}",
            )
    check(True, "every projectless action requires administrator authentication")


def test_no_project_can_shadow_the_helpers_own_package() -> None:
    """The helper's package sits beside the per-tenant staging directories."""
    for reserved in (pi.PACKAGE_NAME, ph.HOST_JOURNAL_PROJECT):
        try:
            pa.action_for("upgrade-tenant").validate({"project": reserved})
            check(False, f"{reserved!r} was accepted as a project slug")
        except pa.ArgumentError as exc:
            check("slug" in str(exc), str(exc))


# -- asking, and being told nothing came back -------------------------------


def test_the_privileged_call_is_a_fixed_program_and_a_catalogued_verb() -> None:
    runner = Recorder()
    result = plan(build_command=lambda name, vals: ["/opt/switchyard/current/switchyard"])
    code = fd.run_action(result, runner=runner, which=lambda _name: "/usr/bin/pkexec",
                         print_func=lambda _line: None)
    check(code == 0, f"an authorized action runs: {code}")
    argv, kwargs = runner.calls[0]
    check(argv[0] == "pkexec", f"through pkexec: {argv}")
    check(argv[1] == str(fd.DEFAULT_HELPER), f"one fixed helper: {argv}")
    check(argv[2] == "deploy-release", f"a catalogued verb: {argv}")
    check(argv[3] == f"commit={COMMIT}", f"and typed values: {argv}")
    check("shell" not in kwargs, f"never a shell: {kwargs}")
    check(kwargs.get("timeout") == fd.DEFAULT_ACTION_TIMEOUT_SECONDS, f"bounded: {kwargs}")


def test_run_action_refuses_a_plan_that_is_not_ready_on_its_own() -> None:
    """The guard has to be in `run_action`, not only in its caller.

    A mutation that removed this check survived the whole suite, because every
    other case reaches `run_action` through `privileged_action_command`, which
    guards first. `run_action` is a public entry point: anything that builds a
    plan and runs it must not be able to skip the readiness it computed.
    """
    runner = Recorder()
    printed: list[str] = []
    unready = plan(decide=not_registered)
    check(not unready.ready, "the fixture really is not ready")
    code = fd.run_action(
        unready, runner=runner, which=lambda _name: "/usr/bin/pkexec",
        print_func=printed.append,
    )
    check(code == 1, f"{code}")
    check(not runner.calls, "and pkexec was never reached")
    check("ready:     no" in "\n".join(printed), "\n".join(printed))

    problem_plan = plan(verify=lambda *_a, **_k: ["the helper is mode 0777"])
    check(not problem_plan.ready, "a drifted plan is not ready either")
    code = fd.run_action(
        problem_plan, runner=runner, which=lambda _name: "/usr/bin/pkexec",
        print_func=printed.append,
    )
    check(code == 1, f"{code}")
    check(not runner.calls, "and still nothing ran")


def test_a_call_that_does_not_come_back_is_a_failure_with_a_duration() -> None:
    """The SYRD-102 shape, turned into an answer."""
    printed: list[str] = []
    runner = Recorder(raises=subprocess.TimeoutExpired(cmd="pkexec", timeout=1.0))
    code = fd.run_action(
        plan(), runner=runner, which=lambda _name: "/usr/bin/pkexec",
        timeout=1.0, print_func=printed.append,
    )
    report = "\n".join(printed)
    check(code == 124, f"a timeout is its own exit status: {code}")
    check("did not complete within 1s" in report, report)
    check("abandoned" in report, report)
    check("rollout-log mefp" in report, f"and says where the record is: {report}")


def test_an_absent_authentication_agent_is_reported_rather_than_waited_on() -> None:
    """`auth_admin` with nobody to answer must not become a silent wait."""
    result = plan(
        action="install-shared-release",
        values={"commit": COMMIT},
        decide=lambda action_id, **kwargs: pp.PolkitDecision(
            pp.NEEDS_AUTHENTICATION, action_id, "polkit.result=auth_admin_keep"
        ),
        build_command=lambda name, vals: ["/opt/switchyard/current/switchyard"],
    )
    check(not result.decision.may_proceed, "it does not proceed into a prompt nobody will see")
    report = "\n".join(result.report())
    check("ready:     no" in report, report)
    check(pp.NEEDS_AUTHENTICATION in report.lower() or "authenticat" in report.lower(), report)


def test_a_missing_pkexec_is_said_out_loud() -> None:
    printed: list[str] = []
    runner = Recorder()
    code = fd.run_action(plan(), runner=runner, which=lambda _name: None,
                         print_func=printed.append)
    check(code == 1, f"{code}")
    check(not runner.calls, "and nothing was run")
    check("not installed" in "\n".join(printed), "\n".join(printed))


def test_a_failed_action_points_at_the_durable_record() -> None:
    printed: list[str] = []
    code = fd.run_action(
        plan(), runner=Recorder(returncode=3), which=lambda _name: "/usr/bin/pkexec",
        print_func=printed.append,
    )
    check(code == 3, f"the exit status is passed through: {code}")
    check("rollout-log mefp" in "\n".join(printed), "\n".join(printed))


# -- installing the boundary ------------------------------------------------


def test_the_install_puts_every_piece_under_root_with_an_explicit_owner() -> None:
    commands = pi.install_commands("/opt/switchyard/releases/" + COMMIT)
    body = "\n".join(commands)
    check("install -d -m 0755 -o root -g root" in body, body)
    check(f"-o root -g root" in body, "every install line names the owner explicitly")
    for line in commands:
        if line.startswith("#"):
            continue
        if " install " in line:
            check("-o root -g root" in line, f"ownership is explicit: {line}")
    check(str(pi.helper_path()) in body, f"the helper: {body}")
    check(str(pi.policy_path()) in body, f"the policy: {body}")
    check(f"chown -R root:root" in body, f"and the package beside it: {body}")
    check("go-w" in body, f"and nothing under it stays group-writable: {body}")


def test_the_rendered_policy_names_the_installed_helper() -> None:
    policy = pi.rendered_policy()
    check(str(pi.helper_path()) in policy, "the policy binds to where the helper is installed")
    for action in pa.CATALOGUE:
        check(action.action_id in policy, f"{action.name} is in the installed catalogue")


def test_the_generated_install_really_produces_the_policy() -> None:
    """Run the rendered commands, as root, and read back what landed.

    Rendering a shell script and asserting on its *text* proves nothing about
    what it does: the policy is piped through `printf` inside single quotes and
    carries apostrophes of its own, so the POSIX `'"'"'` escape is doing real
    work in every line of it. The only way to know that survived is to run it
    and compare the installed bytes with what the renderer said it would
    install.

    `unshare --user --map-root-user` makes this process root inside its own
    namespace, so `install -o root -g root` is exercised exactly as written
    rather than stubbed away. A host without user namespaces skips, loudly.
    """
    import shutil
    import tempfile

    if shutil.which("unshare") is None:
        print("privileged_front_door_test: no unshare; the install case did not run")
        return
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "id", "-u"],
        capture_output=True, text=True,
    )
    if probe.returncode != 0 or probe.stdout.strip() != "0":
        print("privileged_front_door_test: user namespaces unavailable; install case skipped")
        return

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "lib"
        policy_dir = Path(raw) / "actions"
        release = Path(raw) / "release"
        (release / "scripts" / pi.PACKAGE_NAME).mkdir(parents=True)
        (release / "scripts" / pi.PACKAGE_NAME / "privileged_helper.py").write_text(
            "#!/usr/bin/env python3\n", encoding="utf-8"
        )
        script = "set -eu\n" + "\n".join(
            pi.install_commands(str(release), root=root, policy_dir=policy_dir, sudo="")
        )
        result = subprocess.run(
            ["unshare", "--user", "--map-root-user", "bash", "-c", script],
            capture_output=True, text=True,
        )
        check(result.returncode == 0, f"the rendered install ran: {result.stderr[:600]}")

        installed = pi.policy_path(policy_dir).read_text(encoding="utf-8")
        check(
            installed == pi.rendered_policy(root),
            "the installed policy is byte-identical to what the renderer said",
        )
        import xml.dom.minidom

        document = xml.dom.minidom.parseString(installed)
        ids = {
            element.getAttribute("id")
            for element in document.getElementsByTagName("action")
        }
        check(
            ids == {action.action_id for action in pa.CATALOGUE},
            f"and it carries every catalogued action: {sorted(ids)}",
        )
        # The apostrophes are the whole risk, so they are checked by value.
        check("Switchyard's bounded privileged operations" in installed,
              "an apostrophe survived the printf quoting intact")

        helper = pi.helper_path(root)
        info = os.lstat(helper)
        check(stat.S_IMODE(info.st_mode) == pi.HELPER_MODE, f"{stat.S_IMODE(info.st_mode):04o}")
        # `--map-root-user` maps namespace-root onto this process's real uid,
        # so from out here the files read back as ours. What the check is
        # really worth is that `install -o root -g root` SUCCEEDED -- it is a
        # hard error when the caller cannot set that owner, so a zero exit
        # above is the assertion, and this pins the mapping rather than
        # claiming a uid the namespace never had.
        check(info.st_uid == os.getuid(), f"owned by namespace root: {info.st_uid}")
        check((root / pi.PACKAGE_NAME).is_dir(), "the package landed beside it")
        # Everything the verifier can judge from outside the namespace is
        # clean; the ownership clause cannot be, for the mapping reason above.
        remaining = [
            problem for problem in pi.verify_installation(root, policy_dir)
            if "rather than root:root" not in problem
        ]
        check(remaining == [], f"a fresh install verifies clean: {remaining}")


def test_the_install_is_re_runnable_and_repairs_drift() -> None:
    """Upgrade calls this unconditionally, so a second run must be a repair."""
    import shutil
    import tempfile

    if shutil.which("unshare") is None:
        return
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "id", "-u"], capture_output=True, text=True
    )
    if probe.returncode != 0 or probe.stdout.strip() != "0":
        return

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "lib"
        policy_dir = Path(raw) / "actions"
        release = Path(raw) / "release"
        (release / "scripts" / pi.PACKAGE_NAME).mkdir(parents=True)
        (release / "scripts" / pi.PACKAGE_NAME / "privileged_helper.py").write_text(
            "#!/usr/bin/env python3\n", encoding="utf-8"
        )
        script = "set -eu\n" + "\n".join(
            pi.install_commands(str(release), root=root, policy_dir=policy_dir, sudo="")
        )
        run = ["unshare", "--user", "--map-root-user", "bash", "-c", script]
        first = subprocess.run(run, capture_output=True, text=True)
        check(first.returncode == 0, first.stderr[:400])

        # Drift it the way a careless install would.
        pi.helper_path(root).chmod(0o777)
        pi.policy_path(policy_dir).unlink()
        check(
            any("wider than" in problem for problem in pi.verify_installation(root, policy_dir)),
            f"the drifted tree is reported: {pi.verify_installation(root, policy_dir)}",
        )
        check(
            any("not installed" in problem for problem in pi.verify_installation(root, policy_dir)),
            "and so is the removed policy",
        )

        second = subprocess.run(run, capture_output=True, text=True)
        check(second.returncode == 0, f"a second run repairs rather than failing: {second.stderr[:400]}")
        repaired = [
            problem for problem in pi.verify_installation(root, policy_dir)
            if "rather than root:root" not in problem
        ]
        check(repaired == [], f"and the tree verifies clean again: {repaired}")


def test_the_real_cli_answers_instead_of_hanging() -> None:
    """One command, the real dispatch, on a host with no boundary installed.

    This is the acceptance clause turned into a case: the SYRD-102 shape was
    three minutes in authorization with no prompt and nothing written down.
    Here the same situation -- policy absent, helper absent -- produces a
    complete, actionable report and a non-zero exit, in bounded time, without
    a single privileged call.
    """
    import io
    import contextlib

    launcher = ROOT / "scripts"
    if str(launcher) not in sys.path:
        sys.path.insert(0, str(launcher))
    import team_launcher as tl

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        code = tl.switchyard_main(
            ["privileged-action", "mefp", "deploy-release", f"commit={COMMIT}", "--dry-run"]
        )
    report = buffer.getvalue()
    check(code == 1, f"it reports a problem rather than claiming success: {code}")
    check("org.switchyard.privileged.deploy-release" in report, report)
    check("would run: /opt/switchyard/current/switchyard upgrade mefp" in report, report)
    check(f"--deploy-ref {COMMIT}" in report, f"pinned to the exact commit: {report}")
    check("ready:     no" in report, report)
    check("switchyard upgrade mefp" in report, f"and names the repair: {report}")


def test_the_front_door_is_never_escalated_by_the_wrapper() -> None:
    """Escalating it would change the identity the helper proves.

    The helper establishes which pane ran it by walking its own ancestry to a
    tmux parent. A sudo shell in the middle of that walk is a different
    process with a different identity, so the boundary would be asked about
    the wrong one. This is easy to undo by accident -- the command is called
    `privileged-action` and every instinct says it belongs in the privileged
    set -- so it is pinned here.
    """
    launcher = ROOT / "scripts"
    if str(launcher) not in sys.path:
        sys.path.insert(0, str(launcher))
    import team_launcher as tl

    check("privileged-action" in tl.SWITCHYARD_COMMANDS, "the command exists")
    check(
        "privileged-action" in tl.SWITCHYARD_UNPRIVILEGED_COMMANDS,
        "and the wrapper does not escalate it",
    )
    check(
        "privileged-action" not in tl.SWITCHYARD_PRIVILEGED_COMMANDS,
        "which is the same statement from the other table",
    )


def test_the_boundary_is_installed_by_every_tenants_staging_pass() -> None:
    """Provisioning AND upgrade, through the one renderer both of them use.

    An existing tenant that only ever ran its original operator script is how
    a release ships a helper nothing references (SYRD-234). Staging runs on
    every upgrade, so putting the install here is what makes an existing
    tenant gain the boundary rather than keep whatever it was provisioned with.
    """
    from scripts.ticket_board import project_provision as pv

    lines = pv.role_tooling_staging_commands("mefp", "/opt/switchyard/current")
    body = "\n".join(lines)
    check(str(pi.helper_path()) in body, f"the helper is staged: {body[:400]}")
    check(str(pi.policy_path()) in body, "and the policy installed")
    check(
        "/opt/switchyard/current/scripts/ticket_board/privileged_helper.py" in body,
        "from the selected shared release, not the tenant's own tree",
    )
    for action in pa.CATALOGUE:
        check(action.action_id in body, f"{action.name} is in the installed catalogue")


def test_a_redirected_staging_render_writes_nothing_to_the_real_host() -> None:
    """A regression, and a sharp one: the first version of this wrote /usr/share.

    `role_tooling_staging_commands` takes a staging root so a suite can render
    and RUN the real commands against a temporary tree. The boundary install
    was added with its paths hard-coded, so a redirected render still emitted
    `install ... /usr/local/lib/switchyard` and `/usr/share/polkit-1/actions`.
    Under the suite that runs those commands it failed outright; on a host it
    would have written real system paths from a render that was supposed to be
    a sandbox.
    """
    from scripts.ticket_board import project_provision as pv

    sandbox = "/tmp/switchyard-boundary-sandbox"
    lines = pv.role_tooling_staging_commands("porter", "/rel", staging_root=sandbox)
    escapes = [
        line for line in lines
        if str(pi.PRIVILEGED_ROOT) in line or str(pi.POLICY_DIR) in line
    ]
    check(not escapes, f"nothing escapes the staging root: {escapes[:3]}")
    check(
        any(f"{sandbox}/{pi.SANDBOX_POLICY_DIR_NAME}" in line for line in lines),
        "and the policy is redirected with everything else",
    )
    check(
        any(f"{sandbox}/{pi.HELPER_NAME}" in line for line in lines),
        "as is the helper",
    )

    # And the real render still names the real paths, so the redirect did not
    # simply move the boundary somewhere harmless for everyone.
    real = pv.role_tooling_staging_commands("porter", "/rel")
    check(
        any(str(pi.helper_path()) in line for line in real),
        "the unredirected render still installs to the real root",
    )
    check(
        any(str(pi.policy_path()) in line for line in real),
        "and the real polkit action directory",
    )


def test_selecting_a_release_without_the_boundary_removes_it_rather_than_failing() -> None:
    """Rollback must stay possible, and must not leave a stale helper behind.

    A rollback deliberately selects an OLDER release, and an older release
    carries no privileged helper. An unguarded install fails there, and a
    failing staging step blocks the way back at exactly the moment it is
    needed (SYRD-93). This was not hypothetical: it broke a suite that stages
    a release built from origin/main, because that release predates this file.

    The removal matters as much as the install. Leaving a helper and policy
    installed while the code rolls back would leave polkit authorizing a
    helper the running release knows nothing about.
    """
    import shutil
    import tempfile

    if shutil.which("unshare") is None:
        return
    probe = subprocess.run(
        ["unshare", "--user", "--map-root-user", "id", "-u"], capture_output=True, text=True
    )
    if probe.returncode != 0 or probe.stdout.strip() != "0":
        return

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw) / "lib"
        policy_dir = Path(raw) / "actions"
        new_release = Path(raw) / "new"
        old_release = Path(raw) / "old"
        (new_release / "scripts" / pi.PACKAGE_NAME).mkdir(parents=True)
        (new_release / "scripts" / pi.PACKAGE_NAME / "privileged_helper.py").write_text(
            "#!/usr/bin/env python3\n", encoding="utf-8"
        )
        # An older release: a scripts tree, but no privileged helper in it.
        (old_release / "scripts" / pi.PACKAGE_NAME).mkdir(parents=True)

        def stage(release: Path):
            script = "set -eu\n" + "\n".join(
                pi.install_commands(str(release), root=root, policy_dir=policy_dir, sudo="")
            )
            return subprocess.run(
                ["unshare", "--user", "--map-root-user", "bash", "-c", script],
                capture_output=True, text=True,
            )

        forward = stage(new_release)
        check(forward.returncode == 0, f"the new release installs: {forward.stderr[:300]}")
        check(pi.helper_path(root).is_file(), "and the helper is there")
        check(pi.policy_path(policy_dir).is_file(), "and the policy")

        back = stage(old_release)
        check(
            back.returncode == 0,
            f"selecting an older release does not fail the staging step: {back.stderr[:300]}",
        )
        check(not pi.helper_path(root).exists(), "and the helper is taken away")
        check(not pi.policy_path(policy_dir).exists(), "so polkit authorizes nothing")
        check(not (root / pi.PACKAGE_NAME).exists(), "and the package with it")


def main() -> int:
    def watchdog(_signum, _frame):
        raise TimeoutError("privileged_front_door_test exceeded its time budget")

    signal.signal(signal.SIGALRM, watchdog)
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            signal.alarm(120)
            try:
                value()
            finally:
                signal.alarm(0)
    print(f"privileged_front_door_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
