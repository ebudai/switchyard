#!/usr/bin/env python3
"""SYRD-446: the vendor CLI install instructions, against the launcher they came out of.

`host_wide_install_instruction` and `_missing_cli_install_clause` -- how to
make a missing agent CLI host-wide, and the one-line remedy a missing-CLI
report prints -- moved unchanged into `scripts/cli_install_instructions.py`;
the launcher re-exports both. They only format text: switchyard never fetches
or runs a vendor installer (PGU-904, SYRD-210). This pins what makes the move
safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module imports nothing at load, never the launcher.
- **Seams (rule 24):** the install-command table stays on the launcher and is
  read through it when they run, once per call, so a patch there reaches it:
  every case below reads it through a recording copy there, and some rebind
  or empty it there.
- **Readers:** `agent_cli_promotion.py`, `first_run_auth.py`,
  `first_run_setup.py` and `worker_pool_command.py` read them through the
  launcher when they run, as often as before.
- **The behaviour is the baseline's:** every known CLI, an unknown one, empty,
  padded, capitalised and non-string names, and the table rebound or emptied.
  `GOLDEN` below was produced by running the BASELINE launcher's own
  definitions over the very cases embedded here (`gold446.py`), not typed; it
  is byte-identical under `env -i`, in a normal role pane, with another HOME,
  USER and COLUMNS, under umask 077 and under several hash seeds. The vendor
  install commands are recorded by name, so this file holds no installer
  string.

Nothing is fetched, run or installed. Spawns, every exec, signals, account and
group lookups and socket connections are refused for each case.
"""

from __future__ import annotations

import ast
import grp
import json
import os
import pwd
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import cli_install_instructions as m  # noqa: E402

