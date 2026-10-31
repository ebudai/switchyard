#!/usr/bin/env python3
"""SYRD-545: a commit store left by an earlier project is refused, not fetched from.

On otto, `switchyard teardown` kept the owner's home and with it the old
project's commit store (`control.git`, origin `~/Projects/otto-scheduler`) and
role worktrees. `switchyard new` created the new checkout at another path and
named the same store path; the launch saw a ready repository, skipped the clone,
fetched from the old checkout -- since moved -- and said only `fetch failed with
exit 128`.

- **The launch** refuses a ready store whose remote names another checkout,
  naming the store, both checkouts and the remedies, before anything writes to
  it or fetches into it. A matching store, however spelled, behaves as before.
  A store with no recorded remote is left to git, as before.
- **Role worktrees** of another store, or ones the store no longer lists, are
  refused rather than reset.
- **Teardown** lists the store and worktrees it keeps, with the consequence.
- **`switchyard new`** warns about them before it creates anything.
- **SYRD-715:** a store whose remote is the repository the checkout itself
  follows -- the same URL, or one github.com `owner/repository` in any form -- and
  whose role worktrees are all its own, is this project's, and is used without
  being repointed. Another owner or repository of the same name, a host not
  proven equivalent across forms, or a checkout that proves nothing, is refused
  before any git, as before.

Real git repositories on disk throughout, through the real owner-correct git
chokepoint; nothing outside a temporary directory is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for entry in (ROOT, ROOT / "tests"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

from team_launcher_test_helpers import _make_origin_backed_repo, _run_git  # noqa: E402
from scripts import project_worktrees, team_launcher  # noqa: E402

PROJECT = "testing"
ROLES = ("main", "ops")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


class Recorder:
    """A real runner that remembers what it was asked."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append([str(a) for a in args])
        call = dict(kwargs)
        call.setdefault("text", True)
        if "stdout" not in call and "stderr" not in call:
            call.setdefault("capture_output", True)
        return subprocess.run(list(args), **call)

    def git(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if c[:1] == ["git"] and verb in c]


def _config(tmp: Path, repository: Path, *, remote: str = "origin"):
    control = tmp / ".local" / "state" / "switchyard" / "projects" / PROJECT / "control.git"
    layout = tmp / "layout.json"
    layout.write_text(json.dumps({"Orientation": "Horizontal", "Widgets": [
        {"Command": "", "SessionRestoreId": i, "WorkingDirectory": ""} for i, _ in enumerate(ROLES)]}), encoding="utf-8")
    path = tmp / f"{PROJECT}.json"
    path.write_text(json.dumps({
        "desktop_access": {"mode": "headless"}, "project": PROJECT, "layout": str(layout),
        "repository": str(repository), "control_repository": str(control), "worktree_base": str(tmp / "worktrees"),
        "worktree_remote": remote,
        "roles": [{"role": r, "slot": i, "target": f"{PROJECT}-{r}:0.0", "cli": ["codex"]} for i, r in enumerate(ROLES)],
    }), encoding="utf-8")
    team_launcher._control_repository_owner_home = lambda _config: tmp
    return team_launcher.load_project_config(PROJECT, path)


def _checkout(tmp: Path, name: str) -> Path:
    """A project checkout of its own, with its own history."""
    base = tmp / name
    base.mkdir()
    _origin, repo = _make_origin_backed_repo(base)
    return repo


def _origin_of(control: Path, remote: str = "origin") -> str:
    return _run_git(["git", "-C", str(control), "config", "--get", f"remote.{remote}.url"]).stdout.strip()


def _refs(control: Path) -> str:
    return _run_git(["git", "-C", str(control), "for-each-ref", "--format=%(refname) %(objectname)"]).stdout


def _first_project(tmp: Path):
    """The earlier project: its checkout, its store, cloned the ordinary way."""
    old = _checkout(tmp, "old")
    old_config = _config(tmp, old)
    first = team_launcher.ensure_control_repository(old_config, runner=Recorder())
    assert first.ok, first.failed_roles
    return old, old_config.control_repository


