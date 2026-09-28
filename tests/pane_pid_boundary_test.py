#!/usr/bin/env python3
"""SYRD-409: a role pane's process id, against the launcher it came out of.

`tmux_pane_pid_args` and `pane_pid_for_role` moved unchanged into
`scripts/pane_pid.py`, and the launcher re-exports both. This pins what makes
that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; `runner=subprocess.run` is the
  default bound when it is defined; `RoleConfig` is an annotation only.
- **Seam (rule 24):** `pane_pid_for_role` reads the argv builder through the
  launcher when it runs, so a patch there reaches it -- every case below runs
  with it wrapped on the launcher.
- **Readers:** the live runtime, the identity cutover and the tmux live-command
  match still read `launcher.pane_pid_for_role` when they run, as often as
  before; the launcher itself calls neither.
- **Behaviour is the baseline's:** the argv; text output, stdout piped,
  stderr discarded; 0 on any nonzero status; `int(str(stdout).strip())`, with
  only a ValueError answered by 0 -- no stricter validation, and a runner's own
  exception still propagates. `GOLDEN` below was produced by running the
  BASELINE launcher's own functions over the very cases embedded here
  (`gold409.py`), not typed.

Nothing asks tmux or a live process: every runner is a recorder, and the real
`subprocess.run`/`Popen`, `os.kill`, the account lookups and socket
connections are refused for each case.
"""

from __future__ import annotations

import ast
import os
import pwd
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import pane_pid as m  # noqa: E402

CHECKS = 0
MOVED = ("tmux_pane_pid_args", "pane_pid_for_role")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals.
SEAMS = {
    'pane_pid_for_role': {'tmux_pane_pid_args': 1},
}
#: Measured on the baseline launcher: every launcher function that calls one of them (none: the readers are other modules).
DISPATCH = {}
#: The production modules that read `launcher.pane_pid_for_role`, and how often (measured on the baseline).
READERS = {'live_role_runtime': 2, 'role_identity_cutover': 3, 'tmux_session_argv': 1}
#: The BASELINE's own behaviour for the cases below (`gold409.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'a pid': {'answer': 4242, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'surrounding whitespace': {'answer': 17, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'exit 1': {'answer': 0, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'exit 255': {'answer': 0, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'a signal exit': {'answer': 0, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'empty output': {'answer': 0, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'malformed output': {'answer': 0, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'two numbers': {'answer': 0, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'no output at all': {'answer': 0, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'bytes output': {'answer': 0, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'an int output': {'answer': 42, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'a negative value': {'answer': -5, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'zero': {'answer': 0, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'a plus sign': {'answer': 7, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'an underscore': {'answer': 1000, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'a unicode digit': {'answer': 3, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'a float': {'answer': 0, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'a target with spaces': {'answer': 8, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p 409:main.1', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'separator controls around it': {'answer': 42, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'output that cannot be read as text': {'answer': {'raised': 'RuntimeError', 'message': 'this output has no text'}, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'the runner raises OSError': {'answer': {'raised': 'OSError', 'message': 'no such file'}, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'the runner raises FileNotFoundError': {'answer': {'raised': 'FileNotFoundError', 'message': 'not installed'}, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'the runner raises ValueError': {'answer': {'raised': 'ValueError', 'message': 'bad argument'}, 'calls': [['tmux_pane_pid_args', 'ROLE'], ['runner', ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'], {'stderr': 'DEVNULL', 'stdout': 'PIPE', 'text': True}]]},
    'argv': ['tmux', 'display-message', '-p', '-t', 'p409-worker:0.0', '#{pane_pid}'],
}
REACHED: set[str] = set()

