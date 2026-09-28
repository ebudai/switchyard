#!/usr/bin/env python3
"""SYRD-395: a tenant's publication-boundary removal and installation, against the launcher they came out of.

`remove_tenant_publication_boundary` and `install_tenant_publication_boundary`
moved unchanged into `scripts/tenant_publication_boundary.py`, and the launcher
re-exports both. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the `runner` and `print_func`
  defaults are bound when each function is defined; `ProjectConfig` is an
  annotation only; the signatures are the baseline's.
- **Seams (rule 24):** the current user, a user's home, the recorded plan and
  the privileged provision root are read through the launcher as often as
  before, so a patch on the launcher reaches each of them, which this test
  shows for all of them. The provisioning and publication helpers are still
  imported when each function runs, so a stand-in on their own modules reaches
  them.
- **The removal:** a dry run only names a rule that exists and never asks who
  is running it; anyone but root is refused before anything is looked at; a
  missing rule is nothing to do; the one `rm -f` of the rule, its output
  captured; a failure's detail bounded; the rule checked gone afterwards; the
  message; nothing beside the rule is touched.
- **The installation:** the owner and the GitHub identity the recorded plan
  selects, the key path, the registration root fallback, exactly what is handed
  to the installer and in what order, the warnings before the report, no report
  on a dry run, and pending artifacts returned as problems.

Everything real is an owned temporary directory. The process runner is always
a recorder, `os.geteuid` is a stand-in, the real `subprocess.run` and the
launcher facilities' own modules are refused, and the installer, the report
and the identity lookup are stand-ins -- nothing is removed outside the
temporary directory, nothing is installed and no home is read.
"""

from __future__ import annotations

import ast
import inspect
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import host_accounts  # noqa: E402
from scripts import privileged_provision_records  # noqa: E402
from scripts import team_launcher as t  # noqa: E402
from scripts import tenant_publication_boundary as m  # noqa: E402
from scripts.ticket_board import project_provision, publication_boundary  # noqa: E402

CHECKS = 0
MOVED = ("remove_tenant_publication_boundary", "install_tenant_publication_boundary")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals.
SEAMS = {
    'install_tenant_publication_boundary': {'_plan_data_from_config': 1, 'current_user_name': 1, 'home_dir_for_user': 1, 'switchyard_privileged_provision_root': 1},
}
#: Measured on the baseline launcher (`ast.unparse` of each definition's arguments and return annotation).
SIGNATURES = {
    'remove_tenant_publication_boundary': ('config: ProjectConfig, *, config_path: Path | None=None, dry_run: bool=False, sudoers_root: Path | None=None, runner: Callable[..., subprocess.CompletedProcess[Any]]=subprocess.run, print_func: Callable[[str], None]=print', 'list[str]'),
    'install_tenant_publication_boundary': ("config: ProjectConfig, *, release, publish_remote: str='', config_path: Path | None=None, dry_run: bool=False, sudoers_root: Path | None=None, registration_root: Path | None=None, runner: Callable[..., subprocess.CompletedProcess[Any]]=subprocess.run, print_func: Callable[[str], None]=print", 'list[str]'),
}
#: Measured on the baseline launcher: the imports each body makes when it runs, in order.
LOCAL_IMPORTS = {
    'remove_tenant_publication_boundary': ['from scripts.ticket_board.project_provision import publish_sudoers_path'],
    'install_tenant_publication_boundary': ['from scripts.ticket_board.project_provision import publish_sudoers_document, publish_sudoers_path', 'from scripts.ticket_board.publication_boundary import install_publication_boundary, report_publication_outcome', 'from scripts.ticket_board.project_provision import owner_github_key_path, resolve_owner_github_identity'],
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
PROJECT = "p395"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def judged(function, *args: object, **kwargs: object) -> object:
    try:
        return function(*args, **kwargs)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- anything a mutant raises is an answer to compare
        return exc


class patched:
    def __init__(self, target: object, **values: object) -> None:
        self.target, self.values = target, values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.target, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.target, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.target, name, value)


def seam(name: str, function):
    def standing_in(*args: object, **kwargs: object) -> object:
        REACHED.add(name)
        return function(*args, **kwargs)
    return standing_in