def test_a_store_cloned_from_another_checkout_is_refused_before_any_fetch() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd545-other.") as raw:
        tmp = Path(raw)
        old, control = _first_project(tmp)
        before = _refs(control)
        new = _checkout(tmp, "new")
        config = _config(tmp, new)
        runner = Recorder()
        result = team_launcher.ensure_control_repository(config, runner=runner)
        after_origin, after_refs = _origin_of(control), _refs(control)
    reasons = set(result.failed_roles.values())
    check(not result.ok and set(result.failed_roles) == set(ROLES) and len(reasons) == 1,
          f"every role fails with one reason: {result.failed_roles}")
    reason = reasons.pop()
    check(str(control) in reason and str(old) in reason and str(new) in reason,
          f"it names the store, the checkout it fetches from, and this project's: {reason}")
    check("mv " in reason and "remote set-url origin" in reason and "teardown" in reason,
          f"and the two remedies, and why it happens: {reason}")
    check(not any(c[:1] == ["git"] for c in runner.calls),
          f"no git ran against it at all: nothing fetched, configured or cloned: {runner.calls}")
    check(after_origin == str(old) and after_refs == before, "the store is exactly as it was")


def test_the_otto_case_says_why_rather_than_exit_128() -> None:
    """The earlier checkout has been moved aside, as otto's was."""
    with tempfile.TemporaryDirectory(prefix="syrd545-moved.") as raw:
        tmp = Path(raw)
        old, control = _first_project(tmp)
        old.rename(old.with_name(old.name + "-legacy"))
        config = _config(tmp, _checkout(tmp, "new"))
        runner = Recorder()
        result = team_launcher.ensure_control_repository(config, runner=runner)
    reason = next(iter(result.failed_roles.values()), "")
    check(str(old) in reason and "exit 128" not in reason and runner.git("fetch") == [],
          f"the refusal names the old checkout, and no fetch fails on it: {reason}")


def test_a_store_that_follows_this_repository_behaves_as_before() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd545-same.") as raw:
        tmp = Path(raw)
        old, control = _first_project(tmp)
        link = tmp / "checkout-link"
        link.symlink_to(old, target_is_directory=True)
        for spelling in (old, link):
            runner = Recorder()
            result = team_launcher.ensure_control_repository(_config(tmp, spelling), runner=runner)
            git = [c[3:5] for c in runner.calls if c[:1] == ["git"]]
            check(result.ok and git == [["config", "--replace-all"], ["fetch", "origin"]],
                  f"{spelling}: the store is used, with exactly the git it always ran -- the remote is read from "
                  f"its config file, not by running git: {runner.calls}")


def test_only_the_configured_remotes_url_counts() -> None:
    """Another remote naming this checkout, earlier in the file, does not make the store this project's."""
    with tempfile.TemporaryDirectory(prefix="syrd545-mirror.") as raw:
        tmp = Path(raw)
        old, control = _first_project(tmp)
        new = _checkout(tmp, "new")
        config_file = control / "config"
        config_file.write_text(f'[remote "mirror"]\n\turl = {new}\n' + config_file.read_text(encoding="utf-8"),
                               encoding="utf-8")
        result = team_launcher.ensure_control_repository(_config(tmp, new), runner=Recorder())
    reason = next(iter(result.failed_roles.values()), "")
    check(not result.ok and f"its origin is {old}" in reason,
          f"the store follows the old checkout through origin, and is refused: {result.failed_roles}")


def test_a_store_with_no_recorded_remote_is_left_to_git() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd545-noremote.") as raw:
        tmp = Path(raw)
        old, control = _first_project(tmp)
        _run_git(["git", "-C", str(control), "remote", "remove", "origin"])
        runner = Recorder()
        result = team_launcher.ensure_control_repository(_config(tmp, old), runner=runner)
    reason = next(iter(result.failed_roles.values()), "")
    check(len(runner.git("fetch")) == 1 and "another checkout's commit store" not in reason,
          f"no recorded remote is not evidence: the fetch runs and git says what is wrong: {reason}")