# --- the cases, shared verbatim with `gold409.py` (which ran them on the baseline) ------------------------------------
# rc/stdout: what the fake runner answers; raise: what it raises instead; target: the role's pane target.
CASES = {
    "a pid": {"rc": 0, "stdout": "4242\n"},
    "surrounding whitespace": {"rc": 0, "stdout": "  17 \n\n"},
    "exit 1": {"rc": 1, "stdout": "4242\n"},
    "exit 255": {"rc": 255, "stdout": ""},
    "a signal exit": {"rc": -9, "stdout": "4242"},
    "empty output": {"rc": 0, "stdout": ""},
    "malformed output": {"rc": 0, "stdout": "no server running\n"},
    "two numbers": {"rc": 0, "stdout": "12 34\n"},
    "no output at all": {"rc": 0, "stdout": None},
    "bytes output": {"rc": 0, "stdout": b"99\n"},
    "an int output": {"rc": 0, "stdout": 42},
    "a negative value": {"rc": 0, "stdout": "-5\n"},
    "zero": {"rc": 0, "stdout": "0\n"},
    "a plus sign": {"rc": 0, "stdout": "+7\n"},
    "an underscore": {"rc": 0, "stdout": "1_000\n"},
    "a unicode digit": {"rc": 0, "stdout": "٣\n"},
    "a float": {"rc": 0, "stdout": "3.5\n"},
    "a target with spaces": {"rc": 0, "stdout": "8\n", "target": "p 409:main.1"},
    # str.strip() removes \x1c-\x1f, int() alone does not: the strip is not redundant.
    "separator controls around it": {"rc": 0, "stdout": "\x1c42\x1f"},
    # Only a ValueError is answered by 0: output whose text cannot even be made propagates as before.
    "output that cannot be read as text": {"rc": 0, "stdout_raises": "RuntimeError"},
    "the runner raises OSError": {"raise": ("OSError", "no such file")},
    "the runner raises FileNotFoundError": {"raise": ("FileNotFoundError", "not installed")},
    "the runner raises ValueError": {"raise": ("ValueError", "bad argument")},
}
ERRORS = {"OSError": OSError, "FileNotFoundError": FileNotFoundError, "ValueError": ValueError, "RuntimeError": RuntimeError}


