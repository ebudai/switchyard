#!/usr/bin/env python3
"""SYRD-284: an old-pinned tenant on a newer shared release gets the new boundary.

MEFP followed SYRD-283's chain. The host's shared release was 4470716 (with the
new `preview-upgrade` / `upgrade-tenant-release` actions); MEFP was pinned to
49abeb4 from its last upgrade; the installed privileged boundary still came from
49abeb4. `upgrade-tenant` ran the NEW launcher unpinned, which recovered the OLD
pin, refused because the launcher was not that release -- advising to reinstall
the obsolete one -- and so never installed the boundary that carries the new
actions. `preview-upgrade` then said the policy predates it and pointed back at
`upgrade-tenant`: a loop.

The boundary is host-wide and its helper always runs the SHARED launcher, so it
now follows the shared release: `install-shared-release` installs it, and an
unpinned upgrade whose remembered pin is behind the running release installs it
from that release -- as root, before any of the tenant's files are touched --
and then refuses the tenant part, naming the pinned preview and upgrade.

The chain runs as uid 0 in a user namespace against two real releases staged
the way root stages them: `origin/main` as the tenant's old pin, `HEAD` as the
host's new shared release.
"""

from __future__ import annotations

import hashlib
import re
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def tree(path: Path, *, skip: str = "tooling") -> dict[str, str]:
    return {
        str(p.relative_to(path)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(path.rglob("*"))
        if p.is_file() and not p.is_symlink() and skip not in p.relative_to(path).parts
    }


def shell_runner():
    """The upgrade's fake runner, except that the boundary's install script really runs.

    Everything else the upgrade would run -- tmux, git, the board -- is recorded
    and not run, so what it asked for can be read back: nothing but the one
    script, on a refused upgrade.
    """
    from team_launcher_test_helpers import FakeRunner

    class ShellRunner(FakeRunner):
        def __call__(self, command, *args, **kwargs):
            if list(command[:2]) == ["sh", "-euc"]:
                self.calls.append(list(command))
                return subprocess.run(command, *args, **kwargs)
            return super().__call__(command, *args, **kwargs)

    return ShellRunner()


def stale_door(policy_file: Path) -> bool:
    """Whether `privileged-action <p> preview-upgrade` is refused as predating the action."""
    from scripts.ticket_board import polkit_preflight
    from scripts.ticket_board import privileged_front_door as door
    from scripts.ticket_board import privileged_helper as ph

    plan = door.plan_action(
        "porter", "preview-upgrade", {"commit": "a" * 40},
        verify=lambda *a, **k: [],
        read_policy=lambda _path: policy_file.read_text(encoding="utf-8"),
        identity=lambda: ph.CallerIdentity(1, 2, 3),
        decide=lambda action_id, **k: polkit_preflight.PolkitDecision(polkit_preflight.AUTHORIZED, action_id),
    )
    stale = [problem for problem in plan.problems if "predates preview-upgrade" in problem]
    if stale:
        named = [printed for printed in re.findall(r"`([^`]*)`", stale[0]) if printed.startswith("switchyard ")]
        check([parsed_action(printed)[0] for printed in named] == ["upgrade-tenant"],
              f"the one command it names is the bootstrap, as the real parser reads it: {named}")
        check("upgrade-tenant-release" not in stale[0] and "reinstall an older" in stale[0], stale[0])
    return bool(stale)


def parsed_action(printed: str) -> tuple[str, dict]:
    """A printed `switchyard privileged-action` line, read by the real parser and catalogue."""
    import shlex

    from team_launcher_test_helpers import team_launcher
    from scripts.ticket_board import privileged_actions as pa

    argv = shlex.split(printed)
    check(argv[:2] == ["switchyard", "privileged-action"], printed)
    args = team_launcher._build_switchyard_privileged_action_parser().parse_args(argv[2:])
    values = dict(item.split("=", 1) for item in args.values)
    return args.action, pa.action_for(args.action).validate({"project": args.project, **values})


def privileged_chain() -> None:
    import team_launcher_test_helpers as helpers  # noqa: F401 -- sets the sandboxed install roots
    import team_launcher_upgrade_cutover_test as cutover
    from team_launcher_test_helpers import stage_trusted_releases, team_launcher, trusted_release_root_for
    from scripts.ticket_board import privileged_install, shared_release_activation

    check(os.geteuid() == 0, "this half runs as root, so root's ownership checks are real")
    staged = stage_trusted_releases()
    old, new = staged.get("origin/main"), staged.get("HEAD")
    if not old or not new or old == new:
        print("host_boundary_bootstrap_test: HEAD is origin/main here; nothing newer to bootstrap to")
        return
    old_root, new_root = trusted_release_root_for(staged, "origin/main"), trusted_release_root_for(staged, "HEAD")
    running = team_launcher.shared_switchyard_release_for_path(new_root)
    check(running is not None and running.marker_commit == new, f"the new release is a real installed one: {running}")
    saved = team_launcher.running_launcher_release
    # This process IS the new release's code; what it cannot be is installed
    # at the release path. The real release reader supplies which one it is.
    team_launcher.running_launcher_release = lambda root=None: running
    try:
        with tempfile.TemporaryDirectory(prefix="syrd284-chain.") as raw:
            tmp = Path(raw)
            config_path, _ = cutover._declarative_tenant(tmp)
            config = team_launcher.load_project_config("porter", config_path)
            team_launcher.record_upgrade_source(config, source_repo=old_root, commit_git_dir=None, deploy_ref=old)
            check(team_launcher.read_upgrade_source(config)["deploy_ref"] == old, "the tenant is pinned to the old release")
            boundary_root, policy_dir = privileged_install.roots_for(config_path.parent / "tooling")
            policy_file = privileged_install.policy_path(policy_dir)
            # The host as MEFP found it: a boundary installed before these
            # actions existed. The fixture's own is current, so they are cut out.
            stale = re.sub(r'\s*<action id="org\.switchyard\.privileged\.(preview-upgrade|upgrade-tenant-release)">'
                           r'.*?</action>', "", policy_file.read_text(encoding="utf-8"), flags=re.S)
            check("preview-upgrade" not in stale and "upgrade-tenant" in stale, "a stale boundary is in place")
            policy_file.write_text(stale, encoding="utf-8")
            check(stale_door(policy_file), "and the front door says it predates the action")
            before = tree(tmp)

            # 1. The dry run: the same verdict, nothing written.
            runner = shell_runner()
            code, said, _ = cutover._upgrade(config_path, as_root=True, exists=set(), dry_run=True, runner=runner)
            check(runner.calls == [], f"a dry run runs nothing at all: {runner.calls}")
            check(code == 1, f"the dry run refuses as the real run will: {said[-600:]}")
            check(f"pinned to {old}" in said and f"now runs {new}" in said, said[-600:])
            check("would install this host's privileged boundary" in said, said[-600:])
            check(f"preview-upgrade commit={new}" in said and f"upgrade-tenant-release commit={new}" in said,
                  f"it names the pinned way forward: {said[-600:]}")
            check("install that release first" not in said, "and never the obsolete release")
            named = [parsed_action(printed) for printed in re.findall(r"`(switchyard privileged-action [^`]*)`", said)]
            check([(name, values.get("commit")) for name, values in named]
                  == [("preview-upgrade", new), ("upgrade-tenant-release", new)],
                  f"both commands parse and validate as printed, for the host's release: {named}")
            check(policy_file.read_text(encoding="utf-8") == stale, "a dry run installs nothing")
            check(tree(tmp) == before, "and writes nothing of the tenant's")

            # 2. `upgrade-tenant` as the old boundary runs it: unpinned, as root.
            runner = shell_runner()
            code, said, _ = cutover._upgrade(config_path, as_root=True, exists=set(), runner=runner)
            check(code == 1, f"the tenant upgrade is refused: {said[-600:]}")
            check([call[:2] for call in runner.calls] == [["sh", "-euc"]],
                  f"and it ran the boundary install and nothing else -- no pane, board or git: {runner.calls}")
            check(f"installed this host's privileged boundary from {new_root}" in said, said[-600:])
            policy = privileged_install.policy_path(policy_dir).read_text(encoding="utf-8")
            for action in ("preview-upgrade", "upgrade-tenant-release"):
                check(f'id="org.switchyard.privileged.{action}"' in policy, f"the boundary now carries {action}")
            check(privileged_install.verify_installation(boundary_root, policy_dir) == [],
                  "root-owned, in the installed modes")
            check(not stale_door(policy_file), "and the front door no longer calls it stale")
            check(tree(tmp) == before, "and not one of the tenant's files was written")
            check(team_launcher.read_upgrade_source(config)["deploy_ref"] == old, "its pin is untouched")

            # 3. Idempotent: running it again changes nothing and says the same.
            policy_bytes = privileged_install.policy_path(policy_dir).read_bytes()
            code, said_again, _ = cutover._upgrade(config_path, as_root=True, exists=set(), runner=shell_runner())
            check(code == 1 and privileged_install.policy_path(policy_dir).read_bytes() == policy_bytes,
                  "a rerun reinstalls the same bytes")

            # 4. The pinned upgrade it named is past both release checks, and
            #    moves the pin to exactly the release previewed.
            code, said, _ = cutover._upgrade(config_path, as_root=True, exists=set(),
                                             source_repo=new_root, deploy_ref=new)
            check("is pinned to" not in said and "install that release first" not in said,
                  f"the pinned upgrade is not refused for its release: {said[-800:]}")
            check(team_launcher.read_upgrade_source(config)["deploy_ref"] == new,
                  f"and records that release as the pin: {team_launcher.read_upgrade_source(config)}")

        # 5. A host install of a shared release carries its boundary.
        with tempfile.TemporaryDirectory(prefix="syrd284-host.") as raw:
            sandbox = Path(raw) / "boundary"
            said: list[str] = []
            saved_activate = shared_release_activation.activate
            shared_release_activation.activate = lambda commit, **k: shared_release_activation.Activation(
                commit=commit, release_root=str(new_root))
            try:
                code = team_launcher.switchyard_install_shared_release_command(
                    new, print_func=said.append, boundary_root=sandbox)
            finally:
                shared_release_activation.activate = saved_activate
            host_root, host_policy = privileged_install.roots_for(sandbox)
            check(code == 0, f"the host install succeeds: {said}")
            check(privileged_install.policy_path(host_policy).is_file(),
                  f"installing a shared release installs its boundary: {said}")
            check('id="org.switchyard.privileged.preview-upgrade"' in
                  privileged_install.policy_path(host_policy).read_text(encoding="utf-8"),
                  "and the release's actions are usable at once, with no tenant upgrade")
            check(privileged_install.verify_installation(host_root, host_policy) == [], "verified")
    finally:
        team_launcher.running_launcher_release = saved


def test_a_pin_level_with_the_host_is_left_to_the_ordinary_upgrade() -> None:
    from team_launcher_test_helpers import team_launcher

    saved = (team_launcher.running_launcher_release, team_launcher.resolve_trusted_upgrade_release)
    release = type("R", (), {"marker_commit": "a" * 40, "root": Path("/nonexistent")})()
    team_launcher.running_launcher_release = lambda root=None: release
    team_launcher.resolve_trusted_upgrade_release = lambda *a, **k: (type("T", (), {"commit": "a" * 40})(), [])
    try:
        said: list[str] = []
        config = type("C", (), {"project": "porter"})()
        result = team_launcher._recovered_pin_behind_host(
            config, source_repo=None, deploy_ref="a" * 40, dry_run=False, tooling_root=None,
            runner=None, print_func=said.append)
        check(result is None and said == [], f"a pin equal to the running release changes nothing: {said}")
    finally:
        team_launcher.running_launcher_release, team_launcher.resolve_trusted_upgrade_release = saved


def test_an_unprivileged_upgrade_names_the_bootstrap_and_installs_nothing() -> None:
    if os.geteuid() == 0:
        return
    from team_launcher_test_helpers import team_launcher

    old, new = "b" * 40, "c" * 40
    saved = (team_launcher.running_launcher_release, team_launcher.resolve_trusted_upgrade_release)
    team_launcher.running_launcher_release = lambda root=None: type(
        "R", (), {"marker_commit": new, "root": Path("/nonexistent")})()
    team_launcher.resolve_trusted_upgrade_release = lambda *a, **k: (type("T", (), {"commit": old})(), [])
    ran: list = []
    try:
        said: list[str] = []
        result = team_launcher._recovered_pin_behind_host(
            type("C", (), {"project": "porter"})(), source_repo=None, deploy_ref=old, dry_run=False,
            tooling_root=None, runner=lambda *a, **k: ran.append(a), print_func=said.append)
    finally:
        team_launcher.running_launcher_release, team_launcher.resolve_trusted_upgrade_release = saved
    text = "\n".join(said)
    check(result == 1 and not ran, f"refused, and nothing was run: {ran} {text}")
    named = [parsed_action(printed)[0] for printed in re.findall(r"`(switchyard privileged-action [^`]*)`", text)]
    check(named == ["upgrade-tenant", "preview-upgrade", "upgrade-tenant-release"],
          f"it names the bootstrap, then the pinned preview and upgrade: {text}")


def test_the_bootstrap_vehicle_is_the_directors_and_not_a_siblings() -> None:
    """`upgrade-tenant` is what reaches the bootstrap; the helper's REAL caller check."""
    import contextlib
    import io

    import director_upgrade_boundary_test as director
    import privileged_helper_boundary_test as boundary
    from scripts.ticket_board import privileged_helper as ph
    from scripts.ticket_board import privileged_operations as po
    from scripts.ticket_board.peer_identity import read_process

    me = read_process(os.getpid())
    for registered, expect_ran in ((os.getpid(), True), (os.getpid() + 100000, False)):
        listing = boundary._registered(registered, me.start_time if expect_ran else 7, os.getuid())
        board_get, _ = boundary._board(boundary.WORKFLOW, listing)
        ran: list = []
        buffer = io.StringIO()
        with boundary.swapped(
            ph,
            privileged_install=boundary._StubInstall([]),
            caller_identity=lambda *a, **k: ph.CallerIdentity(os.getpid(), me.start_time, os.getuid()),
            board_url_for=lambda project, **k: "http://127.0.0.1:1/",
            _board_get=board_get,
            _attempt_factory=lambda project, command, **kw: boundary.FakeAttempt(project, command, log=[], **kw),
            _run_command=lambda command, *, timeout: ran.append(list(command)) or boundary.Completed(0),
        ):
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                code = ph.main(["upgrade-tenant", f"project={director.PROJECT}"])
        if expect_ran:
            check(code == 0 and ran == [[po.LAUNCHER, "upgrade", director.PROJECT]],
                  f"the registered Director reaches it: {buffer.getvalue()}")
        else:
            check(code == 1 and not ran, f"a same-uid sibling does not: {buffer.getvalue()}")


def main() -> int:
    if "--privileged-child" in sys.argv:
        privileged_chain()
        print(f"host_boundary_bootstrap_test: privileged child ran {CHECKS} checks")
        return 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    child = subprocess.run(
        ["unshare", "--user", "--map-root-user", sys.executable, __file__, "--privileged-child"],
        text=True, capture_output=True, check=False,
    )
    if child.returncode != 0 or "privileged child ran" not in child.stdout:
        print(child.stdout[-3000:] + child.stderr[-3000:])
        return 1
    print(child.stdout.strip().splitlines()[-1])
    print(f"host_boundary_bootstrap_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
