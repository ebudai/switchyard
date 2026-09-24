#!/usr/bin/env python3
"""SYRD-257: the size warning reaches the source checkouts roles actually commit in.

Implementers commit in source clones they make themselves, from their panes,
with a plain `git clone` -- and in worktrees linked to those clones. Switchyard
installed its warning-only 1,250-line policy in the installer's checkout and in
each tenant's own repositories, and in nothing else, so on this host 100 of 103
source roots had no pre-commit hook and a commit to a 37,000-line file said
nothing.

The fix gives a role's NEW repositories the policy at birth: root stages a Git
template with the tenant's role tooling, and role panes name it in
GIT_TEMPLATE_DIR. These cases run the real rendered staging commands, read the
variable from the real pane environment, and commit through real `git` in an
independently cloned source repository and in a worktree linked to it. They
also pin what must not change: the warning never blocks, the upstream hook's
verdict is the commit's, and nothing global is configured.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts"), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from team_launcher_test_helpers import _stage_role_tooling, team_launcher  # noqa: E402
from scripts import repository_hooks  # noqa: E402
from scripts.ticket_board import project_provision  # noqa: E402

PROJECT = "porter"
CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


class Sandbox:
    """A home with an empty global Git config, and a staged role tooling bundle."""

    def __init__(self, tmp: Path, *, release_root: Path = ROOT) -> None:
        self.tmp = tmp
        self.home = tmp / "home"
        self.home.mkdir()
        self.global_config = self.home / ".gitconfig"
        self.staging_root = _stage_role_tooling(tmp, PROJECT, release_root=release_root)
        os.environ[project_provision.TENANT_CONTROL_ROOT_ENV] = str(self.staging_root)
        self.base_env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": str(self.global_config),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "Role",
            "GIT_AUTHOR_EMAIL": "role@example.invalid",
            "GIT_COMMITTER_NAME": "Role",
            "GIT_COMMITTER_EMAIL": "role@example.invalid",
        }

    def pane_template(self, role_env: dict[str, str] | None = None) -> str:
        """GIT_TEMPLATE_DIR as the launcher writes it into a role's pane command."""
        config_path = self.tmp / f"{PROJECT}.json"
        role = {"role": "main", "cli": "claude", "slot": 0, "workdir": str(self.tmp / "wd")}
        if role_env:
            role["env"] = role_env
        config_path.write_text(json.dumps({
            "project": PROJECT, "board_url": "http://127.0.0.1:1/", "roles": [role],
        }), encoding="utf-8")
        config = team_launcher.load_project_config(PROJECT, config_path)
        command = team_launcher.cli_command_for_role(config.roles[0], session_dir=config.session_dir)
        named = [word.split("=", 1)[1] for word in command if word.startswith("GIT_TEMPLATE_DIR=")]
        return named[-1] if named else ""

    def pane_env(self) -> dict[str, str]:
        """What git run in a role's pane sees."""
        template = self.pane_template()
        return {**self.base_env, **({"GIT_TEMPLATE_DIR": template} if template else {})}

    def git(self, *args: str, cwd: Path, env: dict[str, str] | None = None,
            check_ok: bool = True) -> subprocess.CompletedProcess[str]:
        done = subprocess.run(["git", *args], cwd=cwd, env=env or self.base_env,
                              capture_output=True, text=True, check=False)
        if check_ok and done.returncode != 0:
            raise AssertionError(f"git {' '.join(args)} failed: {done.stderr}")
        return done


def upstream_repository(box: Sandbox) -> Path:
    """The product repository roles clone from, made without any template."""
    origin = box.tmp / "origin.git"
    seed = box.tmp / "seed"
    seed.mkdir()
    box.git("init", "-q", "-b", "main", cwd=seed)
    (seed / "README").write_text("product\n")
    box.git("add", "README", cwd=seed)
    box.git("commit", "-q", "-m", "seed", cwd=seed)
    box.git("clone", "-q", "--bare", str(seed), str(origin), cwd=box.tmp)
    return origin


def write_lines(path: Path, count: int) -> None:
    path.write_text("".join(f"x = {i}\n" for i in range(count)), encoding="utf-8")


def commit(box: Sandbox, repo: Path, message: str, *files: str) -> subprocess.CompletedProcess[str]:
    box.git("add", *files, cwd=repo)
    return box.git("commit", "-q", "-m", message, cwd=repo, env=box.pane_env(), check_ok=False)


def warnings(done: subprocess.CompletedProcess[str]) -> list[str]:
    return [line for line in done.stderr.splitlines() if "consider splitting" in line]