def refuse(what: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{what} was called: {args} {kwargs}")
    return refused


class contained:
    """No real process, and no launcher facility reached past the launcher: their defining modules refuse."""

    def __enter__(self) -> None:
        self.parts = [patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen")),
                      patched(host_accounts, home_dir_for_user=refuse("host_accounts.home_dir_for_user")),
                      patched(privileged_provision_records,
                              switchyard_privileged_provision_root=refuse("privileged_provision_records.switchyard_privileged_provision_root"))]
        for part in self.parts:
            part.__enter__()

    def __exit__(self, *exc: object) -> None:
        for part in reversed(self.parts):
            part.__exit__(*exc)


def config(owner: str = "p395-owner") -> SimpleNamespace:
    return SimpleNamespace(project=PROJECT, run_as_user=owner)


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.tenant_publication_boundary as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.tenant_publication_boundary", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.tenant_publication_boundary")):
        result = python("import importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.tenant_publication_boundary as m; "
                        f"ps = [inspect.signature(getattr(m, n)).parameters for n in {MOVED!r}]; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "all(p['runner'].default is subprocess.run and p['print_func'].default is print for p in ps), "
                        "not hasattr(m, 'ProjectConfig') and not hasattr(m, 'current_user_name') and not hasattr(m, 'home_dir_for_user'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")
    for name in MOVED:
        parameters = inspect.signature(getattr(m, name)).parameters
        check(parameters["runner"].default is subprocess.run and parameters["print_func"].default is print,
              f"{name}: the runner and print defaults are the ones bound when it was defined")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "tenant_publication_boundary.py").read_text(encoding="utf-8"))
    for name in MOVED:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        check((ast.unparse(node.args), ast.unparse(node.returns)) == SIGNATURES[name], f"{name}: the baseline's signature")
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        if expected:
            check(imports[0] == "from scripts import team_launcher as launcher" and ast.unparse(node.body[first]) == imports[0]
                  and imports[1:] == LOCAL_IMPORTS[name],
                  f"{name}: the launcher imported first thing when it runs, then its own helpers as before: {imports}")
        else:
            check(imports == LOCAL_IMPORTS[name] and ast.unparse(node.body[first]) == imports[0],
                  f"{name}: reads nothing of the launcher's, and imports its helper first thing when it runs: {imports}")
        skip = {id(y) for f in ast.walk(node) if isinstance(f, ast.FunctionDef)
                for part in [f.returns, *(a.annotation for a in f.args.args + f.args.kwonlyargs), *f.args.defaults, *[d for d in f.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import os", "import subprocess", "from pathlib import Path",
                  "from typing import TYPE_CHECKING, Any, Callable"] and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")
    order = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    check(order == list(MOVED), f"the two in the launcher's order, and nothing else: {order}")


def test_the_launcher_reexports_both_above_every_reader() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.tenant_publication_boundary"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the two, unaliased")
    first_def = min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef)))
    check(imports[0].lineno < first_def, "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later
    # slice moves it on (SYRD-397) -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"role_control_accounts", "_recovered_pin_behind_host"} <= defined | exported,
          "the launcher defines neither, and keeps its neighbours, its own or re-exported")
    phases = ast.parse((ROOT / "scripts" / "upgrade_phases.py").read_text(encoding="utf-8"))
    reads = [x for x in ast.walk(phases) if isinstance(x, ast.Attribute) and x.attr in MOVED]
    check(len(reads) == 2 and all(isinstance(x.value, ast.Name) and x.value.id == "launcher" and x.attr == MOVED[0] for x in reads),
          f"the upgrade's tooling phase still reads the removal through the launcher, twice: {[ast.unparse(x) for x in reads]}")


# --- the removal ---------------------------------------------------------------------------------------------------


class Runner:
    """Records each command; answers as told; removes the rule only when told the command worked and did."""

    def __init__(self, returncode: object = 0, stderr: object = "", removes: bool = True, answer: object = None) -> None:
        self.calls: list = []
        self.returncode, self.stderr, self.removes, self.answer = returncode, stderr, removes, answer

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        if self.removes and Path(argv[-1]).is_file():
            Path(argv[-1]).unlink()
        return self.answer if self.answer is not None else SimpleNamespace(returncode=self.returncode, stderr=self.stderr)


def rule_dir(base: Path) -> Path:
    """The sudo rule and, beside it, the root-owned key and grant the removal must leave alone."""
    sudoers = base / "sudoers.d"
    sudoers.mkdir()
    (sudoers / f"48-{PROJECT}-publish").write_text("rule\n")
    (sudoers / f"47-{PROJECT}-control").write_text("control\n")
    kept = base / "etc-switchyard" / "publish"
    kept.mkdir(parents=True)
    (kept / "id_ed25519").write_text("key\n")
    (kept / "grant.json").write_text("{}\n")
    return sudoers