def test_a_renamed_remote_is_the_one_compared() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd545-upstream.") as raw:
        tmp = Path(raw)
        old = _checkout(tmp, "old")
        first = team_launcher.ensure_control_repository(_config(tmp, old, remote="upstream"), runner=Recorder())
        assert first.ok, first.failed_roles
        same = team_launcher.ensure_control_repository(_config(tmp, old, remote="upstream"), runner=Recorder())
        other = team_launcher.ensure_control_repository(_config(tmp, _checkout(tmp, "new"), remote="upstream"),
                                                        runner=Recorder())
    check(same.ok and not other.ok and "its upstream is" in next(iter(other.failed_roles.values())),
          f"the configured remote's URL is what is compared: {same.failed_roles} {other.failed_roles}")


def test_a_worktree_of_another_store_is_refused_not_reset() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd545-worktree.") as raw:
        tmp = Path(raw)
        old, control = _first_project(tmp)
        config = _config(tmp, old)
        first = team_launcher.ensure_control_role_worktrees(config, refresh=True, runner=Recorder())
        assert first.ok, first.failed_roles
        main = Path(next(r.workdir for r in config.roles if r.role == "main"))
        ops = Path(next(r.workdir for r in config.roles if r.role == "ops"))
        # The store is moved aside and replaced; the worktrees are left behind.
        control.rename(control.with_name("control.git.legacy"))
        replaced = team_launcher.ensure_control_repository(config, runner=Recorder())
        assert replaced.ok, replaced.failed_roles
        left_behind = (main / ".git").is_file() and (ops / ".git").is_file()
        runner = Recorder()
        result = team_launcher.ensure_control_role_worktrees(config, refresh=True, runner=runner)
        touched = [c for c in runner.calls if str(main) in c or str(ops) in c]
    for role in ("main", "ops"):
        reason = result.failed_roles.get(role, "")
        check("has since been replaced" in reason and str(control / "worktrees" / role) in reason
              and "nothing here adopts or resets" in reason,
              f"{role}: refused, naming the entry the replaced store no longer has: {reason}")
    check(touched == [], f"and nothing ran in either worktree: {touched}")
    check(left_behind, "the worktrees were there to refuse")


def test_a_worktree_of_a_different_store_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd545-elsewhere.") as raw:
        tmp = Path(raw)
        old, control = _first_project(tmp)
        config = _config(tmp, old)
        other = tmp / "elsewhere" / "control.git"
        _run_git(["git", "clone", "-q", "--bare", str(old), str(other)])
        ops = Path(next(r.workdir for r in config.roles if r.role == "ops"))
        ops.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["git", "--git-dir", str(other), "worktree", "add", "-q", "--detach", str(ops)])
        runner = Recorder()
        result = team_launcher.ensure_control_role_worktrees(config, refresh=True, runner=runner)
    reason = result.failed_roles.get("ops", "")
    check(f"is a worktree of {other.resolve()}, not of this project's commit store {control}" in reason,
          f"a worktree another store added is refused, naming that store: {reason}")
    check(not any(str(ops) in c for c in runner.calls) and "main" not in result.failed_roles,
          f"nothing ran in it, and the other role was set up normally: {result.failed_roles}")


def test_a_repository_of_its_own_at_a_role_path_is_refused_and_a_proper_worktree_still_refreshes() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd545-standalone.") as raw:
        tmp = Path(raw)
        old, control = _first_project(tmp)
        config = _config(tmp, old)
        ops = Path(next(r.workdir for r in config.roles if r.role == "ops"))
        ops.parent.mkdir(parents=True, exist_ok=True)
        _run_git(["git", "init", "-q", str(ops)])
        result = team_launcher.ensure_control_role_worktrees(config, refresh=True, runner=Recorder())
        again = team_launcher.ensure_control_role_worktrees(config, refresh=True, runner=Recorder())
    check("is a repository of its own" in result.failed_roles.get("ops", "") and "main" not in result.failed_roles,
          f"a standalone repository is refused; the worktree this store adds is not: {result.failed_roles}")
    check("main" not in again.failed_roles, f"and a worktree of this store refreshes on the next launch: {again.failed_roles}")