class Unreadable:
    """Output whose text raises when it is asked for."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def __str__(self) -> str:
        raise ERRORS[self.kind]("this output has no text")
_PIPE, _DEVNULL = subprocess.PIPE, subprocess.DEVNULL


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """Run one case against `holder.pane_pid_for_role`, the argv builder wrapped on the launcher `t`."""
    calls: list = []
    role = SimpleNamespace(role="worker", target=spec.get("target", "p409-worker:0.0"), tmux_session="p409-worker")

    def norm(value):
        if isinstance(value, (list, tuple)):
            return [norm(v) for v in value]
        if isinstance(value, dict):
            return {str(k): norm(v) for k, v in value.items()}
        for obj, name in ((role, "ROLE"), (_PIPE, "PIPE"), (_DEVNULL, "DEVNULL")):
            if value is obj:
                return name
        return value

    def runner(argv, **kwargs):
        calls.append(["runner", norm(list(argv)), norm(dict(sorted(kwargs.items())))])
        if "raise" in spec:
            kind, text = spec["raise"]
            raise ERRORS[kind](text)
        if "stdout_raises" in spec:
            return SimpleNamespace(returncode=spec["rc"], stdout=Unreadable(spec["stdout_raises"]), stderr="")
        return SimpleNamespace(returncode=spec["rc"], stdout=spec["stdout"], stderr="")

    real = t.tmux_pane_pid_args

    def argv_for(r):
        reached.add("tmux_pane_pid_args")
        calls.append(["tmux_pane_pid_args", norm(r)])
        return real(r)

    t.tmux_pane_pid_args = argv_for
    try:
        answer = holder.pane_pid_for_role(role, runner=runner)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- the baseline's own answer, whatever it raises
        answer = {"raised": type(exc).__name__, "message": str(exc)}
    finally:
        t.tmux_pane_pid_args = real
    return {"answer": answer, "calls": calls}
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


class contained:
    """Spawns, signals, account lookups and connections refused."""

    def __enter__(self) -> None:
        self.parts = [patched(subprocess, run=refuse("subprocess.run"), Popen=refuse("subprocess.Popen")),
                      patched(os, kill=refuse("os.kill")), patched(pwd, getpwnam=refuse("pwd.getpwnam"), getpwuid=refuse("pwd.getpwuid")),
                      patched(socket.socket, connect=refuse("socket.connect"), connect_ex=refuse("socket.connect_ex"))]
        for part in self.parts:
            part.__enter__()

    def __exit__(self, *exc: object) -> None:
        for part in reversed(self.parts):
            part.__exit__(*exc)


def run(spec: dict) -> dict:
    with contained():
        return run_case(t, m, spec, REACHED)


def test_the_guard_itself_refuses_a_spawn_a_signal_a_lookup_and_a_connection() -> None:
    for attempt in (lambda: subprocess.run(["true"]), lambda: subprocess.Popen(["true"]), lambda: os.kill(os.getpid(), 0),
                    lambda: pwd.getpwuid(0), lambda: socket.socket().connect(("127.0.0.1", 9))):
        with contained():
            try:
                attempt()
            except AssertionError as exc:
                refused = " was called: " in str(exc)
            else:
                refused = False
        check(refused, "the guard refuses a spawn, a signal, an account lookup and a connection")


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.pane_pid as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_default() -> None:
    for order in (("scripts.pane_pid", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.pane_pid")):
        result = python("import importlib, inspect, subprocess; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.pane_pid as m; "
                        "p = inspect.signature(m.pane_pid_for_role).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "p['runner'].default is subprocess.run and list(p) == ['role', 'runner'] "
                        "and list(inspect.signature(m.tmux_pane_pid_args).parameters) == ['role'], "
                        "not hasattr(m, 'RoleConfig') and not hasattr(m, 'launcher'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.subprocess is subprocess, "the standard-library name is the module's own")


def test_the_seam_reads_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "pane_pid.py").read_text(encoding="utf-8"))
    for name in MOVED:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        check(imports == (["from scripts import team_launcher as launcher"] if expected else []) and (not expected or ast.unparse(node.body[0]) == imports[0]),
              f"{name}: the launcher imported first thing when it runs, or not at all: {imports}")
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    pid = next(n for n in tree.body if getattr(n, "name", None) == "pane_pid_for_role")
    check([ast.unparse(d) for d in pid.args.kw_defaults if d is not None] == ["subprocess.run"], "the runner default is bound when it is defined")
    check([ast.unparse(h.type) for h in ast.walk(pid) if isinstance(h, ast.ExceptHandler)] == ["ValueError"], "only a ValueError is answered by 0")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import subprocess", "from typing import TYPE_CHECKING, Any, Callable"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import RoleConfig"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")
    check([n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))] == list(MOVED), "the two in the launcher's order, and nothing else")


def test_the_launcher_reexports_both_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.pane_pid"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the two, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"role_pane_declaration", "worktree_ref"} <= defined | exported,
          "the launcher defines neither, and keeps its neighbours, its own or re-exported")
    calls: dict = {}
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef):
            for x in ast.walk(fn):
                if isinstance(x, ast.Call) and ast.unparse(x.func).split(".")[-1] in MOVED:
                    calls.setdefault(fn.name, {}).setdefault(ast.unparse(x.func), 0)
                    calls[fn.name][ast.unparse(x.func)] += 1
    check(calls == DISPATCH, f"the launcher still calls them where it did (nowhere): {calls}")
    for module, count in READERS.items():
        source = ast.parse((ROOT / "scripts" / f"{module}.py").read_text(encoding="utf-8"))
        uses = [ast.unparse(x) for x in ast.walk(source) if (isinstance(x, ast.Attribute) and x.attr in MOVED)
                or (isinstance(x, ast.Name) and x.id in MOVED)]
        check(uses == ["launcher.pane_pid_for_role"] * count, f"{module} reads it through the launcher, as often as before: {uses}")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_answer_is_the_baselines() -> None:
    check(sorted(CASES) == sorted(k for k in GOLDEN if k != "argv"), "every measured case is asserted, and nothing else")
    for label, spec in CASES.items():
        got = run(spec)
        check(got == GOLDEN[label], f"{label}: the baseline's answer and every call in order: {got}")
    check(m.tmux_pane_pid_args(SimpleNamespace(target="p409-worker:0.0")) == GOLDEN["argv"], "the argv")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_default",
             "test_the_seam_reads_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_both_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"pane_pid_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