def kept_intact(base: Path) -> bool:
    return (base / "etc-switchyard" / "publish" / "id_ed25519").read_text() == "key\n" \
        and (base / "etc-switchyard" / "publish" / "grant.json").read_text() == "{}\n" \
        and (base / "sudoers.d" / f"47-{PROJECT}-control").read_text() == "control\n"


def test_a_dry_run_only_names_a_rule_that_exists_and_never_asks_who_runs_it() -> None:
    with tempfile.TemporaryDirectory() as tmp, contained():
        base = Path(tmp); sudoers = rule_dir(base); rule = sudoers / f"48-{PROJECT}-publish"
        said: list[str] = []; runner = Runner()
        with patched(os, geteuid=refuse("os.geteuid")):
            answer = m.remove_tenant_publication_boundary(config(), dry_run=True, sudoers_root=sudoers, runner=runner, print_func=said.append)
        check(answer == [] and said == [f"switchyard: would remove {PROJECT}'s publication sudo rule {rule}"] and runner.calls == []
              and rule.exists() and kept_intact(base), f"a dry run names the rule and removes nothing: {answer} {said} {runner.calls}")
        rule.unlink()
        with patched(os, geteuid=refuse("os.geteuid")):
            answer = m.remove_tenant_publication_boundary(config(), dry_run=True, sudoers_root=sudoers, runner=runner, print_func=said.append)
        check(answer == [] and len(said) == 1 and runner.calls == [], f"and says nothing when there is no rule: {answer} {said}")


def test_only_root_removes_it_and_asks_that_first() -> None:
    with tempfile.TemporaryDirectory() as tmp, contained():
        base = Path(tmp); sudoers = rule_dir(base); rule = sudoers / f"48-{PROJECT}-publish"
        said: list[str] = []; runner = Runner()
        for present in (True, False):
            if not present:
                rule.unlink()
            with patched(os, geteuid=lambda: 1000):
                answer = m.remove_tenant_publication_boundary(config(), sudoers_root=sudoers, runner=runner, print_func=said.append)
            check(answer == [f"{rule} can only be removed by root"] and said == [] and runner.calls == [] and rule.exists() == present
                  and kept_intact(base), f"anyone else is refused, whether or not the rule is there (present={present}): {answer}")
        with patched(os, geteuid=lambda: 0):
            answer = m.remove_tenant_publication_boundary(config(), sudoers_root=sudoers, runner=runner, print_func=said.append)
        check(answer == [] and said == [] and runner.calls == [] and kept_intact(base), f"root with no rule has nothing to do: {answer} {said}")


def test_root_removes_the_rule_and_only_the_rule() -> None:
    with tempfile.TemporaryDirectory() as tmp, contained():
        base = Path(tmp); sudoers = rule_dir(base); rule = sudoers / f"48-{PROJECT}-publish"
        said: list[str] = []; runner = Runner()
        with patched(os, geteuid=lambda: 0):
            answer = m.remove_tenant_publication_boundary(config(), sudoers_root=sudoers, runner=runner, print_func=said.append)
        check(runner.calls == [(["rm", "-f", str(rule)], {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True})],
              f"one rm -f of the rule, its output captured as text: {runner.calls}")
        check(answer == [] and not rule.exists() and kept_intact(base)
              and said == [f"switchyard: removed {PROJECT}'s publication sudo rule; implementers publish by "
                           "pushing to the project remote and nothing here runs as root"],
              f"the rule is gone, the key, grant and control rule are not, and it says so: {answer} {said}")


def test_a_failed_or_unfinished_removal_is_a_problem() -> None:
    long = "e" * 400
    cases = [
        (Runner(returncode=1, stderr=f"  {long}\n", removes=False), lambda rule: [f"could not remove {rule}: {long[:300]}"]),
        (Runner(returncode=1, stderr=" \n", removes=False), lambda rule: [f"could not remove {rule}: no output"]),
        (Runner(returncode=2, stderr=None, removes=False), lambda rule: [f"could not remove {rule}: no output"]),
        (Runner(removes=False, answer=SimpleNamespace(stdout="")), lambda rule: [f"could not remove {rule}: no output"]),
        (Runner(returncode=0, removes=False), lambda rule: [f"{rule} is still on disk after removing it"]),
    ]
    for runner, expected in cases:
        with tempfile.TemporaryDirectory() as tmp, contained():
            base = Path(tmp); sudoers = rule_dir(base); rule = sudoers / f"48-{PROJECT}-publish"
            said: list[str] = []
            with patched(os, geteuid=lambda: 0):
                answer = m.remove_tenant_publication_boundary(config(), sudoers_root=sudoers, runner=runner, print_func=said.append)
            check(answer == expected(rule) and said == [] and len(runner.calls) == 1 and rule.exists() and kept_intact(base),
                  f"reported, not announced as removed: {answer} {said}")