def test_teardown_names_the_store_and_worktrees_it_keeps() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd545-teardown.") as raw:
        tmp = Path(raw)
        registry = tmp / "registry"
        registry.mkdir()

        def run(remove_owner_home: bool) -> list[str]:
            said: list[str] = []
            team_launcher.switchyard_teardown_command(
                "otto", dry_run=True, owner_user="otto-agent", config_dir=tmp / "configs", registry_dir=registry,
                home_base=tmp / "home", remove_owner_home=remove_owner_home,
                runner=lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "no psql here"),
                print_func=said.append,
            )
            return said

        kept, removed = run(False), run(True)
    store = tmp / "home" / "otto-agent" / ".local" / "state" / "switchyard" / "projects" / "otto" / "control.git"
    worktrees = tmp / "home" / "otto-agent" / "otto-worktrees"
    preserved = kept[kept.index("switchyard: preserved by default:") + 1:kept.index("switchyard: actions:")]
    line = next((l for l in preserved if "commit store" in l), "")
    check(str(store) in line and str(worktrees) in line and "switchyard new" in line and "move both aside" in line,
          f"the dry run lists the store and worktrees it keeps, and what that means: {preserved}")
    check(not any("commit store" in l for l in removed), f"with the owner's home removed, they go with it: {removed}")


def test_new_warns_before_creating_anything() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd545-new.") as raw:
        tmp = Path(raw)
        home = tmp / "home"
        store = home / "otto-agent" / ".local" / "state" / "switchyard" / "projects" / "otto" / "control.git"
        worktrees = home / "otto-agent" / "otto-worktrees"

        def preflight() -> list[str]:
            said: list[str] = []
            saved = (team_launcher._precheck_project_path_before_mutating, team_launcher.precheck_new_project,
                     team_launcher.require_agent_clis_for_new_tenant)
            team_launcher._precheck_project_path_before_mutating = lambda owner, path: None
            team_launcher.precheck_new_project = lambda plan, **kw: None
            team_launcher.require_agent_clis_for_new_tenant = lambda clis, **kw: clis
            try:
                team_launcher._check_new_project_preflight(
                    from_artifact=None, source_repo=ROOT, commit_git_dir=None, port=None, database=None, yes=True,
                    home_base=home, port_in_use=lambda _p: False, socket_exists=lambda _p: False, config_dir=None,
                    registry_dir=None, input_func=lambda _q: "", print_func=said.append, agent_cli_policy="refuse",
                    agent_cli_sources=None, include_audit=True, include_designer=True, owner_user="otto-agent",
                    project_dir=home / "otto-agent" / "Projects" / "otto_scheduler", resolved_agy_credential_source="",
                    resolved_project_name="Otto", resolved_slug="otto", runner=lambda *a, **k: None,
                    selected_audit_roles=("audit",), selected_implementer_roles=("main",),
                    selected_role_clis=(("main", "codex"),), selected_router=(), selected_documentation=(), stages=SimpleNamespace(begin=lambda _name: None),
                )
            finally:
                (team_launcher._precheck_project_path_before_mutating, team_launcher.precheck_new_project,
                 team_launcher.require_agent_clis_for_new_tenant) = saved
            return said

        clean = preflight()
        store.mkdir(parents=True)
        worktrees.mkdir(parents=True)
        warned = preflight()
    check(not any("warning" in l for l in clean), f"a clean home is not warned about: {clean}")
    line = next((l for l in warned if l.startswith("switchyard: warning:")), "")
    check(str(store) in line and str(worktrees) in line and "otto_scheduler" in line and "refuses" in line,
          f"leftovers are named before anything is created, with what the launch will do: {warned}")


def _set_url(repository: Path, url: str, remote: str = "origin") -> None:
    _run_git(["git", "-C", str(repository), "config", f"remote.{remote}.url", url])


def _following_canonical(tmp: Path):
    """syrd's shape (SYRD-715): the store fetches the repository the checkout itself follows, not the checkout."""
    checkout, control = _first_project(tmp)
    canonical = checkout.parent / "origin.git"
    config = _config(tmp, checkout)
    created = team_launcher.ensure_control_role_worktrees(config, refresh=True, runner=Recorder())
    assert created.ok, created.failed_roles
    _set_url(control, str(canonical))
    return checkout, control, canonical, config


