#!/usr/bin/env python3
"""SYRD-390: polkit readiness, against the launcher it came out of.

The four polkit constants, the apt archive question, the install command, the
service question and the readiness check moved unchanged into
`scripts/polkit_readiness.py`, and the launcher re-exports them. This pins what
makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the defaults are bound when the
  functions are defined, to `shutil.which`, `subprocess.run` and the query
  timeout, exactly as before.
- **Seams (rule 24):** every name defined here that another definition here
  reads when it runs -- the rules directory, the accepted codes, the restart
  command and the three sibling functions -- is read through the launcher as
  often as before, so a patch or rebind on the launcher reaches it, which this
  test shows for all of them.
- **The behaviour is unchanged:** pacman before apt, the archive asked as the
  installer asks, no command when there is no known manager; pkcheck asked one
  question about this process with a bounded wait, any answer a working
  service, a timeout, an unrunnable client and any other code each with its
  own message; the rules directory before pkexec, the install suggestion or
  its fallback, the restart instruction, and nothing when all is well.

Nothing real is run: every `which` and runner is a recording fake, and
`os.getpid` is a stand-in. No pkcheck, apt-cache, systemctl, sudo or pkexec.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import polkit_readiness as m  # noqa: E402
from scripts import team_launcher as t  # noqa: E402

CHECKS = 0
MOVED = ("POLKIT_RULES_DIR", "POLKIT_ANSWERED_EXIT_CODES", "POLKIT_RESTART_COMMAND", "POLKIT_QUERY_TIMEOUT_SECONDS",
         "_apt_archive_has", "polkit_install_command", "polkit_service_problem", "polkit_readiness_problems")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'polkit_install_command': {'_apt_archive_has': 1},
    'polkit_service_problem': {'POLKIT_ANSWERED_EXIT_CODES': 1},
    'polkit_readiness_problems': {'POLKIT_RESTART_COMMAND': 1, 'POLKIT_RULES_DIR': 1, 'polkit_install_command': 1, 'polkit_service_problem': 1},
}
#: Every seam name a stand-in on the launcher has been shown to reach; checked last.
REACHED: set[str] = set()
PID = 4390
ARCH = {"pacman": "/usr/bin/pacman", "pkexec": "/usr/bin/pkexec", "pkcheck": "/usr/bin/pkcheck"}
DEBIAN = {"apt-get": "/usr/bin/apt-get", "pkexec": "/usr/bin/pkexec", "pkcheck": "/usr/bin/pkcheck"}
PKCHECK = ["/usr/bin/pkcheck", "--action-id", "org.freedesktop.policykit.exec", "--process", str(PID)]


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


class Runner:
    """A recording fake runner: apt-cache answers `apt`, pkcheck answers `pkcheck` (a code, or an exception)."""

    def __init__(self, *, apt: int | BaseException = 0, pkcheck: int | BaseException = 2, stderr: str | None = "") -> None:
        self.apt, self.pkcheck, self.stderr = apt, pkcheck, stderr
        self.calls: list = []

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        answer = self.apt if argv[0] == "apt-cache" else self.pkcheck
        if isinstance(answer, BaseException):
            raise answer
        return subprocess.CompletedProcess(argv, answer, stdout="", stderr=self.stderr)


def which(tools: dict):
    asked: list = []

    def looking(name):
        asked.append(name)
        return tools.get(name)
    looking.asked = asked
    return looking


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.polkit_readiness as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_eager_defaults() -> None:
    for order in (("scripts.polkit_readiness", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.polkit_readiness")):
        result = python("import importlib, inspect, shutil, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.polkit_readiness as m; "
                        "d = lambda f, n: inspect.signature(f).parameters[n].default; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "all(d(f, 'which') is shutil.which and d(f, 'runner') is subprocess.run "
                        "for f in (m.polkit_install_command, m.polkit_service_problem, m.polkit_readiness_problems)) "
                        "and d(m.polkit_service_problem, 'timeout_seconds') is t.POLKIT_QUERY_TIMEOUT_SECONDS == 15.0 "
                        "and d(m.polkit_readiness_problems, 'rules_dir') is None "
                        "and d(m._apt_archive_has, 'runner') is inspect.Parameter.empty, "
                        "d(t.precheck_new_project, 'polkit_problems') is m.polkit_readiness_problems)")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.shutil is shutil and m.subprocess is subprocess and m.Path is Path, "the standard-library names are the module's own")
    check((m.POLKIT_RULES_DIR, m.POLKIT_ANSWERED_EXIT_CODES, m.POLKIT_RESTART_COMMAND, m.POLKIT_QUERY_TIMEOUT_SECONDS)
          == (Path("/etc/polkit-1/rules.d"), frozenset({0, 1, 2, 3}),
              "sudo systemctl unmask polkit.service && sudo systemctl restart polkit.service", 15.0), "the four constants' values")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "polkit_readiness.py").read_text(encoding="utf-8"))
    for name in MOVED:
        if name.isupper():
            assigned = [n for n in tree.body if isinstance(n, ast.Assign) and [ast.unparse(x) for x in n.targets] == [name]]
            check(len(assigned) == 1, f"{name}: one module-level assignment")
            continue
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        if expected:
            check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[1]) == imports[0],
                  f"{name}: the launcher imported once, first thing after the docstring: {imports}")
        else:
            check(imports == [], f"{name}: reads nothing of the launcher's")
        defaults = {id(y) for d in node.args.defaults + [d for d in node.args.kw_defaults if d is not None] for y in ast.walk(d)}
        check(not [x for x in ast.walk(node) if id(x) in defaults and isinstance(x, ast.Attribute) and ast.unparse(x).startswith("launcher.")],
              f"{name}: its defaults are bound when it is defined, never through the launcher")
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in defaults})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(top == ["from __future__ import annotations", "import os", "import shutil", "import subprocess", "from pathlib import Path", "from typing import Any, Callable"],
          f"only the standard library at the top: {top}")
    names = [n.name if isinstance(n, ast.FunctionDef) else ast.unparse(n.targets[0]) for n in tree.body if isinstance(n, (ast.FunctionDef, ast.Assign))]
    check(names == list(MOVED), f"the eight in the launcher's order: {names}")


def test_the_launcher_reexports_the_eight() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.polkit_readiness"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the eight, unaliased")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    check(not defined & set(MOVED) and "precheck_new_project" in defined, "the launcher defines none of them, and keeps its caller")


# --- the install command -------------------------------------------------------------------------------------------


def test_the_archive_is_asked_as_the_installer_asks() -> None:
    for code, expected in ((0, True), (100, False), (1, False)):
        runner = Runner(apt=code)
        check(m._apt_archive_has("pkexec", runner=runner) is expected
              and runner.calls == [(["apt-cache", "show", "--no-all-versions", "pkexec"], {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL})],
              f"apt-cache show --no-all-versions, output discarded; exit {code} is {expected}: {runner.calls}")
    check(judged(m._apt_archive_has, "pkexec", runner=Runner(apt=FileNotFoundError(2, "apt-cache"))) is False, "an archive that cannot be asked has nothing")
    strange = ValueError("not an OSError")
    check(judged(m._apt_archive_has, "pkexec", runner=Runner(apt=strange)) is strange, "anything else propagates")


def test_one_command_an_operator_can_paste() -> None:
    asked: list = []
    real_has = m._apt_archive_has
    with patched(t, _apt_archive_has=seam("_apt_archive_has", lambda package, *, runner: asked.append(package) or real_has(package, runner=runner))):
        both = which({**ARCH, **DEBIAN})
        check(m.polkit_install_command(which=both, runner=Runner()) == "sudo pacman -S --needed polkit" and both.asked == ["pacman"] and asked == [],
              "pacman first, and apt is not asked")
        check(m.polkit_install_command(which=which(DEBIAN), runner=Runner(apt=0)) == "sudo apt-get install -y polkitd pkexec"
              and asked == ["pkexec"], f"apt with pkexec in its archive: {asked}")
        check(m.polkit_install_command(which=which(DEBIAN), runner=Runner(apt=100)) == "sudo apt-get install -y policykit-1",
              "apt without it: the older package")
        none = which({})
        check(m.polkit_install_command(which=none, runner=Runner()) == "" and none.asked == ["pacman", "apt-get"], "no known manager: no command")


# --- the service ---------------------------------------------------------------------------------------------------


def test_one_real_question_through_pkcheck() -> None:
    with patched(os, getpid=lambda: PID):
        runner = Runner(pkcheck=2)
        check(m.polkit_service_problem(which=which(ARCH), runner=runner) == ""
              and runner.calls == [(PKCHECK, {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True, "timeout": 15.0})],
              f"pkcheck about this process, output captured as text, bounded by the default: {runner.calls}")
        runner = Runner()
        m.polkit_service_problem(which=which(ARCH), runner=runner, timeout_seconds=2.5)
        check(runner.calls[0][1]["timeout"] == 2.5, "an explicit bound is used")
        for code in (0, 1, 2, 3):
            check(m.polkit_service_problem(which=which(ARCH), runner=Runner(pkcheck=code)) == "", f"exit {code} is an answer")
        check(m.polkit_service_problem(which=which({}), runner=Runner()) == "pkcheck, polkit's own client, is not on PATH", "no client")
        check(judged(m.polkit_service_problem, which=which(ARCH), runner=Runner(pkcheck=subprocess.TimeoutExpired("pkcheck", 7.5)), timeout_seconds=7.5)
              == "the polkit authority did not answer within 7.5s", "no answer in time")
        check(judged(m.polkit_service_problem, which=which(ARCH), runner=Runner(pkcheck=subprocess.TimeoutExpired("pkcheck", 15.0)))
              == "the polkit authority did not answer within 15s", "the default bound, shown compactly")
        check(judged(m.polkit_service_problem, which=which(ARCH), runner=Runner(pkcheck=PermissionError(13, "Permission denied")))
              == "pkcheck could not be run: [Errno 13] Permission denied", "an unrunnable client")
        check(m.polkit_service_problem(which=which(ARCH), runner=Runner(pkcheck=127, stderr="  Error checking\n  for authorization\t: no service  "))
              == "the polkit authority could not answer an authorization query (pkcheck: Error checking for authorization : no service)",
              "any other code: its stderr, whitespace collapsed")
        for stderr in ("", None, "   \n"):
            check(m.polkit_service_problem(which=which(ARCH), runner=Runner(pkcheck=126, stderr=stderr))
                  == "the polkit authority could not answer an authorization query (pkcheck: exit 126)", f"no stderr ({stderr!r}): the exit code")
        with patched(t, POLKIT_ANSWERED_EXIT_CODES=frozenset({0})):
            check(m.polkit_service_problem(which=which(ARCH), runner=Runner(pkcheck=2)).endswith("(pkcheck: exit 2)"), "the accepted codes are the launcher's")
            REACHED.add("POLKIT_ANSWERED_EXIT_CODES")


# --- readiness -----------------------------------------------------------------------------------------------------


def test_readiness_before_anything_is_created() -> None:
    with tempfile.TemporaryDirectory() as tmp, patched(os, getpid=lambda: PID):
        rules = Path(tmp) / "rules.d"
        asked: list = []
        stand_ins = dict(polkit_install_command=seam("polkit_install_command", lambda **kw: asked.append(("install", sorted(kw))) or "INSTALL-IT"),
                         polkit_service_problem=seam("polkit_service_problem", lambda **kw: asked.append(("service", sorted(kw))) or ""))
        with patched(t, **stand_ins):
            no_tools = which({})
            problems = m.polkit_readiness_problems(rules_dir=rules, which=no_tools, runner=Runner())
            check(problems == [f"polkit is not installed ({rules} does not exist; pkexec is not on PATH). Provisioning installs a polkit "
                               "rule so the project account can restart its own board, and privileged commands run through pkexec. "
                               "Install it, then run this again:\n    INSTALL-IT"]
                  and asked == [("install", ["runner", "which"])] and no_tools.asked == ["pkexec"],
                  f"the directory first, then pkexec; the install command, asked with the same which and runner: {problems} {asked}")
            rules.mkdir()
            asked.clear()
            check(m.polkit_readiness_problems(rules_dir=rules, which=which({}), runner=Runner())
                  == ["polkit is not installed (pkexec is not on PATH). Provisioning installs a polkit rule so the project account can "
                      "restart its own board, and privileged commands run through pkexec. Install it, then run this again:\n    INSTALL-IT"],
                  "only pkexec missing")
            check(m.polkit_readiness_problems(rules_dir=rules, which=which(ARCH), runner=Runner()) == []
                  and asked[-1] == ("service", ["runner", "which"]), f"installed and answering: nothing: {asked}")
        with patched(t, polkit_install_command=lambda **kw: ""):
            check(m.polkit_readiness_problems(rules_dir=Path(tmp) / "absent", which=which(ARCH), runner=Runner())[0].endswith(
                  "Install your distribution's polkit package, then run this again."), "no command: the generic advice, no sudo")
        with patched(t, polkit_service_problem=lambda **kw: "SERVICE-BROKEN"):
            check(m.polkit_readiness_problems(rules_dir=rules, which=which(ARCH), runner=Runner())
                  == ["polkit is installed but not working: SERVICE-BROKEN. The board's deploy rule is enforced by that service. "
                      "Start it, then run this again:\n    sudo systemctl unmask polkit.service && sudo systemctl restart polkit.service"],
                  "a broken service: its problem and the restart command")
            with patched(t, POLKIT_RESTART_COMMAND="RESTART-IT"):
                check(m.polkit_readiness_problems(rules_dir=rules, which=which(ARCH), runner=Runner())[0].endswith("\n    RESTART-IT"),
                      "the restart command is the launcher's")
                REACHED.add("POLKIT_RESTART_COMMAND")
        with patched(t, POLKIT_RULES_DIR=Path(tmp) / "launcher-rules"):
            first = m.polkit_readiness_problems(which=which(ARCH), runner=Runner())
            check(first and f"{Path(tmp) / 'launcher-rules'} does not exist" in first[0], f"no rules_dir given: the launcher's directory, read when it runs: {first}")
            REACHED.add("POLKIT_RULES_DIR")
        with patched(t, POLKIT_RULES_DIR=rules):
            check(m.polkit_readiness_problems(which=which(ARCH), runner=Runner()) == [], "a rebound launcher directory that exists is ready")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_eager_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_eight")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"polkit_readiness_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