def test_the_rule_is_named_by_the_provisioning_helper_when_it_runs() -> None:
    with tempfile.TemporaryDirectory() as tmp, contained():
        base = Path(tmp); sudoers = rule_dir(base)
        elsewhere = base / "elsewhere-rule"; elsewhere.write_text("rule\n")
        asked: list = []
        with patched(project_provision, publish_sudoers_path=lambda project, *, root=None: asked.append((project, root)) or elsewhere), \
                patched(os, geteuid=lambda: 0):
            answer = m.remove_tenant_publication_boundary(config(), sudoers_root=sudoers, runner=Runner(), print_func=lambda line: None)
        check(answer == [] and asked == [(PROJECT, sudoers)] and not elsewhere.exists() and kept_intact(base),
              f"the path comes from project_provision as it is when the removal runs: {asked} {answer}")


# --- the installation ----------------------------------------------------------------------------------------------


class Install:
    """Stand-ins for everything the installation reaches, each recording in one shared order."""

    def __init__(self, *, resolved: bool = True, problems=(), pending=(), home: Path | None = Path("/nonexistent/home/p395-owner"),
                 plan: dict | None = None, provision_root: Path = Path("/nonexistent/syrd395/provision")) -> None:
        self.order: list = []
        self.outcome = SimpleNamespace(problems=list(problems), pending=list(pending))
        self.resolved, self.home, self.plan, self.provision_root = resolved, home, plan or {}, provision_root
        self.installed: dict = {}

    def parts(self) -> list:
        o = self.order
        return [
            patched(t,
                    current_user_name=seam("current_user_name", lambda: o.append(("current user",)) or "p395-current"),
                    home_dir_for_user=seam("home_dir_for_user", lambda user: o.append(("home", user)) or self.home),
                    _plan_data_from_config=seam("_plan_data_from_config", lambda c, path: o.append(("plan", c.project, path)) or self.plan),
                    switchyard_privileged_provision_root=seam("switchyard_privileged_provision_root",
                                                              lambda: o.append(("provision root",)) or self.provision_root)),
            patched(project_provision,
                    publish_sudoers_path=lambda project, *, root=None: o.append(("sudoers path", project, root)) or Path(f"/nonexistent/sudoers.d/48-{project}-publish"),
                    publish_sudoers_document=lambda project, owner: o.append(("sudoers document", project, owner)) or f"document for {owner}",
                    resolve_owner_github_identity=lambda home, **kw: o.append(("identity", home, kw)) or SimpleNamespace(resolved=self.resolved, key_name="deploy-key"),
                    owner_github_key_path=lambda home, *, key_name="": o.append(("key path", home, key_name)) or f"{home}/.ssh/{key_name or 'id_ed25519'}"),
            patched(publication_boundary,
                    install_publication_boundary=lambda **kw: o.append(("install",)) or self.installed.update(kw) or self.outcome,
                    report_publication_outcome=lambda outcome, *, project, print_func: o.append(("report", outcome is self.outcome, project)) or print_func("REPORT")),
        ]

    def run(self, cfg, **kwargs) -> tuple[object, list[str]]:
        said: list[str] = []
        parts = self.parts()
        with contained():
            for part in parts:
                part.__enter__()
            try:
                answer = judged(m.install_tenant_publication_boundary, cfg, print_func=lambda line: said.append(line) or self.order.append(("say", line)), **kwargs)
            finally:
                for part in reversed(parts):
                    part.__exit__(None, None, None)
        return answer, said


