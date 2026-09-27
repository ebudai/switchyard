#!/usr/bin/env python3
"""SYRD-361: resolving a tenant's release target, against the launcher it came out of.

`switchyard_bare_repo`, `_parse_ls_remote_head`, `_deploy_ref_remote_branch`,
`git_deploy_ref_ls_remote_args`, `git_deploy_ref_rev_parse_args`,
`_resolve_deploy_ref_readonly`, `_resolve_deploy_ref_from_bare_repo`,
`explicit_source_caches`, `tenant_release_status` and
`installed_release_deploy_target` moved into `scripts/tenant_release_target.py`
unchanged. This pins what makes that safe:

- **No cycle.** At its top the module imports only the standard library and
  the `release_refs` leaf, whose default deploy ref is the launcher's very
  object. The publication boundary's `root_controlled_problems` is still
  imported inside the function that uses it.
- **One set of objects.** The launcher re-exports every name, the very same
  objects whichever module is imported first, so `launcher_checkout`,
  `project_status` and the suites that patch `tenant_release_status` reach
  them.
- **Seams (rule 24).** Every launcher facility these use -- the owner-correct
  git chokepoint and `TenantReleaseStatus` included -- and every name here that
  another definition here reads, is read from the launcher when it runs.
  Nothing they bind is read through it (rule 27).
- **The behaviour is unchanged:**
  - an environment cache must be absolute;
  - deploy refs parse into remote and branch the same way, with the same git
    argv, run through the owner-correct chokepoint in a checkout and directly
    against a bare cache, with the same fallback and error text, and nothing
    fetched;
  - explicit caches keep their order, drop relative entries and expand `~`;
  - the board, owner and home are derived and refused as before, root's mirror
    of the units is preferred and all three units are required;
  - an installed release's marker is asked first, and its refusal is final --
    no cache is consulted;
  - a tenant's configured provenance cache never becomes a deploy source.

Every facility is this test's own fixture or fake, installed before anything
runs. Paths are temporary directories this test creates; the environment
variables read are set and restored here. No git runs, and no root record,
release, grant, account or tenant is touched.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0
MOVED = ("switchyard_bare_repo", "_parse_ls_remote_head", "_deploy_ref_remote_branch", "git_deploy_ref_ls_remote_args",
         "git_deploy_ref_rev_parse_args", "_resolve_deploy_ref_readonly", "_resolve_deploy_ref_from_bare_repo",
         "explicit_source_caches", "tenant_release_status", "installed_release_deploy_target")
OWN = ("os", "re", "subprocess", "Path", "Any", "Callable", "DEFAULT_TENANT_RELEASE_DEPLOY_REF")
SEAMS = {
    "git_deploy_ref_ls_remote_args": {"_deploy_ref_remote_branch": 1},
    "_resolve_deploy_ref_readonly": {"_deploy_ref_remote_branch": 1, "run_owner_correct_git": 2, "git_deploy_ref_ls_remote_args": 2,
                                     "_parse_ls_remote_head": 1, "_proc_failure_reason": 2, "git_deploy_ref_rev_parse_args": 2},
    "_resolve_deploy_ref_from_bare_repo": {"_deploy_ref_remote_branch": 1, "_proc_failure_reason": 1},
    "tenant_release_status": {"_tenant_board_root_from_config_or_plan": 1, "_plan_data_from_config": 1, "current_user_name": 1,
                              "privileged_provision_dir": 1, "switchyard_privileged_provision_root": 1, "_repo_root": 1,
                              "_current_tenant_release": 1, "shared_switchyard_release_for_path": 1,
                              "installed_release_deploy_target": 1, "TenantReleaseStatus": 2, "explicit_source_caches": 1,
                              "switchyard_bare_repo": 1, "_resolve_deploy_ref_from_bare_repo": 1, "_resolve_deploy_ref_readonly": 1},
    "installed_release_deploy_target": {"shared_switchyard_release_for_path": 1},
}
SHA = "a" * 40
OTHER = "b" * 40


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def attempt(call):
    """(result, None) or (None, what it raised) -- a refusal is an answer, and anything else is judged, not a crash."""
    try:
        return call(), None
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- SystemExit is a refusal here, and so is anything a mutant raises
        return None, exc


def judged(function, *args: object, **kwargs: object) -> object:
    """What a call returned, or what it raised, as a value to compare."""
    result, raised = attempt(lambda: function(*args, **kwargs))
    return result if raised is None else raised


class patched:
    """Rebind attributes of one object for one block, as the suites do."""

    def __init__(self, target: object, **values: object) -> None:
        self.target, self.values = target, values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.target, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.target, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.target, name, value)


class environ:
    """Set (or, with None, remove) environment variables for one block, and put them back."""

    def __init__(self, **values: str | None) -> None:
        self.values = values

    def __enter__(self) -> None:
        self.saved = {name: os.environ.get(name) for name in self.values}
        for name, value in self.values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def refuse(label: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{label} must not be reached: {args} {kwargs}")
    return refused


def done(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_only_the_release_refs_leaf_at_import() -> None:
    result = python("import sys, scripts.tenant_release_target as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "['scripts.release_refs']",
          f"it imports on its own and loads only the release_refs leaf: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.tenant_release_target", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.tenant_release_target"),
                  ("scripts.launcher_checkout", "scripts.project_status", "scripts.team_launcher")):
        result = python("import importlib; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.tenant_release_target as m, scripts.release_refs as r; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), all(getattr(t, n) is getattr(m, n) for n in {OWN!r}),"
                        " m.DEFAULT_TENANT_RELEASE_DEPLOY_REF is r.DEFAULT_TENANT_RELEASE_DEPLOY_REF)")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_local_import_and_the_defaults() -> None:
    module = ast.parse((ROOT / "scripts" / "tenant_release_target.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    functions = [n for n in module.body if isinstance(n, ast.FunctionDef)]
    check([f.name for f in functions] == list(MOVED), f"the ten, in baseline order: {[f.name for f in functions]}")
    every = {name for reads in SEAMS.values() for name in reads}
    for f in functions:
        through: dict[str, int] = {}
        for n in ast.walk(f):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
                through[n.attr] = through.get(n.attr, 0) + 1
        annotations = {id(x) for a in [*f.args.args, *f.args.kwonlyargs] if a.annotation for x in ast.walk(a.annotation)}
        annotations |= {id(x) for x in ast.walk(f.returns)} if f.returns else set()
        bare = sorted({n.id for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in every
                       and id(n) not in annotations})
        check(through == SEAMS.get(f.name, {}) and not bare, f"{f.name}: exactly its call-time reads, none bare: {through} {bare}")
    locals_ = sorted(ast.unparse(n) for f in functions for n in ast.walk(f) if isinstance(n, ast.ImportFrom) and n.module != "scripts")
    check(locals_ == ["from scripts.ticket_board.publication_boundary import root_controlled_problems"]
          and not any(isinstance(n, ast.Attribute) and n.attr == "root_controlled_problems" for n in ast.walk(module)),
          f"the trust walk still imported where it is used, never the launcher's: {locals_}")
    from scripts import release_refs, team_launcher, tenant_release_target as m

    status_defaults = m.tenant_release_status.__kwdefaults__
    check(status_defaults["deploy_ref"] is release_refs.DEFAULT_TENANT_RELEASE_DEPLOY_REF is team_launcher.DEFAULT_TENANT_RELEASE_DEPLOY_REF
          and status_defaults["runner"] is subprocess.run
          and m._resolve_deploy_ref_readonly.__kwdefaults__ == {"runner": subprocess.run}
          and m._resolve_deploy_ref_from_bare_repo.__kwdefaults__ == {"runner": subprocess.run}
          and m.installed_release_deploy_target.__kwdefaults__ == {"install_root": None},
          "every default is the object it was: the leaf's deploy ref, subprocess.run, None")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    defined = {n.name for n in launcher.body if isinstance(n, ast.FunctionDef)}
    # SYRD-379 moved the status class to scripts/tenant_release_report.py; the launcher still keeps it as its own
    # name -- defined there, or re-exported by exactly that name -- and it never moved here.
    keeps_status = "TenantReleaseStatus" in {n.name for n in launcher.body if isinstance(n, ast.ClassDef)} or any(
        isinstance(n, ast.ImportFrom) and n.module == "scripts.tenant_release_report"
        and any(a.name == "TenantReleaseStatus" and a.asname is None for a in n.names) for n in launcher.body)
    here = ast.parse((ROOT / "scripts" / "tenant_release_target.py").read_text(encoding="utf-8"))
    check(not defined & set(MOVED) and keeps_status
          and "TenantReleaseStatus" not in {n.name for n in here.body if isinstance(n, ast.ClassDef)},
          f"the launcher defines none of them, and keeps the status class, which is not defined here: {defined & set(MOVED)}")
    exported = [sorted(a.name for a in n.names) for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.tenant_release_target"]
    check(exported == [sorted(MOVED)], f"one explicit re-export of all ten: {exported}")
    for consumer, used in (("launcher_checkout.py", ("_parse_ls_remote_head",)), ("project_status.py", ("tenant_release_status",))):
        tree = ast.parse((ROOT / "scripts" / consumer).read_text(encoding="utf-8"))
        reads = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.attr in used}
        bare = [n for n in ast.walk(tree) if isinstance(n, ast.Name) and n.id in used]
        check(reads == set(used) and not bare, f"{consumer} still reads them through the launcher")


# --- deploy refs and their git ---------------------------------------------------------------------------------------


def test_the_environment_cache_must_be_absolute() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        with environ(SWITCHYARD_BARE_REPO=None):
            check(judged(t.switchyard_bare_repo) is None, "unset: no cache")
        with environ(SWITCHYARD_BARE_REPO="   "):
            check(judged(t.switchyard_bare_repo) is None, "blank: no cache")
        with environ(SWITCHYARD_BARE_REPO=f"  {tmp}/cache.git  "):
            check(judged(t.switchyard_bare_repo) == Path(tmp) / "cache.git", "an absolute path, stripped")
        with environ(SWITCHYARD_BARE_REPO="~/cache.git", HOME=tmp):
            check(judged(t.switchyard_bare_repo) == Path(tmp) / "cache.git", "`~` expanded before it is judged")
        with environ(SWITCHYARD_BARE_REPO="relative/cache.git"):
            _, refused = attempt(t.switchyard_bare_repo)
    check(isinstance(refused, SystemExit)
          and str(refused) == "switchyard: SWITCHYARD_BARE_REPO must be an absolute source-cache path",
          f"a relative cache is refused, not resolved against wherever this runs: {refused!r}")


def test_ref_parsing_and_the_git_argv() -> None:
    from scripts import team_launcher as t

    check(judged(t._parse_ls_remote_head, "") is None and judged(t._parse_ls_remote_head, "\n   \n") is None, "no line: no head")
    check(judged(t._parse_ls_remote_head, f"\n  \n  {SHA}\trefs/heads/main\n{OTHER}\trefs/heads/x\n") == SHA, "the first non-blank line's first field")
    for ref, parsed in (("origin/main", ("origin", "main")), ("origin/feat/x", ("origin", "feat/x")), ("refs/remotes/origin/main", None),
                        ("main", None), ("/main", None), ("origin/", None), (SHA, None)):
        check(judged(t._deploy_ref_remote_branch, ref) == parsed, f"{ref!r} -> {parsed}")
    repo = Path("/fixture/repo")
    check(judged(t.git_deploy_ref_ls_remote_args, repo, "origin/feat/x") == ["git", "-C", "/fixture/repo", "ls-remote", "origin", "refs/heads/feat/x"],
          "ls-remote names the remote and the branch's head ref")
    check(judged(t.git_deploy_ref_rev_parse_args, repo, "v1") == ["git", "-C", "/fixture/repo", "rev-parse", "--verify", "v1^{commit}"],
          "rev-parse verifies the ref as a commit")
    _, refused = attempt(lambda: t.git_deploy_ref_ls_remote_args(repo, "main"))
    check(isinstance(refused, ValueError) and str(refused) == "deploy ref does not name a remote branch: main",
          f"ls-remote argv is refused for a ref with no remote: {refused!r}")
    with patched(t, _deploy_ref_remote_branch=lambda ref: ("seam", "branch")):
        check(judged(t.git_deploy_ref_ls_remote_args, repo, "main")[-2:] == ["seam", "refs/heads/branch"], "the parser is read through the launcher")


def owner_git(results: list[subprocess.CompletedProcess[str]], calls: list[tuple[list[str], dict[str, object]]]):
    def run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if not results:
            raise AssertionError(f"an unexpected git call: {calls}")
        return results.pop(0)
    return run


def test_a_checkout_resolves_through_the_owner_correct_chokepoint() -> None:
    from scripts import team_launcher as t

    repo = Path("/fixture/checkout")
    runner = refuse("the raw runner, past the chokepoint")
    piped = {"runner": runner, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    ls_remote = ["git", "-C", "/fixture/checkout", "ls-remote", "origin", "refs/heads/main"]
    rev_parse = ["git", "-C", "/fixture/checkout", "rev-parse", "--verify", "origin/main^{commit}"]

    calls: list = []
    with patched(t, run_owner_correct_git=owner_git([done(0, f"{SHA}\trefs/heads/main\n")], calls)):
        check(judged(t._resolve_deploy_ref_readonly, repo, "origin/main", runner=runner) == (SHA, "") and calls == [(ls_remote, piped)],
              f"a remote branch: the remote's head, asked once, owner-correct, the runner handed on: {calls}")
    calls = []
    with patched(t, run_owner_correct_git=owner_git([done(0, "  \n"), done(0, f"  {OTHER}\n")], calls)):
        check(judged(t._resolve_deploy_ref_readonly, repo, "origin/main", runner=runner) == (OTHER, "")
              and calls == [(ls_remote, piped), (rev_parse, piped)],
              f"an empty remote answer falls back to the local ref: {calls}")
    calls = []
    with patched(t, run_owner_correct_git=owner_git([done(2, "", "fatal: could not read\n  from remote\n")], calls)):
        check(judged(t._resolve_deploy_ref_readonly, repo, "origin/main", runner=runner)
              == ("", f"`{' '.join(ls_remote)}` failed: fatal: could not read from remote") and len(calls) == 1,
              "a failed ls-remote is the answer, with all of git's reason on one line -- no fallback")
    calls = []
    with patched(t, run_owner_correct_git=owner_git([done(128)], calls)):
        check(judged(t._resolve_deploy_ref_readonly, repo, "origin/main", runner=runner)
              == ("", f"`{' '.join(ls_remote)}` failed: git ls-remote exited 128"), "no stderr: the exit status")
    calls = []
    rev_v1 = ["git", "-C", "/fixture/checkout", "rev-parse", "--verify", "v1^{commit}"]
    with patched(t, run_owner_correct_git=owner_git([done(1, "", "fatal: bad ref\n")], calls)):
        check(judged(t._resolve_deploy_ref_readonly, repo, "v1", runner=runner) == ("", f"`{' '.join(rev_v1)}` failed: fatal: bad ref")
              and calls == [(rev_v1, piped)], f"a local ref: rev-parse only, and its failure named: {calls}")
    calls = []
    with patched(t, run_owner_correct_git=owner_git([done(1)], calls)):
        check(judged(t._resolve_deploy_ref_readonly, repo, "v1", runner=runner) == ("", f"`{' '.join(rev_v1)}` failed: git rev-parse exited 1"),
              "no stderr: the exit status")
    calls = []
    with patched(t, run_owner_correct_git=owner_git([done(0, f"{SHA}\n")], calls),
                 _parse_ls_remote_head=refuse("the head parser"), _proc_failure_reason=refuse("the reason formatter")):
        check(judged(t._resolve_deploy_ref_readonly, repo, "v1", runner=runner) == (SHA, ""), "a local ref's commit, stripped")


def test_a_bare_cache_resolves_its_remote_tracking_ref_directly() -> None:
    from scripts import team_launcher as t

    bare = Path("/fixture/cache.git")
    calls: list = []

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if not results:
            raise AssertionError(f"an unexpected git call: {calls}")
        return results.pop(0)

    piped = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True}
    with patched(t, run_owner_correct_git=refuse("the checkout chokepoint")):
        results = [done(0, f" {SHA} \n")]
        check(judged(t._resolve_deploy_ref_from_bare_repo, bare, "origin/main", runner=runner) == (SHA, "")
              and calls == [(["git", "--git-dir=/fixture/cache.git", "rev-parse", "--verify", "refs/remotes/origin/main^{commit}"], piped)],
              f"a remote branch is the cache's remote-tracking ref, read with --git-dir: {calls}")
        calls.clear()
        results = [done(1, "", "fatal: Needed a single revision\n")]
        check(judged(t._resolve_deploy_ref_from_bare_repo, bare, "v1", runner=runner) == ("", "v1: fatal: Needed a single revision")
              and calls[0][0][-1] == "v1^{commit}", f"any other ref as named, and its failure recorded against it: {calls}")
        results = [done(3)]
        check(judged(t._resolve_deploy_ref_from_bare_repo, bare, "origin/x", runner=runner) == ("", "refs/remotes/origin/x: git rev-parse exited 3"),
              "no stderr: the exit status")


def test_explicit_caches_keep_order_and_drop_relative_entries() -> None:
    from scripts import team_launcher as t

    check(judged(t.explicit_source_caches, None) == () and judged(t.explicit_source_caches, "") == (), "none named: none")
    with tempfile.TemporaryDirectory() as tmp, environ(HOME=tmp):
        named = os.pathsep.join(["  /z/second.git ", "", "  ", "relative.git", "~/home.git", "/a/first.git"])
        check(judged(t.explicit_source_caches, named) == (Path("/z/second.git"), Path(tmp) / "home.git", Path("/a/first.git")),
              f"in the order named, stripped, `~` expanded, relative entries dropped: {judged(t.explicit_source_caches, named)}")


# --- tenant_release_status -------------------------------------------------------------------------------------------


class Tenant:
    """One fixture tenant: a board, an owner, root's mirror of the units, a provision directory, and a checkout."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.board = tmp / "home" / "p361-owner" / "p361-ticketboard-live"
        self.priv_root = tmp / "etc-switchyard"
        self.mirror = self.priv_root / "p361"
        # The provision directory and the checkout are reached through symlinks,
        # so a path that is not resolved is visibly not the resolved one.
        (tmp / "provision-real").mkdir()
        (tmp / "provision").symlink_to(tmp / "provision-real")
        (tmp / "checkout-real").mkdir()
        (tmp / "checkout").symlink_to(tmp / "checkout-real")
        self.config_path = tmp / "provision" / "p361.json"
        self.checkout = tmp / "checkout"
        self.release = tmp / "releases" / "current"
        self.plan: dict[str, object] = {"owner_user": "syrd361-owner", "owner_home": str(tmp / "home" / "p361-owner"),
                                        "commit_git_dir": "/plan/provenance.git", "port": " 8361 ", "socket_path": " /fixture/sock "}
        self.log: list[tuple] = []
        self.shared = False

    def units(self, where: Path, names=("", "-canary", "-notify-listener")) -> None:
        where.mkdir(parents=True, exist_ok=True)
        for name in names:
            (where / f"p361-ticket-board{name}.service").write_text("fixture\n")

    def seams(self, **extra: object) -> dict[str, object]:
        from scripts import team_launcher as t

        def status(**fields: object) -> object:
            self.log.append(("status",))
            return t_class(**fields)

        t_class = t.TenantReleaseStatus
        names = {
            "_tenant_board_root_from_config_or_plan": lambda config, config_path: self.log.append(("board", config_path)) or self.board,
            "_plan_data_from_config": lambda config, config_path: self.log.append(("plan", config_path)) or dict(self.plan),
            "current_user_name": lambda: "syrd361-caller",
            "switchyard_privileged_provision_root": lambda: self.priv_root,
            "privileged_provision_dir": lambda project, root: root / project,
            "_repo_root": lambda: self.checkout,
            "_current_tenant_release": lambda board_root: (self.release, "c" * 40),
            "shared_switchyard_release_for_path": lambda path: self.log.append(("shared?", path)) or (object() if self.shared else None),
            "installed_release_deploy_target": refuse("the installed-release marker"),
            "explicit_source_caches": t.explicit_source_caches,
            "switchyard_bare_repo": lambda: None,
            "_resolve_deploy_ref_from_bare_repo": refuse("a source cache"),
            "_resolve_deploy_ref_readonly": refuse("the checkout"),
            "TenantReleaseStatus": status,
        }
        names.update(extra)
        return names


