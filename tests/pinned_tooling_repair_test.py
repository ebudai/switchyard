#!/usr/bin/env python3
"""SYRD-551: a pinned tenant's launch-time tooling repair converges, and its failures are seen.

After the 2026-10-04 crash syrd's board was pinned to release 792774a while
`/opt/switchyard/current` was 2951c5c. Three things kept its launch repairing
itself every time:

- **The Git template's helper list came from the running code.** 792774a has
  no `ticket_board/file_size_policy.py` (SYRD-541 added it), so staging the
  template from 792774a ran an `install` of a file that is not there, and the
  verifier demanded it too.
- **The repair script had no `set -eu`.** A failed step in the middle was hidden
  behind whatever ran last, in the exit status and in the rollout journal.
- **The helper check and repair used `current`.** The launch's protocol check
  compared the staged bridge and attach helper with `current` and restaged the
  whole bundle from there, moving a pinned tenant's tooling off the release its
  board runs, while the bundle check judges the bundle by its own release.

Two installed releases are built in a temporary install root: A, which lacks
`file_size_policy.py` and whose attach helper differs, and B, which `current`
names. Everything is staged by the product's own staging commands, run as root
in a user namespace with a `sudo` that only executes its arguments. The rollout
recorder is the release's own, journalling into the temporary tree. Nothing
outside it is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0
PROJECT = "p551"
SHA_A = "a" * 40
SHA_B = "b" * 40
HELPER = "file_size_policy.py"


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def namespaces_available() -> bool:
    return subprocess.run(["unshare", "--user", "--map-root-user", "true"], capture_output=True).returncode == 0


def _release(install: Path, sha: str, *, lacks_helper: bool) -> Path:
    release = install / "releases" / sha
    shutil.copytree(ROOT / "scripts", release / "scripts", ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("skills",):
        if (ROOT / name).is_dir():
            shutil.copytree(ROOT / name, release / name, ignore=shutil.ignore_patterns("__pycache__"))
    (release / ".switchyard-release.json").write_text(json.dumps({"commit": sha}) + "\n", encoding="utf-8")
    if lacks_helper:
        (release / "scripts" / "ticket_board" / HELPER).unlink()
        attach = release / "scripts" / "switchyard-display-attach"
        attach.write_text(attach.read_text(encoding="utf-8") + "\n# release A's own bytes\n", encoding="utf-8")
    return release


class World:
    """An install root with releases A and B (current), a staging root, a journal, and a sudo that only runs."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.install = tmp / "opt"
        self.a = _release(self.install, SHA_A, lacks_helper=True)
        self.b = _release(self.install, SHA_B, lacks_helper=False)
        (self.install / "current").symlink_to(self.b)
        self.stage_root = tmp / "lib"
        self.stage_root.mkdir()
        self.stage_root.chmod(0o755)
        self.journal = tmp / "journal"
        bin_dir = tmp / "bin"
        bin_dir.mkdir()
        sudo = bin_dir / "sudo"
        sudo.write_text('#!/bin/sh\nexec "$@"\n', encoding="utf-8")
        sudo.chmod(0o755)
        os.environ["SWITCHYARD_SHARED_INSTALL_ROOT"] = str(self.install)
        os.environ["SWITCHYARD_ROLLOUT_JOURNAL_ROOT"] = str(self.journal)
        os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}"

    @property
    def staged(self) -> Path:
        return self.stage_root / PROJECT

    def stage(self, release: Path) -> subprocess.CompletedProcess[str]:
        from scripts import team_launcher as launcher

        script = "set -eu\n" + "\n".join(
            launcher.role_tooling_staging_commands(PROJECT, str(release), staging_root=self.stage_root))
        return subprocess.run(["bash", "-c", script], capture_output=True, text=True)

    def problems(self, release: Path) -> list[str]:
        from scripts import team_launcher as launcher

        return launcher.staged_role_tooling_problems(PROJECT, str(release), staging_root=self.staged, expect_uid=0)

    def template_helpers(self) -> set[str]:
        hooks = self.staged / "git-template" / "hooks"
        return {p.name for p in hooks.iterdir()} if hooks.is_dir() else set()