def hooks_dir(box: Sandbox, repo: Path) -> Path:
    common = box.git("rev-parse", "--git-common-dir", cwd=repo).stdout.strip()
    path = Path(common)
    return (path if path.is_absolute() else repo / path) / "hooks"


def test_the_committed_template_hook_is_the_rendered_one() -> None:
    committed = (ROOT / "scripts" / repository_hooks.TEMPLATE_PRE_COMMIT).read_text(encoding="utf-8")
    check(committed == repository_hooks.render_template_pre_commit(),
          "scripts/git_template_pre_commit is stale: regenerate it from render_template_pre_commit()")
    mode = (ROOT / "scripts" / repository_hooks.TEMPLATE_PRE_COMMIT).stat().st_mode
    check(bool(mode & stat.S_IXUSR), "and it is executable in the release")


def test_a_second_clone_and_its_linked_worktree_warn_before_their_first_commit() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd257-clone.") as raw:
        box = Sandbox(Path(raw))
        origin = upstream_repository(box)
        pane = box.pane_env()
        template = Path(pane.get("GIT_TEMPLATE_DIR", ""))
        check(template == box.staging_root / PROJECT / project_provision.GIT_TEMPLATE_DIR_NAME,
              f"the pane names the staged template: {pane.get('GIT_TEMPLATE_DIR')}")
        check(not project_provision.staged_role_tooling_problems(
            PROJECT, str(ROOT), staging_root=box.staging_root / PROJECT),
            "and the staged bundle, template included, verifies")

        # The installer's checkout is not the point; an independent clone is.
        first = box.tmp / "first"
        second = box.tmp / "second"
        for clone in (first, second):
            box.git("clone", "-q", str(origin), str(clone), cwd=box.tmp, env=pane)
        worktree = box.tmp / "second-feature"
        box.git("worktree", "add", "-q", "-b", "feature", str(worktree), cwd=second, env=pane)
        check(hooks_dir(box, worktree) == hooks_dir(box, second),
              "the linked worktree runs its clone's hooks")

        for repo in (second, worktree):
            write_lines(repo / "at_limit.py", 1250)
            quiet = commit(box, repo, "at the limit", "at_limit.py")
            check(quiet.returncode == 0, f"{repo.name}: {quiet.stderr}")
            check(warnings(quiet) == [], f"{repo.name}: 1,250 lines is quiet: {quiet.stderr}")

            write_lines(repo / "over.py", 1251)
            loud = commit(box, repo, "over the limit", "over.py")
            check(loud.returncode == 0, f"{repo.name}: never blocks: {loud.stderr}")
            check(warnings(loud) == [
                "warning: over.py is 1251 lines (soft limit 1250) - consider splitting."
            ], f"{repo.name}: 1,251 warns, once: {loud.stderr}")

            write_lines(repo / "over.py", 1300)
            write_lines(repo / "also_over.py", 1400)
            both = commit(box, repo, "two oversized", "over.py", "also_over.py")
            check(both.returncode == 0, both.stderr)
            check(sorted(warnings(both)) == [
                "warning: also_over.py is 1400 lines (soft limit 1250) - consider splitting.",
                "warning: over.py is 1300 lines (soft limit 1250) - consider splitting.",
            ], f"{repo.name}: one warning per path, on every commit: {both.stderr}")

        check(not box.global_config.exists() or "hooksPath" not in box.global_config.read_text(),
              "and nothing global was configured")


def test_the_upstream_hook_verdict_is_the_commit_verdict() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd257-upstream.") as raw:
        box = Sandbox(Path(raw))
        origin = upstream_repository(box)
        clone = box.tmp / "clone"
        box.git("clone", "-q", str(origin), str(clone), cwd=box.tmp, env=box.pane_env())
        upstream = hooks_dir(box, clone) / "pre-commit.switchyard-upstream"
        upstream.write_text("#!/bin/sh\necho upstream-ran >&2\nexit \"${UPSTREAM_STATUS:-0}\"\n")
        upstream.chmod(0o755)
        write_lines(clone / "over.py", 1251)
        box.git("add", "over.py", cwd=clone)

        refused = box.git("commit", "-q", "-m", "x", cwd=clone,
                          env={**box.pane_env(), "UPSTREAM_STATUS": "3"}, check_ok=False)
        check(refused.returncode != 0, f"the upstream hook's refusal stands: {refused.stderr}")
        check("upstream-ran" in refused.stderr and len(warnings(refused)) == 1,
              f"and the warning still prints beside it: {refused.stderr}")

        accepted = box.git("commit", "-q", "-m", "x", cwd=clone, env=box.pane_env(), check_ok=False)
        check(accepted.returncode == 0, accepted.stderr)
        check("upstream-ran" in accepted.stderr and len(warnings(accepted)) == 1, accepted.stderr)

        # Warning-only means a broken warning too: a helper that crashes must
        # not turn into a refused commit.
        helper = hooks_dir(box, clone) / "warn-file-size-limit.py"
        helper.write_text("#!/bin/sh\necho helper-crashed >&2\nexit 7\n")
        write_lines(clone / "over.py", 1300)
        box.git("add", "over.py", cwd=clone)
        crashed = box.git("commit", "-q", "-m", "y", cwd=clone, env=box.pane_env(), check_ok=False)
        check("helper-crashed" in crashed.stderr, f"the broken helper really ran: {crashed.stderr}")
        check(crashed.returncode == 0, f"and the commit still went through: {crashed.stderr}")