def status_of(tenant: Tenant, config: object | None = None, **kwargs: object):
    from scripts import team_launcher as t

    config = config or SimpleNamespace(project="p361", run_as_user="")
    kwargs.setdefault("config_path", tenant.config_path)
    return attempt(lambda: t.tenant_release_status(config, **kwargs))


def status_ok(tenant: Tenant, config: object | None = None, **kwargs: object):
    """A status that must come back: a refusal or a crash here is a failed check, not a crash."""
    status, refused = status_of(tenant, config, **kwargs)
    check(refused is None and status is not None, f"a status, not a refusal: {refused!r}")
    return status


def test_no_board_is_no_status_and_owner_and_home_are_derived_or_refused() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        tenant = Tenant(Path(tmp))
        with patched(t, **tenant.seams(_tenant_board_root_from_config_or_plan=lambda config, config_path: None,
                                       _plan_data_from_config=refuse("the plan"))):
            check(status_of(tenant) == (None, None), "no board: no status, and the plan is not read")
        checked = lambda **reads: patched(t, **tenant.seams(_resolve_deploy_ref_readonly=lambda *a, **k: ("", ""), **reads))
        with checked():
            status = status_ok(tenant, SimpleNamespace(project="p361", run_as_user="configured"))
        check(status.owner_user == "syrd361-owner" and status.owner_home == Path(tmp) / "home" / "p361-owner",
              "the plan's owner and home win")
        tenant.plan.update(owner_user="", owner_home="")
        with checked():
            status = status_ok(tenant, SimpleNamespace(project="p361", run_as_user="configured"))
        check(status.owner_user == "configured" and status.owner_home == tenant.board.parent,
              "then the config's run-as user; a live board directory's parent is its owner's home")
        with checked():
            status = status_ok(tenant)
        check(status.owner_user == "syrd361-caller", "then the caller, read through the launcher")
        with checked(current_user_name=lambda: "  "):
            _, refused = status_of(tenant)
        check(str(refused) == "switchyard: cannot determine tenant owner for p361", f"no owner at all is refused: {refused!r}")
        tenant.board = Path(tmp) / "elsewhere" / "board"
        with checked():
            _, refused = status_of(tenant)
        check(str(refused) == (f"switchyard: provision plan for p361 is missing owner_home and it cannot be derived from board_root "
                               f"{tenant.board}"), f"no home and nothing to derive it from is refused: {refused!r}")
        tenant.plan["owner_home"] = "relative/home"
        with checked():
            _, refused = status_of(tenant)
        check(str(refused) == "switchyard: tenant owner_home must be absolute: relative/home", f"a relative home is refused: {refused!r}")
        tenant.plan["owner_home"] = "~/owner"
        with checked(), environ(HOME=tmp):
            status = status_ok(tenant)
        check(status.owner_home == Path(tmp) / "owner", "the plan's home is `~`-expanded")
        tenant.log.clear()
        tenant.board = Path(tmp) / "home" / "p361-owner" / "p361-ticketboard-live"
        with checked(_plan_data_from_config=refuse("the plan"), current_user_name=lambda: "caller"):
            status = status_ok(tenant, config_path=None)
        check(status.owner_user == "caller" and status.provisioned_system_unit is None and status.commit_git_dir == ""
              and status.board_port == "" and status.board_socket == "",
              "no config path: no plan read, no unit looked for, nothing taken from a plan")