def case_the_template_stages_only_what_the_release_ships() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd551-template.") as raw:
        world = World(Path(raw))
        a = world.stage(world.a)
        a_helpers, a_problems = world.template_helpers(), world.problems(world.a)
        b = world.stage(world.b)
        b_helpers, b_problems = world.template_helpers(), world.problems(world.b)
        back = world.stage(world.a)
        back_helpers, back_problems = world.template_helpers(), world.problems(world.a)
    check(a.returncode == 0, f"staging from a release without {HELPER} succeeds: {a.stderr[-400:]}")
    check(HELPER not in a_helpers and {"pre-commit", "warn-file-size-limit.py", "report_file_size_limit.py",
                                        "warn-worktree-count.py"} <= a_helpers,
          f"its template has the hook and the helpers it ships, and not {HELPER}: {sorted(a_helpers)}")
    check(a_problems == [], f"and the post-stage check against that release passes: {a_problems}")
    check(b.returncode == 0 and HELPER in b_helpers and b_problems == [],
          f"a release that ships it stages it, and checks clean: {sorted(b_helpers)} {b_problems}")
    check(back.returncode == 0 and HELPER not in back_helpers and back_problems == [],
          f"and going back to the older release takes it away again: {sorted(back_helpers)} {back_problems}")


def case_the_verifier_holds_the_template_to_the_selected_release() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd551-verify.") as raw:
        world = World(Path(raw))
        world.stage(world.b)
        against_a = world.problems(world.a)
        world.stage(world.a)
        (world.staged / "git-template" / "hooks" / HELPER).unlink(missing_ok=True)
        world.stage(world.b)
        (world.staged / "git-template" / "hooks" / HELPER).unlink()
        missing_in_b = world.problems(world.b)
    check(any(HELPER in p and "is not in this release" in p for p in against_a),
          f"a helper the selected release lacks is reported as left behind: {against_a}")
    check(any(HELPER in p and "is not staged" in p for p in missing_in_b),
          f"and one it ships that is missing is reported missing: {missing_in_b}")


def case_a_failed_middle_step_fails_the_repair_and_the_record() -> None:
    from scripts import team_launcher as launcher
    from scripts.ticket_board import rollout_journal

    with tempfile.TemporaryDirectory(prefix="syrd551-fail.") as raw:
        world = World(Path(raw))
        from scripts.ticket_board import privileged_install

        # A file where the boundary's policy directory goes: that `install -d`
        # fails, early in the script, and every step after it would succeed.
        blocker = world.stage_root / privileged_install.SANDBOX_POLICY_DIR_NAME
        blocker.write_text("not a directory\n", encoding="utf-8")
        command = launcher.tenant_control_repair_command(PROJECT, release_root=str(world.b), root=world.stage_root)
        done = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
        records = rollout_journal.attempts(PROJECT, root=world.journal)
        said: list[str] = []
        problem = launcher.repair_tenant_control_helper(
            PROJECT, release_root=str(world.b), root=world.stage_root,
            runner=lambda args, **kw: subprocess.run(args, capture_output=True, text=True), print_func=said.append)
    check(command.startswith("sudo ") and "switchyard-record-rollout" in command,
          f"the repair runs through the release's rollout recorder: {command[:160]}")
    check(done.returncode != 0, f"the repair exits non-zero when a middle step fails: {done.returncode}")
    check(records and records[-1].get("status") not in ("succeeded", "running") and records[-1].get("exit_status") not in (0, None),
          f"and the rollout record says it failed: {records[-1:] }")
    check(problem.startswith(f"repairing the tenant control helper for {PROJECT} failed"),
          f"and the launch is told: {problem!r}")