def test_a_later_repair_replaces_the_template_hook_rather_than_nesting_it() -> None:
    """The template's hook is managed; installing over it must not warn twice."""
    with tempfile.TemporaryDirectory(prefix="syrd257-repair.") as raw:
        box = Sandbox(Path(raw))
        origin = upstream_repository(box)
        clone = box.tmp / "clone"
        box.git("clone", "-q", str(origin), str(clone), cwd=box.tmp, env=box.pane_env())
        repository_hooks.install_local_warning(clone, source_root=ROOT)
        check(not (hooks_dir(box, clone) / "pre-commit.switchyard-upstream").exists(),
              "the template hook was not kept as somebody else's upstream hook")
        write_lines(clone / "over.py", 1251)
        done = commit(box, clone, "x", "over.py")
        check(len(warnings(done)) == 1, f"one warning, not one per layer: {done.stderr}")


def test_a_template_a_role_config_names_is_not_overridden() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd257-own.") as raw:
        box = Sandbox(Path(raw))
        check(box.pane_template() == str(box.staging_root / PROJECT / project_provision.GIT_TEMPLATE_DIR_NAME),
              "by default the pane names the staged template")
        check(box.pane_template({"GIT_TEMPLATE_DIR": "/srv/own-template"}) == "/srv/own-template",
              "a role that chose its own keeps it")


def test_repositories_roles_did_not_make_are_left_alone() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd257-scope.") as raw:
        box = Sandbox(Path(raw))
        origin = upstream_repository(box)
        elsewhere = box.tmp / "not-from-a-pane"
        box.git("clone", "-q", str(origin), str(elsewhere), cwd=box.tmp)
        check(not (hooks_dir(box, elsewhere) / "pre-commit").exists(),
              "a clone made outside a role pane gets nothing")


def test_a_release_without_the_template_stages_none_and_names_none() -> None:
    """A rollback to a release that predates this must not leave a template behind."""
    with tempfile.TemporaryDirectory(prefix="syrd257-rollback.") as raw:
        tmp = Path(raw)
        box = Sandbox(tmp)
        template = box.staging_root / PROJECT / project_provision.GIT_TEMPLATE_DIR_NAME
        check((template / "hooks" / "pre-commit").is_file(), "staged from this release")

        older = tmp / "older-release"
        subprocess.run(["cp", "-a", str(ROOT) + "/.", str(older)], check=True)
        (older / "scripts" / repository_hooks.TEMPLATE_PRE_COMMIT).unlink()
        _stage_role_tooling(tmp, PROJECT, release_root=older)
        check(not template.exists(), "restaging from an older release removes the template")
        check(team_launcher.role_git_template_env(PROJECT) == {}, "and panes stop naming it")
        check(not project_provision.staged_role_tooling_problems(
            PROJECT, str(older), staging_root=box.staging_root / PROJECT),
            "and that bundle verifies against its own release")
        check(project_provision.staged_role_tooling_problems(
            PROJECT, str(ROOT), staging_root=box.staging_root / PROJECT),
            "while this release's verifier says its template is missing")


def main() -> int:
    saved = os.environ.get(project_provision.TENANT_CONTROL_ROOT_ENV)
    try:
        for name, value in sorted(globals().items()):
            if name.startswith("test_") and callable(value):
                value()
    finally:
        if saved is None:
            os.environ.pop(project_provision.TENANT_CONTROL_ROOT_ENV, None)
        else:
            os.environ[project_provision.TENANT_CONTROL_ROOT_ENV] = saved
    print(f"role_source_clone_hooks_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