def test_roots_mirror_is_preferred_and_all_three_units_are_required() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        tenant = Tenant(Path(tmp))
        provision = tenant.config_path.parent
        seams = tenant.seams(_resolve_deploy_ref_readonly=lambda *a, **k: ("", ""))
        with patched(t, **seams):
            check(status_ok(tenant).provisioned_system_unit is None, "no units anywhere: none")
            tenant.units(provision)
            check(status_ok(tenant).provisioned_system_unit == Path(tmp) / "provision-real" / "p361-ticket-board.service",
                  "only the tenant's provision directory has them: that one, resolved")
            tenant.units(tenant.mirror, names=("", "-canary"))
            check(status_ok(tenant).provisioned_system_unit == Path(tmp) / "provision-real" / "p361-ticket-board.service",
                  "root's mirror missing the listener unit is not a complete install source")
            tenant.units(tenant.mirror)
            check(status_ok(tenant).provisioned_system_unit == tenant.mirror / "p361-ticket-board.service",
                  "root's complete mirror wins over the tenant's copy")
            (provision / "p361-ticket-board-canary.service").unlink()
            (tenant.mirror / "p361-ticket-board-notify-listener.service").unlink()
            check(status_ok(tenant).provisioned_system_unit is None, "neither complete: none")


def test_a_checkout_target_and_the_exact_fields() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        tenant = Tenant(Path(tmp))
        tenant.units(tenant.mirror)
        asked: list = []
        runner = refuse("the runner itself")
        readonly = lambda repo, ref, *, runner: asked.append((repo, ref, runner)) or (SHA, "")
        with patched(t, **tenant.seams(_resolve_deploy_ref_readonly=readonly)):
            status = status_ok(tenant, runner=runner, commit_git_dir="  /arg/cache.git  ")
        check(asked == [(Path(tmp) / "checkout-real", "origin/main", runner)],
              f"no source repo named: the launcher's own checkout, resolved, at the default ref, with the runner: {asked}")
        expected = t.TenantReleaseStatus(
            board_root=tenant.board, owner_user="syrd361-owner", owner_home=Path(tmp) / "home" / "p361-owner",
            provisioned_system_unit=tenant.mirror / "p361-ticket-board.service", commit_git_dir="/arg/cache.git",
            current_release=tenant.release, current_sha="c" * 40, target_sha=SHA, deploy_ref="origin/main",
            source_repo=Path(tmp) / "checkout-real", resolve_error="", clone_source_repo=None,
            board_port="8361", board_socket="/fixture/sock")
        check(status == expected, f"every field: {status}")
        check(("status",) in tenant.log, "the status class is the launcher's, constructed through it")
        check(status.unchanged is False, "the kept class's own property still answers")
        asked.clear()
        named = Path(tmp) / "named-checkout"
        with patched(t, **tenant.seams(_resolve_deploy_ref_readonly=lambda repo, ref, *, runner: asked.append((repo, ref)) or ("", "why"))):
            status = status_ok(tenant, source_repo=named, deploy_ref="v9")
        check(asked == [(named, "v9")] and status.resolve_error == "why" and status.target_sha == "" and status.deploy_ref == "v9"
              and status.commit_git_dir == "/plan/provenance.git" and status.clone_source_repo is None,
              f"a named checkout and ref; no commit-git-dir argument: the plan's, as provenance only: {status}")
        with patched(t, **tenant.seams(_resolve_deploy_ref_readonly=lambda repo, ref, *, runner: ("", ""))):
            check(status_ok(tenant, commit_git_dir="").commit_git_dir == "", "an empty argument is still the argument, not the plan's")