def test_a_store_following_the_checkouts_own_remote_is_this_projects() -> None:
    """The ticket's reproduction: the checkout's origin is the store's origin, and config names the checkout."""
    with tempfile.TemporaryDirectory(prefix="syrd715-same.") as raw:
        tmp = Path(raw)
        checkout, control, canonical, config = _following_canonical(tmp)
        check(_origin_of(checkout) == _origin_of(control) == str(canonical) and str(config.repository) == str(checkout),
              "the checkout and the store follow one repository, and config names the checkout")
        runner = Recorder()
        result = team_launcher.ensure_control_repository(config, runner=runner)
        added = team_launcher.ensure_control_role_worktrees(config, refresh=True, runner=Recorder())
        after = _origin_of(control)
    check(result.ok and [c[3:5] for c in runner.calls if c[:1] == ["git"]] == [["config", "--replace-all"], ["fetch", "origin"]],
          f"the store is used as it is, fetching the repository it follows: {result.failed_roles} {runner.calls}")
    check(after == str(canonical), f"and is never repointed at the checkout: {after}")
    check(added.ok, f"its worktrees refresh, as a new role's would be added: {added.failed_roles}")


def test_one_github_repository_in_any_of_its_forms_is_the_same() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd715-github.") as raw:
        tmp = Path(raw)
        checkout, control, _canonical, config = _following_canonical(tmp)
        for store_url, checkout_url in (
                ("https://github.com/acme/project.git", "git@github.com:acme/project.git"),
                ("https://github.com/acme/project.git", "ssh://git@github.com/Acme/Project"),
                ("ssh://git@github.com:22/acme/project", "https://github.com/acme/project")):
            _set_url(control, store_url)
            _set_url(checkout, checkout_url)
            said = project_worktrees.control_repository_origin_mismatch(config)
            check(said == "", f"{store_url} and {checkout_url} are one GitHub repository: {said}")


def test_an_unrelated_repository_of_the_same_name_is_still_refused_before_any_git() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd715-other.") as raw:
        tmp = Path(raw)
        checkout, control, _canonical, config = _following_canonical(tmp)
        for store_url, checkout_url, why in (
                ("https://github.com/other/project.git", "git@github.com:acme/project.git", "another owner, same name"),
                ("https://github.com/acme/project-old.git", "git@github.com:acme/project.git", "another repository"),
                ("https://git.example.org/acme/project.git", "git@git.example.org:acme/project.git",
                 "a host nothing here proves equivalent across forms"),
                ("git@github-not-github:acme/project.git", "https://github.com/acme/project.git",
                 "an SSH alias named like GitHub, which may resolve anywhere (Audit)"),
                ("git@github-acme:acme/project.git", "git@github.com:acme/project.git", "a github-* alias against github.com"),
                ("https://ssh.github.com/acme/project.git", "git@github.com:acme/project.git", "a subdomain against github.com"),
                (str(tmp / "elsewhere" / "project.git"), str(tmp / "other" / "project.git"), "two local paths, one basename")):
            _set_url(control, store_url)
            _set_url(checkout, checkout_url)
            before = _refs(control)
            runner = Recorder()
            result = team_launcher.ensure_control_repository(config, runner=runner)
            reason = next(iter(result.failed_roles.values()), "")
            check(not result.ok and f"its origin is {store_url}" in reason and "another checkout's commit store" in reason,
                  f"{why}: refused, as SYRD-545 refuses it: {reason}")
            check(not any(c[:1] == ["git"] for c in runner.calls) and _refs(control) == before and _origin_of(control) == store_url,
                  f"{why}: before any git, the store untouched: {runner.calls}")