def case_a_pinned_tenant_converges_on_its_own_release() -> None:
    from scripts import team_launcher as launcher
    from scripts.ticket_board import rollout_journal

    with tempfile.TemporaryDirectory(prefix="syrd551-pinned.") as raw:
        world = World(Path(raw))
        staged = world.stage(world.a)
        assert staged.returncode == 0, staged.stderr
        assert world.problems(world.a) == [], world.problems(world.a)
        # Interrupted staging: the bridge is gone; the rest is release A's.
        (world.staged / "switchyard-tenant-control").unlink()
        grant = {"project": PROJECT, "authorized_user": launcher.current_user_name()}
        runs: list[list[str]] = []

        def runner(args, **kwargs):
            runs.append(list(args))
            return subprocess.run(list(args), capture_output=True, text=True)

        said: list[str] = []
        launcher.ensure_tenant_control_helper(PROJECT, grant=grant, root=world.stage_root, owner_uid=0,
                                              runner=runner, print_func=said.append)
        first_runs = len(runs)
        after_a = world.problems(world.a)
        marker = json.loads((world.staged / ".switchyard-release.json").read_text(encoding="utf-8"))["commit"]
        launcher.ensure_tenant_control_helper(PROJECT, grant=grant, root=world.stage_root, owner_uid=0,
                                              runner=runner, print_func=said.append)
        second_runs = len(runs) - first_runs
        records = rollout_journal.attempts(PROJECT, root=world.journal)
    check(first_runs == 1, f"the absent bridge is repaired with one privileged step: {runs}")
    check(marker == SHA_A and after_a == [],
          f"from the release the tenant is pinned to, so it checks clean against A: {marker} {after_a}")
    check(any(str(world.a) in line for line in said), f"and the launch names that release: {said}")
    check(second_runs == 0, f"a second launch runs no repair: {runs[first_runs:]}")
    check(len(records) == 1 and records[0].get("exit_status") == 0, f"one journalled repair, succeeded: {records}")


def case_a_tenant_with_no_staged_release_still_uses_current() -> None:
    from scripts import team_launcher as launcher

    with tempfile.TemporaryDirectory(prefix="syrd551-nothing.") as raw:
        world = World(Path(raw))
        world.staged.mkdir(parents=True)
        grant = {"project": PROJECT, "authorized_user": launcher.current_user_name()}
        said: list[str] = []
        launcher.ensure_tenant_control_helper(
            PROJECT, grant=grant, root=world.stage_root, owner_uid=0,
            runner=lambda args, **kw: subprocess.run(list(args), capture_output=True, text=True), print_func=said.append)
        marker = json.loads((world.staged / ".switchyard-release.json").read_text(encoding="utf-8"))["commit"]
        clean = world.problems(world.b)
    check(marker == SHA_B and clean == [], f"a tenant with nothing staged is staged from current: {marker} {clean}")


def privileged_child() -> int:
    for name, value in sorted(globals().items()):
        if name.startswith("case_") and callable(value):
            value()
    print(f"pinned_tooling_repair_test: privileged child ran {CHECKS} checks")
    return 0


def test_the_source_renders_each_helper_conditionally() -> None:
    """Unprivileged: the rendered template commands guard every helper on the release."""
    from scripts.ticket_board.provision_role_tooling import git_template_files, git_template_staging_commands

    release = "/rel"
    commands = git_template_staging_commands(release, "/stage")
    for source, relative, _mode in git_template_files(release)[1:]:
        guard = commands.index(f"    if [ -f '{source}' ]; then")
        check(commands[guard + 1].strip().startswith("sudo install") and source in commands[guard + 1]
              and commands[guard + 2] == "    fi", f"{relative} is installed only if the release carries it")


def test_the_repair_script_stops_at_the_first_failure() -> None:
    from scripts import team_launcher as launcher

    saved = launcher._rollout_recorder_path
    launcher._rollout_recorder_path = lambda: None
    try:
        command = launcher.tenant_control_repair_command(PROJECT, release_root="/rel", root=Path("/nowhere"))
    finally:
        launcher._rollout_recorder_path = saved
    check(command.startswith("bash -c ") and "set -eu\n" in command.split(" ", 2)[2],
          f"the repair script is run with set -eu: {command[:80]}")


def main() -> int:
    if "--privileged-child" in sys.argv:
        return privileged_child()
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
    if not namespaces_available():
        print("pinned_tooling_repair_test: user namespaces are unavailable; the staging half cannot run")
        return 1
    done = subprocess.run(["unshare", "--user", "--map-root-user", sys.executable, __file__, "--privileged-child"],
                          capture_output=True, text=True)
    if done.returncode != 0:
        print(done.stdout + done.stderr)
        return 1
    print(done.stdout.strip())
    print(f"pinned_tooling_repair_test: {CHECKS} unprivileged checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