def test_an_installed_release_asks_its_marker_first_and_never_falls_back() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        tenant = Tenant(Path(tmp))
        tenant.shared = True
        release = Path(tmp) / "releases" / SHA
        for answer in ((SHA, ""), ("", "not root-controlled")):
            asked: list = []
            marker = lambda repo, ref: asked.append((repo, ref)) or answer
            with patched(t, **tenant.seams(installed_release_deploy_target=marker, switchyard_bare_repo=refuse("the environment cache"),
                                           explicit_source_caches=refuse("the explicit caches"))):
                with environ(SWITCHYARD_BARE_REPO=str(Path(tmp) / "env.git")):
                    status = status_ok(tenant, source_repo=release, deploy_ref=SHA, commit_git_dir="/named/cache.git")
            check(asked == [(release.resolve(strict=False), SHA)] and status.target_sha == answer[0] and status.resolve_error == answer[1]
                  and status.clone_source_repo is None and status.source_repo == release.resolve(strict=False)
                  and status.commit_git_dir == "/named/cache.git" and status.board_port == "8361" and status.board_socket == "/fixture/sock",
                  f"the marker's answer {answer} is final: no cache consulted, nothing to clone, the rest recorded: {status}")


def test_an_unmarked_release_uses_only_explicitly_named_caches() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        tenant = Tenant(Path(tmp))
        tenant.shared = True
        # Named through a symlink: a cache is cloned from as the path it resolves to.
        (Path(tmp) / "caches-real").mkdir()
        (Path(tmp) / "caches").symlink_to(Path(tmp) / "caches-real")
        named_first, named_second = Path(tmp) / "caches" / "first.git", Path(tmp) / "caches" / "second.git"
        first, second, env_cache = Path(tmp) / "caches-real" / "first.git", Path(tmp) / "caches-real" / "second.git", Path(tmp) / "env.git"
        base = tenant.seams(installed_release_deploy_target=lambda repo, ref: ("", ""))
        runner = refuse("the runner itself")
        tried: list = []

        def caches(answers: dict[Path, tuple[str, str]]):
            def resolve(cache: Path, ref: str, runner: object = None) -> tuple[str, str]:
                tried.append((cache, ref, runner))
                if cache not in answers:
                    raise AssertionError(f"a cache nobody named was tried: {cache}")
                return answers[cache]
            return resolve

        with patched(t, **{**base, "_resolve_deploy_ref_from_bare_repo": caches({first: ("", "no ref"), second: (SHA, ""), env_cache: (OTHER, "")}),
                                   "switchyard_bare_repo": lambda: env_cache}):
            status = status_ok(tenant, runner=runner, commit_git_dir=os.pathsep.join([str(named_first), "rel.git", str(named_second)]))
        check(tried == [(first, "origin/main", runner), (second, "origin/main", runner)] and status.target_sha == SHA
              and status.clone_source_repo == second and status.resolve_error == "",
              f"named caches in order, then the environment's; the first that resolves is cloned from: {tried} {status}")
        tried.clear()
        with patched(t, **{**base, "_resolve_deploy_ref_from_bare_repo": caches({first: ("", "no ref"), env_cache: ("", "empty")}),
                                   "switchyard_bare_repo": lambda: env_cache}):
            status = status_ok(tenant, commit_git_dir=str(named_first))
        check([c for c, _, _ in tried] == [first, env_cache] and status.target_sha == "" and status.clone_source_repo is None
              and status.resolve_error == f"{first}: no ref; {env_cache}: empty",
              f"every failure kept, in order: {status.resolve_error}")
        tried.clear()
        with patched(t, **{**base, "_resolve_deploy_ref_from_bare_repo": caches({})}):
            status = status_ok(tenant)
        check(tried == [] and status.target_sha == "" and status.commit_git_dir == "/plan/provenance.git" and status.resolve_error == (
            "installed shared releases require an explicit source cache in --commit-git-dir "
            "or SWITCHYARD_BARE_REPO, or an explicit --source-repo checkout"),
              f"the plan's provenance cache is never a deploy source: {status}")