CHECKS = 0
MOVED = ('host_wide_install_instruction', '_missing_cli_install_clause')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'host_wide_install_instruction': {'AGENT_CLI_INSTALL_COMMANDS': 1},
    '_missing_cli_install_clause': {'AGENT_CLI_INSTALL_COMMANDS': 1},
}
#: Measured on the baseline launcher: every launcher definition outside the two that names them, and how often (none).
DISPATCH = {}
#: Measured on the baseline, by AST: every production module that reads them through the launcher, and how often.
READERS = {'scripts/agent_cli_promotion.py': {'launcher.host_wide_install_instruction': 1}, 'scripts/first_run_auth.py': {'launcher._missing_cli_install_clause': 1}, 'scripts/first_run_setup.py': {'launcher._missing_cli_install_clause': 1}, 'scripts/worker_pool_command.py': {'launcher._missing_cli_install_clause': 1}}
#: The BASELINE's own behaviour for the cases below (`gold446.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'host-wide: claude': {'result': 'install claude host-wide: run the vendor installer under a throwaway HOME so nothing it writes becomes shared, then move only the executable to /usr/local/bin owned by root, mode 0755. No configuration, token or session file may travel with it -- credentials stay in the owner account that authenticates', 'type': 'str', 'reads': [['claude', '']]},
    'host-wide: codex': {'result': 'install codex host-wide: run the vendor installer under a throwaway HOME so nothing it writes becomes shared, then move only the executable to /usr/local/bin owned by root, mode 0755. No configuration, token or session file may travel with it -- credentials stay in the owner account that authenticates', 'type': 'str', 'reads': [['codex', '']]},
    'host-wide: agy': {'result': 'install agy host-wide: run the vendor installer under a throwaway HOME so nothing it writes becomes shared, then move only the executable to /usr/local/bin owned by root, mode 0755. No configuration, token or session file may travel with it -- credentials stay in the owner account that authenticates', 'type': 'str', 'reads': [['agy', '']]},
    'host-wide: hermes': {'result': 'install hermes host-wide: run the vendor installer under a throwaway HOME so nothing it writes becomes shared, then move only the executable to /usr/local/bin owned by root, mode 0755. No configuration, token or session file may travel with it -- credentials stay in the owner account that authenticates', 'type': 'str', 'reads': [['hermes', '']]},
    'host-wide: an unknown CLI': {'result': "install some-future-cli with that vendor's own installer, then place the executable in /usr/local/bin owned by root, mode 0755, so every tenant resolves it", 'type': 'str', 'reads': [['some-future-cli', '']]},
    'host-wide: an empty name': {'result': "install  with that vendor's own installer, then place the executable in /usr/local/bin owned by root, mode 0755, so every tenant resolves it", 'type': 'str', 'reads': [['', '']]},
    'host-wide: a padded name': {'result': "install  claude  with that vendor's own installer, then place the executable in /usr/local/bin owned by root, mode 0755, so every tenant resolves it", 'type': 'str', 'reads': [[' claude ', '']]},
    'host-wide: a name in capitals': {'result': "install Claude with that vendor's own installer, then place the executable in /usr/local/bin owned by root, mode 0755, so every tenant resolves it", 'type': 'str', 'reads': [['Claude', '']]},
    'clause: claude': {'result': 'install claude host-wide with <install command for claude>, or let switchyard promote a copy you already have when it offers', 'type': 'str', 'reads': [['claude', '']]},
    'clause: codex': {'result': 'install codex host-wide with <install command for codex>, or let switchyard promote a copy you already have when it offers', 'type': 'str', 'reads': [['codex', '']]},
    'clause: agy': {'result': 'install agy host-wide with <install command for agy>, or let switchyard promote a copy you already have when it offers', 'type': 'str', 'reads': [['agy', '']]},
    'clause: hermes': {'result': 'install hermes host-wide with <install command for hermes>, or let switchyard promote a copy you already have when it offers', 'type': 'str', 'reads': [['hermes', '']]},
    'clause: an unknown CLI': {'result': "install some-future-cli host-wide with that vendor's own installer, or let switchyard promote a copy you already have when it offers", 'type': 'str', 'reads': [['some-future-cli', '']]},
    'clause: an empty name': {'result': "install  host-wide with that vendor's own installer, or let switchyard promote a copy you already have when it offers", 'type': 'str', 'reads': [['', '']]},
    'clause: a padded name': {'result': "install  claude  host-wide with that vendor's own installer, or let switchyard promote a copy you already have when it offers", 'type': 'str', 'reads': [[' claude ', '']]},
    'clause: a name in capitals': {'result': "install Claude host-wide with that vendor's own installer, or let switchyard promote a copy you already have when it offers", 'type': 'str', 'reads': [['Claude', '']]},
    'host-wide: the table rebound on the launcher': {'result': 'install claude host-wide: run the vendor installer under a throwaway HOME so nothing it writes becomes shared, then move only the executable to /usr/local/bin owned by root, mode 0755. No configuration, token or session file may travel with it -- credentials stay in the owner account that authenticates', 'type': 'str', 'reads': [['claude', '']]},
    'host-wide: the table emptied on the launcher': {'result': "install claude with that vendor's own installer, then place the executable in /usr/local/bin owned by root, mode 0755, so every tenant resolves it", 'type': 'str', 'reads': [['claude', '']]},
    'clause: the table rebound on the launcher': {'result': 'install claude host-wide with syrd446 install claude, or let switchyard promote a copy you already have when it offers', 'type': 'str', 'reads': [['claude', '']]},
    'clause: the table emptied on the launcher': {'result': "install codex host-wide with that vendor's own installer, or let switchyard promote a copy you already have when it offers", 'type': 'str', 'reads': [['codex', '']]},
    'host-wide: a name that is not a string': {'result': "install None with that vendor's own installer, then place the executable in /usr/local/bin owned by root, mode 0755, so every tenant resolves it", 'type': 'str', 'reads': [[None, '']]},
    'clause: a name that is not a string': {'result': "install 7 host-wide with that vendor's own installer, or let switchyard promote a copy you already have when it offers", 'type': 'str', 'reads': [[7, '']]},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold446.py` (which ran them on the baseline) ------------------------------------
# A case asks for the host-wide install instruction or the missing-CLI clause for one CLI name. Both only format text;
# nothing is fetched, run or installed. The vendor install commands are recorded by name, never copied into the record,
# so this file holds no installer string. The install-command table may be rebound on the launcher, where both read it
# when they run. Recorded: the answer, or the exact exception.
NAMES = {
    "claude": "claude", "codex": "codex", "agy": "agy", "hermes": "hermes",
    "an unknown CLI": "some-future-cli", "an empty name": "", "a padded name": " claude ", "a name in capitals": "Claude",
}
CASES = {
    **{f"host-wide: {k}": {"call": "host", "cli": v} for k, v in NAMES.items()},
    **{f"clause: {k}": {"call": "clause", "cli": v} for k, v in NAMES.items()},
    "host-wide: the table rebound on the launcher": {"call": "host", "cli": "claude", "table": {"claude": "syrd446 install claude"}},
    "host-wide: the table emptied on the launcher": {"call": "host", "cli": "claude", "table": {}},
    "clause: the table rebound on the launcher": {"call": "clause", "cli": "claude", "table": {"claude": "syrd446 install claude"}},
    "clause: the table emptied on the launcher": {"call": "clause", "cli": "codex", "table": {}},
    "host-wide: a name that is not a string": {"call": "host", "cli": None},
    "clause: a name that is not a string": {"call": "clause", "cli": 7},
}
FUNCTIONS = ("host_wide_install_instruction", "_missing_cli_install_clause")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s definitions; the table read on `t`, the launcher, and recorded when it is read."""
    table = dict(t.AGENT_CLI_INSTALL_COMMANDS)

    def scrub(text):
        for cli, command in table.items():
            text = text.replace(command, f"<install command for {cli}>")
        return text

    class Recorded(dict):
        def get(self, *args):
            reached.add("AGENT_CLI_INSTALL_COMMANDS")
            reads.append(list(args))
            return super().get(*args)

    reads: list = []
    saved = t.AGENT_CLI_INSTALL_COMMANDS
    try:
        t.AGENT_CLI_INSTALL_COMMANDS = Recorded(spec["table"] if "table" in spec else saved)
        try:
            got = getattr(holder, "host_wide_install_instruction" if spec["call"] == "host" else "_missing_cli_install_clause")(spec["cli"])
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
            return {"result": {"raised": type(exc).__name__, "message": scrub(str(exc))}, "reads": reads}
        return {"result": scrub(got) if isinstance(got, str) else repr(got), "type": type(got).__name__, "reads": reads}
    finally:
        t.AGENT_CLI_INSTALL_COMMANDS = saved
# ----------------------------------------------------------------------------------------------------------------------


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


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


def refuse(what: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{what} was called: {args} {kwargs}")
    return refused


EXECS = ("execv", "execve", "execvp", "execvpe", "execl", "execle", "execlp", "execlpe")


class contained:
    """Spawns, every exec, signals, connections and real account/group lookups refused."""

    def __enter__(self) -> None:
        self.parts = [patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen")),
                      patched(os, kill=refuse("os.kill"), system=refuse("os.system"), **{name: refuse(f"os.{name}") for name in EXECS}),
                      patched(pwd, getpwnam=refuse("pwd.getpwnam"), getpwuid=refuse("pwd.getpwuid")),
                      patched(grp, getgrgid=refuse("grp.getgrgid"), getgrnam=refuse("grp.getgrnam")),
                      patched(socket.socket, connect=refuse("socket.connect"), connect_ex=refuse("socket.connect_ex"))]
        for part in self.parts:
            part.__enter__()

    def __exit__(self, *exc: object) -> None:
        for part in reversed(self.parts):
            part.__exit__(*exc)


def run(holder: object, spec: dict) -> dict:
    with contained():
        return json.loads(json.dumps(run_case(t, holder, spec, REACHED)))


def test_the_guard_itself_refuses_a_spawn_an_exec_a_lookup_and_a_connection() -> None:
    attempts = [lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"]), lambda: os.kill(os.getpid(), 0), lambda: os.system("true"),
                lambda: pwd.getpwuid(0), lambda: grp.getgrgid(0), lambda: socket.socket().connect(("127.0.0.1", 9)),
                *(lambda name=name: getattr(os, name)("true", ["true"]) for name in EXECS)]
    for attempt in attempts:
        with contained():
            try:
                attempt()
            except AssertionError as exc:
                refused = " was called: " in str(exc)
            else:
                refused = False
        check(refused, "the guard refuses a spawn, every exec, a signal, an account or group lookup and a connection")


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_only_where_its_defaults_come_from() -> None:
    result = python("import sys, scripts.cli_install_instructions as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.cli_install_instructions", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.cli_install_instructions")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.cli_install_instructions as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "[list(inspect.signature(getattr(m, n)).parameters) for n in " + repr(FUNCTIONS) + "], "
                        "sorted(f'{n}.{k}' for n in " + repr(FUNCTIONS) + " for k, v in inspect.signature(getattr(m, n)).parameters.items() "
                        "if v.default is not inspect.Parameter.empty), "
                        "not any(hasattr(m, n) for n in ('launcher', 'team_launcher', 'AGENT_CLI_INSTALL_COMMANDS')))")
        check(result.stdout.strip() == "True [['cli'], ['cli']] [] True",
              f"{' then '.join(order)}: one set of objects, one parameter each, no defaults, and the install table not bound here: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "cli_install_instructions.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name, siblings included, read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        first = 1 if ast.get_docstring(node) is not None else 0
        check(imports == (["from scripts import team_launcher as launcher"] if expected else [])
              and (not expected or ast.unparse(node.body[first]) == imports[0]),
              f"{name}: the launcher imported first thing when it reads one, and nothing else imported: {imports}")
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *[d for d in node.args.kw_defaults if d]]
                if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations"] and tc == [],
          f"nothing imported at load at all: {top} {tc}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the two in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_the_two_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.cli_install_instructions"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the two, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    # Annotated constants count too: the install table is `AGENT_CLI_INSTALL_COMMANDS: dict[str, str] = {...}`.
    defined = ({getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
               | {n.target.id for n in tree.body if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)})
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    seams = {name for reads in SEAMS.values() for name in reads} - set(MOVED)
    check(not defined & set(MOVED) and seams | {"AGENT_CLI_INSTALL_COMMANDS", "PROC_ROOT", "_format_missing_cli_launch_failure", "_owner_user_cli_reminder"} <= defined | exported,
          "the launcher defines none of them, and keeps its caller and every seam they read, its own or re-exported")
    uses: dict = {}
    for fn in tree.body:
        if isinstance(fn, (ast.FunctionDef, ast.ClassDef)):
            for x in ast.walk(fn):
                if isinstance(x, ast.Name) and x.id in MOVED:
                    uses.setdefault(fn.name, {}).setdefault(x.id, 0)
                    uses[fn.name][x.id] += 1
    check(uses == DISPATCH, f"no launcher definition calls them, as before: {uses}")
    past = sorted(ast.unparse(x) for x in ast.walk(tree) if isinstance(x, ast.Attribute) and x.attr in MOVED)
    loose = sorted({x.id for n in tree.body if not isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)) for x in ast.walk(n)
                    if isinstance(x, ast.Name) and x.id in MOVED})
    check(past == [] and loose == [], f"and nothing reaches past the launcher's names, or reads them at module level: {past} {loose}")
    for path, counts in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got: dict[str, int] = {}
        for x in ast.walk(source):
            if isinstance(x, ast.Attribute) and x.attr in MOVED and isinstance(x.value, ast.Name) and x.value.id in ("launcher", "team_launcher"):
                got[ast.unparse(x)] = got.get(ast.unparse(x), 0) + 1
        bare = sorted({x.id for x in ast.walk(source) if isinstance(x, ast.Name) and x.id in MOVED})
        check(dict(sorted(got.items())) == counts and bare == [], f"{path} still reads them through the launcher, as often as before: {got} {bare}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(GOLDEN), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        for holder in (m, t):
            got = run(holder, spec)
            check(got == GOLDEN[label], f"{label} ({holder.__name__}): the baseline's answer, every call in order: {got}")


def test_the_rules_hold_in_the_measured_record() -> None:
    def result(label):
        return GOLDEN[label]["result"]

    known = ("claude", "codex", "agy", "hermes")
    check(all(result(f"host-wide: {c}") == f"install {c} host-wide: run the vendor installer under a throwaway HOME so nothing it writes becomes shared, "
                                             "then move only the executable to /usr/local/bin owned by root, mode 0755. No configuration, token or session "
                                             "file may travel with it -- credentials stay in the owner account that authenticates" for c in known),
          "a known CLI: the host-wide recipe -- a throwaway HOME, only the executable, root-owned 0755 in /usr/local/bin, no credentials with it")
    check(all(result(f"host-wide: {k}") == f"install {c} with that vendor's own installer, then place the executable in /usr/local/bin owned by root, "
                                             "mode 0755, so every tenant resolves it"
              for k, c in (("an unknown CLI", "some-future-cli"), ("an empty name", ""), ("a padded name", " claude "), ("a name in capitals", "Claude"),
                           ("the table emptied on the launcher", "claude"), ("a name that is not a string", "None"))),
          "anything the table does not name exactly -- unknown, empty, padded, capitalised, not a string -- gets the vendor's own installer and the destination")
    check(all(result(f"clause: {c}") == f"install {c} host-wide with <install command for {c}>, or let switchyard promote a copy you already have when it offers"
              for c in known)
          and result("clause: an unknown CLI") == "install some-future-cli host-wide with that vendor's own installer, or let switchyard promote a copy you already have when it offers"
          and result("clause: the table emptied on the launcher").startswith("install codex host-wide with that vendor's own installer"),
          "the clause: the vendor's own command as text for a person to run, else 'that vendor's own installer', and the promotion offer")
    check(result("clause: the table rebound on the launcher") == "install claude host-wide with syrd446 install claude, or let switchyard promote a copy you already have when it offers"
          and result("host-wide: the table rebound on the launcher") == result("host-wide: claude"),
          "the table is the launcher's when they run; the host-wide recipe names no command, only whether one exists")
    check(all(GOLDEN[k]["reads"] == [[CASES[k]["cli"], ""]] for k in GOLDEN) and all(GOLDEN[k]["type"] == "str" for k in GOLDEN),
          "each reads the table exactly once, for the name as given, and always answers text -- never raises")
    check(not any("curl" in json.dumps(v) for v in GOLDEN.values()),
          "and no installer string is in this record: the vendor commands are recorded by name")


def test_every_launcher_seam_is_reached() -> None:
    # The one name the two read on the launcher is the install table, replaced there by a recording copy for every case.
    check(SEAMS == {"host_wide_install_instruction": {"AGENT_CLI_INSTALL_COMMANDS": 1}, "_missing_cli_install_clause": {"AGENT_CLI_INSTALL_COMMANDS": 1}}
          and REACHED == {"AGENT_CLI_INSTALL_COMMANDS"}, f"the table read through the launcher: {sorted(REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_two_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"cli_install_instructions_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