def test_the_installer_is_handed_the_owner_the_identity_and_the_roots_in_order() -> None:
    release, runner = object(), refuse("the runner, which only the installer may use")
    plan = {"owner_github_key_name": "deploy-key", "owner_github_host_alias": "github-p395"}
    s = Install(plan=plan)
    answer, said = s.run(config(), release=release, publish_remote="git@github-p395:o/r.git", config_path=Path("/nonexistent/syrd395/p395.json"),
                         sudoers_root=Path("/nonexistent/sudoers.d"), runner=runner)
    home = "/nonexistent/home/p395-owner"
    check(s.order[:-1] == [("sudoers path", PROJECT, Path("/nonexistent/sudoers.d")), ("home", "p395-owner"),
                           ("plan", PROJECT, Path("/nonexistent/syrd395/p395.json")),
                           ("identity", home, {"recorded_key_name": "deploy-key", "recorded_host_alias": "github-p395"}),
                           ("key path", home, "deploy-key"), ("provision root",), ("sudoers document", PROJECT, "p395-owner"), ("install",),
                           ("report", True, PROJECT)]
          and s.order[-1] == ("say", "REPORT"),
          f"the configured owner, its home, the recorded plan, the identity it selects, its key, then the installer and the report: {s.order}")
    check(s.installed == {"project": PROJECT, "release": release, "registration_root": Path("/nonexistent/syrd395/provision"),
                          "sudoers_path": f"/nonexistent/sudoers.d/48-{PROJECT}-publish", "sudoers_document": "document for p395-owner",
                          "declared_remote": "git@github-p395:o/r.git", "shared_identity_file": f"{home}/.ssh/deploy-key",
                          "owner_user": "p395-owner", "dry_run": False, "runner": runner, "print_func": s.installed.get("print_func")}
          and callable(s.installed.get("print_func")), f"exactly what the installer is handed: {s.installed}")
    check(answer == [] and said == ["REPORT"], f"a complete boundary is no problem: {answer} {said}")


def test_the_fallbacks_the_current_user_no_plan_an_unresolved_identity_and_a_given_root() -> None:
    s = Install(resolved=False)
    answer, said = s.run(config(owner=""), release="r", registration_root=Path("/nonexistent/syrd395/given"))
    home = "/nonexistent/home/p395-owner"
    check(s.order[:6] == [("current user",), ("sudoers path", PROJECT, None), ("home", "p395-current"),
                          ("identity", home, {"recorded_key_name": "", "recorded_host_alias": ""}), ("key path", home, ""),
                          ("sudoers document", PROJECT, "p395-current")],
          f"no configured owner means the current user; no config path, no plan; an unresolved identity, the default key; "
          f"a given registration root, no provision root: {s.order}")
    check(s.installed["registration_root"] == Path("/nonexistent/syrd395/given") and s.installed["owner_user"] == "p395-current"
          and s.installed["declared_remote"] == "" and s.installed["shared_identity_file"] == f"{home}/.ssh/id_ed25519",
          f"what the installer is handed then: {s.installed}")


def test_problems_are_warned_before_the_report_and_pending_is_not_success() -> None:
    s = Install(problems=["the key could not be read"], pending=["/nonexistent/known_hosts", "/nonexistent/grant"])
    answer, said = s.run(config(), release="r")
    check(said == ["warning: switchyard: the key could not be read", "REPORT"]
          and [step for step in s.order if step[0] in ("install", "say", "report")]
          == [("install",), ("say", "warning: switchyard: the key could not be read"), ("report", True, PROJECT), ("say", "REPORT")],
          f"the problems are warned after the installer answers and before the report: {s.order}")
    check(answer == ["the key could not be read",
                     f"/nonexistent/known_hosts was not installed, so {PROJECT}'s publication boundary is incomplete",
                     f"/nonexistent/grant was not installed, so {PROJECT}'s publication boundary is incomplete"],
          f"the problems then every pending artifact, as problems the caller records: {answer}")


def test_a_dry_run_is_handed_on_and_reports_nothing() -> None:
    s = Install(problems=["would change the key"], pending=["/nonexistent/known_hosts"])
    answer, said = s.run(config(), release="r", dry_run=True)
    check(s.installed["dry_run"] is True and not any(step[0] == "report" for step in s.order),
          f"the installer is told it is a dry run, and nothing is reported as installed: {s.order}")
    check(said == ["warning: switchyard: would change the key"]
          and answer == ["would change the key", f"/nonexistent/known_hosts was not installed, so {PROJECT}'s publication boundary is incomplete"],
          f"its problems are still warned and returned: {said} {answer}")


def test_an_error_reaches_the_caller() -> None:
    failure = RuntimeError("the installer failed")
    s = Install()
    parts = s.parts()
    with contained():
        for part in parts:
            part.__enter__()
        try:
            with patched(publication_boundary, install_publication_boundary=lambda **kw: (_ for _ in ()).throw(failure)):
                answer = judged(m.install_tenant_publication_boundary, config(), release="r", print_func=refuse("print"))
        finally:
            for part in reversed(parts):
                part.__exit__(None, None, None)
    check(answer is failure and not any(step[0] == "report" for step in s.order), f"it propagates, and nothing is reported: {answer}")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_both_above_every_reader")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"tenant_publication_boundary_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