# --- installed_release_deploy_target ---------------------------------------------------------------------------------


def test_the_marker_answers_only_an_exact_sha_and_refuses_rather_than_guesses() -> None:
    from scripts import team_launcher as t
    from scripts.ticket_board import publication_boundary

    walked: list = []
    walk = lambda path, *, expect_uid, base: walked.append((path, expect_uid, base)) or list(problems)
    problems: list[str] = []
    release = Path("/fixture/releases/one")
    with patched(publication_boundary, root_controlled_problems=walk):
        with patched(t, shared_switchyard_release_for_path=refuse("the release lookup")):
            check(judged(t.installed_release_deploy_target, release, "origin/main") == ("", "")
                  and judged(t.installed_release_deploy_target, release, "a" * 39) == ("", ""),
                  "a symbolic or short ref is not answered here, and nothing is looked up")
        seen: list = []
        with patched(t, shared_switchyard_release_for_path=lambda path, install_root=None: seen.append((path, install_root)) or None):
            check(judged(t.installed_release_deploy_target, release, SHA, install_root=Path("/fixture/install")) == ("", "")
                  and seen == [(release, Path("/fixture/install"))] and walked == [],
                  "not an installed release: no answer, and no walk")
        for marker, overridden, answer in (
                (f" {SHA.upper()} ", None, (SHA, "")),
                ("", "/fixture/shared", ("", f"{release} carries no release marker, so there is nothing to say which commit it is; "
                                             "it is not deployed from")),
                (OTHER, None, ("", f"{release} is the installed release for {OTHER}, but this deploy is pinned at {SHA}. Nothing was "
                                   "deployed: deploying one release while naming another is how a board ends up running code nobody selected."))):
            walked.clear()
            with patched(t, shared_switchyard_release_for_path=lambda path, install_root=None: SimpleNamespace(marker_commit=marker)), \
                    environ(SWITCHYARD_SHARED_INSTALL_ROOT=overridden):
                got = judged(t.installed_release_deploy_target, release, f" {SHA.upper()} ")
            expected_walk = [(str(release), os.getuid(), "/fixture/shared")] if overridden else [(str(release), 0, "/")]
            check(got == answer and walked == expected_walk,
                  f"marker {marker!r}: {got}; root's uid and / unless the install root is overridden: {walked}")
        problems[:] = ["group-writable", "owned by 1000"]
        walked.clear()
        with patched(t, shared_switchyard_release_for_path=lambda path, install_root=None: SimpleNamespace(marker_commit=SHA)), \
                environ(SWITCHYARD_SHARED_INSTALL_ROOT="  "):
            got = judged(t.installed_release_deploy_target, release, SHA)
        check(got == ("", f"{release} is named as an installed release but is not root-controlled: group-writable; owned by 1000")
              and walked == [(str(release), 0, "/")],
              f"a release root does not control is refused, even when its marker matches; a blank override is none: {got} {walked}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_only_the_release_refs_leaf_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_local_import_and_the_defaults")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"tenant_release_target_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