def test_a_matching_remote_without_the_rest_of_the_proof_is_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="syrd715-proof.") as raw:
        tmp = Path(raw)
        checkout, control, canonical, config = _following_canonical(tmp)
        # A role worktree another store added: the store does not back this project's worktrees.
        other = tmp / "elsewhere" / "control.git"
        _run_git(["git", "clone", "-q", "--bare", str(checkout), str(other)])
        ops = Path(next(r.workdir for r in config.roles if r.role == "ops"))
        _run_git(["git", "--git-dir", str(control), "worktree", "remove", "--force", str(ops)])
        _run_git(["git", "--git-dir", str(other), "worktree", "add", "-q", "--detach", str(ops)])
        foreign = project_worktrees.control_repository_origin_mismatch(config)
        check("another checkout's commit store" in foreign, f"a matching remote, a foreign role worktree: refused: {foreign}")
        shutil.rmtree(ops)
        check(project_worktrees.control_repository_origin_mismatch(config) == "", "with that worktree gone, it is this project's")
        # The checkout's own remote of that name is what proves it: another remote, or none, does not.
        _run_git(["git", "-C", str(checkout), "remote", "rename", "origin", "upstream"])
        check("another checkout's commit store" in project_worktrees.control_repository_origin_mismatch(config),
              "a checkout whose origin is gone proves nothing")
        _run_git(["git", "-C", str(checkout), "remote", "rename", "upstream", "origin"])
        # A checkout that is not a repository of its own (its .git a file, or absent) proves nothing either.
        shutil.move(str(checkout / ".git"), str(tmp / "moved-dot-git"))
        (checkout / ".git").write_text(f"gitdir: {tmp / 'moved-dot-git'}\n", encoding="utf-8")
        check("another checkout's commit store" in project_worktrees.control_repository_origin_mismatch(config),
              "a checkout whose .git is a pointer is not read through")
        (checkout / ".git").unlink()
        check("another checkout's commit store" in project_worktrees.control_repository_origin_mismatch(config),
              "and one with no .git proves nothing")
        check(_origin_of(control) == str(canonical), "the store's remote was never touched")


def test_an_existing_worktree_that_proves_nothing_refuses_before_any_git() -> None:
    """Audit, SYRD-715: inconclusive worktree metadata is no proof the store backs it; only an absent directory is."""
    with tempfile.TemporaryDirectory(prefix="syrd715-inconclusive.") as raw:
        tmp = Path(raw)
        checkout, control, canonical, config = _following_canonical(tmp)
        ops = Path(next(r.workdir for r in config.roles if r.role == "ops"))
        dot_git = ops / ".git"
        good = dot_git.read_text(encoding="utf-8")
        entry = Path(good.split(":", 1)[1].strip())

        def restore() -> None:
            if ops.is_symlink():
                ops.unlink()
                shutil.move(str(tmp / "real-ops"), str(ops))
            if dot_git.is_symlink() or dot_git.exists():
                dot_git.chmod(0o644) if not dot_git.is_symlink() else None
                dot_git.unlink()
            dot_git.write_text(good, encoding="utf-8")
            (entry / "gitdir").write_text(f"{dot_git}\n", encoding="utf-8")

        def link_role_directory() -> None:
            shutil.move(str(ops), str(tmp / "real-ops"))
            ops.symlink_to(tmp / "real-ops", target_is_directory=True)

        cases = [
            ("a .git that is not a gitdir record", lambda: dot_git.write_text("not a gitdir record\n", encoding="utf-8")),
            ("a missing .git", lambda: dot_git.unlink()),
            ("a .git that is a link", lambda: (dot_git.unlink(), (tmp / "moved.git").write_text(good, encoding="utf-8"),
                                               dot_git.symlink_to(tmp / "moved.git"))),
            ("a store entry that names another worktree back", lambda: (entry / "gitdir").write_text(f"{tmp / 'x' / '.git'}\n",
                                                                                                    encoding="utf-8")),
            ("a role directory that is a link", link_role_directory),
        ]
        if os.geteuid() != 0:
            cases.append(("an unreadable .git", lambda: dot_git.chmod(0)))
        for label, make in cases:
            make()
            said = project_worktrees.control_repository_origin_mismatch(config)
            runner = Recorder()
            result = team_launcher.ensure_control_repository(config, runner=runner)
            check("another checkout's commit store" in said and not result.ok,
                  f"{label}: the store is not proven this project's, and is refused: {said or result.failed_roles}")
            check(not any(c[:1] == ["git"] for c in runner.calls), f"{label}: before any git: {runner.calls}")
            restore()
            check(project_worktrees.control_repository_origin_mismatch(config) == "", f"restored, it is accepted ({label})")
        check(_origin_of(control) == str(canonical), "the store's remote was never touched")


def test_as_root_membership_is_read_by_descriptor_and_compared_as_written() -> None:
    """The root branch (SYRD-630's reads) of the same proof: the owner's reads stood in by no-follow ones."""
    from dataclasses import replace
    from scripts import owner_writes

    with tempfile.TemporaryDirectory(prefix="syrd715-root.") as raw:
        tmp = Path(raw)
        checkout, control, canonical, config = _following_canonical(tmp)
        config = replace(config, run_as_user="owner715")
        seen: list[str] = []

        def status(owner, path):
            seen.append(f"status {path}")
            for parent in Path(path).parents:  # a link above it is refused, as the descriptor walk refuses it
                if parent.is_symlink():
                    raise SystemExit(f"refusing {path}: {parent} is a link")
            try:
                return os.lstat(path)
            except FileNotFoundError:
                return None

        def read(owner, path):
            seen.append(f"read {path}")
            if status(owner, path) is None:
                return None
            if Path(path).is_symlink():
                raise SystemExit(f"refusing {path}: it is a link")
            return Path(path).read_bytes()

        saved = (team_launcher.root_writes_for, owner_writes.owner_entry_status, owner_writes.read_owner_bytes)
        team_launcher.root_writes_for = lambda user: user == "owner715"
        owner_writes.owner_entry_status, owner_writes.read_owner_bytes = status, read
        try:
            check(project_worktrees.control_repository_origin_mismatch(config) == "",
                  "as root, a store following the checkout's own remote, with its own worktrees, is this project's")
            check(any(entry.startswith("read ") and entry.endswith("/.git") for entry in seen)
                  and any(entry.startswith("read ") and entry.endswith("/gitdir") for entry in seen),
                  f"each worktree's .git and the store entry's back-pointer were read through the owner's reads: {seen[-6:]}")
            ops = Path(next(r.workdir for r in config.roles if r.role == "ops"))
            good = (ops / ".git").read_text(encoding="utf-8")
            entry = Path(good.split(":", 1)[1].strip())
            for label, make, undo in (
                    ("a .git that is not a gitdir record", lambda: (ops / ".git").write_text("nonsense\n", encoding="utf-8"),
                     lambda: (ops / ".git").write_text(good, encoding="utf-8")),
                    ("a gitdir named through '..'", lambda: (ops / ".git").write_text(
                        f"gitdir: {control}/worktrees/../worktrees/{entry.name}\n", encoding="utf-8"),
                     lambda: (ops / ".git").write_text(good, encoding="utf-8")),
                    ("a store entry naming another worktree back", lambda: (entry / "gitdir").write_text("/elsewhere/.git\n",
                                                                                                         encoding="utf-8"),
                     lambda: (entry / "gitdir").write_text(f"{ops / '.git'}\n", encoding="utf-8")),
                    ("a .git that is a link", lambda: ((ops / ".git").rename(tmp / "moved.git"), (ops / ".git").symlink_to(tmp / "moved.git")),
                     lambda: ((ops / ".git").unlink(), (tmp / "moved.git").rename(ops / ".git")))):
                make()
                said = project_worktrees.control_repository_origin_mismatch(config)
                check("another checkout's commit store" in said, f"as root, {label} proves nothing: {said}")
                undo()
                check(project_worktrees.control_repository_origin_mismatch(config) == "", f"as root, restored ({label})")
            # The owner's reads refuse a link above a worktree; a refusal is no proof either.
            base = Path(str(config.worktree_base))
            base.rename(tmp / "real-worktrees")
            base.symlink_to(tmp / "real-worktrees", target_is_directory=True)
            said = project_worktrees.control_repository_origin_mismatch(config)
            check("another checkout's commit store" in said, f"as root, worktrees reached through a link prove nothing: {said}")
            base.unlink()
            (tmp / "real-worktrees").rename(base)
            check(project_worktrees.control_repository_origin_mismatch(config) == "", "as root, restored (the link)")
        finally:
            team_launcher.root_writes_for, owner_writes.owner_entry_status, owner_writes.read_owner_bytes = saved


def main() -> int:
    saved = team_launcher._control_repository_owner_home
    try:
        for name, value in sorted(globals().items()):
            if name.startswith("test_") and callable(value):
                value()
    finally:
        team_launcher._control_repository_owner_home = saved
    print(f"control_repository_origin_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
