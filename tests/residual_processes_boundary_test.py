#!/usr/bin/env python3
"""SYRD-402: residual tenant process discovery and containment, against the launcher they came out of.

`ResidualProcess`, `_process_environ`, `_process_ancestry`,
`process_systemd_unit`, `residual_project_processes` and
`contain_residual_project_processes` moved unchanged into
`scripts/residual_processes.py`, and the launcher re-exports all six. This pins
what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads nothing of Switchyard's; the record is frozen with the
  baseline's fields; the `signaller=os.kill`, `print_func=print`,
  `proc_root=None` and `exclude=()` defaults are the ones bound when each is
  defined.
- **Seams (rule 24):** the project's managed unit names and every sibling
  discovery and containment use are read through the launcher as often as
  before, so a patch there reaches each of them, which this test shows.
- **The selection and the signalling are the baseline's:** the exact slug; a
  caller role required; this process and its ancestors out, the parent read
  after the LAST ')'; managed services and scopes out by the kernel's cgroup
  record; unreadable or raced entries skipped; non-digit entries ignored and
  the rest in name order; an empty command named by its pid; SIGTERM to the
  selected processes only; a vanished process ignored; a refused or failed
  signal reported, bounded -- `GOLDEN` below was produced by running the
  BASELINE launcher's own functions over identical synthetic trees
  (`gold402.py`), not typed.

Nothing is scanned or signalled: every `/proc` is a synthetic tree in an owned
temporary directory, every signaller is a recorder, and the real `os.kill` is
refused for the whole run.
"""

from __future__ import annotations

import ast
import errno
import inspect
import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The launcher first, as a program would: a module that imported it at load would then show up below, not crash here.
from scripts import team_launcher as t  # noqa: E402,I001
from scripts import residual_processes as m  # noqa: E402

CHECKS = 0
MOVED = ("ResidualProcess", "_process_environ", "_process_ancestry", "process_systemd_unit", "residual_project_processes",
         "contain_residual_project_processes")
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals, siblings included.
SEAMS = {
    'residual_project_processes': {'ResidualProcess': 1, '_process_ancestry': 1, '_process_environ': 1, 'managed_unit_names': 1, 'process_systemd_unit': 1},
    'contain_residual_project_processes': {'residual_project_processes': 1},
}
#: The BASELINE's own behaviour for the cases below (`gold402.py`, run on the baseline launcher under the guard).
GOLDEN = {'discover:exact slug only': [[101, 'ME', 'claude --resume', 'ResidualProcess']],
 'discover:a caller role is required': [[113, 'ME', 'claude --resume', 'ResidualProcess']],
 'discover:managed units are out': [[123, 'ME', 'claude --resume', 'ResidualProcess'], [125, 'ME', 'claude --resume', 'ResidualProcess']],
 'discover:this process and its ancestors are out': [[131, 'ME', 'claude --resume', 'ResidualProcess']],
 'discover:unreadable or raced entries': [[142, 'ME', 'claude --resume', 'ResidualProcess']],
 'discover:non-digit entries, sorted by name': [[10, 'ME', 'claude --resume', 'ResidualProcess'],
                                                [100, 'ME', 'claude --resume', 'ResidualProcess'],
                                                [9, 'ME', 'claude --resume', 'ResidualProcess']],
 "discover:the uid is the entry owner's": [[171, 4242, 'claude --resume', 'ResidualProcess'],
                                           [172, 'ME', 'claude --resume', 'ResidualProcess']],
 'discover:an empty command': [[151, 'ME', 'pid 151', 'ResidualProcess']],
 'discover:an explicit exclusion': [[162, 'ME', 'claude --resume', 'ResidualProcess']],
 'contain:stopped': {'problems': [],
                     'sent': [[201, 15], [202, 15]],
                     'said': ['stopped escaped atlas process: 201 '
                              '(xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx)',
                              'stopped escaped atlas process: 202 (claude --long '
                              'yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy)']},
 'contain:already gone': {'problems': [],
                          'sent': [[201, 15], [202, 15]],
                          'said': ['stopped escaped atlas process: 201 '
                                   '(xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx)']},
 'contain:not ours': {'problems': ['atlas process 202 (uid ME) survived its pane and this process may not stop it: claude --long '
                                   'yyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyyy'],
                      'sent': [[201, 15], [202, 15]],
                      'said': ['stopped escaped atlas process: 201 '
                               '(xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx)']},
 'contain:another error': {'problems': ['could not stop atlas process 202: [Errno 5] Input/output error'],
                           'sent': [[201, 15], [202, 15]],
                           'said': ['stopped escaped atlas process: 201 '
                                    '(xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx)']},
 'environ:a value with =': {'A': '1=2', 'B': ''},
 'environ:no separator': {'C': '3'},
 'environ:invalid utf-8': {'D': '��'},
 'environ:empty': {},
 'environ:unreadable': {},
 'unit:system service': 'atlas-ticket-board.service',
 'unit:user scope': 'session-3.scope',
 'unit:v1 lines first': 'cron.service',
 'unit:root only': '',
 'unit:trailing slash': 'a.service',
 'unit:a slice': '',
 'unit:unreadable': '',
 'ancestry:a funny comm': [500, 501],
 'ancestry:a cycle': [510, 511],
 'ancestry:a short tail': [520],
 'ancestry:a non-numeric parent': [530],
 'ancestry:a missing parent': [540, 541],
 'ancestry:a long chain': [600, 601, 602, 603]}
REACHED: set[str] = set()

# --- the synthetic /proc trees, as `gold402.py` builds them ---------------------------------------------------------
PARENT = 3999999  # a synthetic parent of this process, well above pid_max defaults
GRANDPARENT = 3999998
#: Entries whose owner a stat of them reports as someone else (the files are all ours; the uid is what is read).
UIDS = {}
MANAGED = {"atlas-ticket-board.service", "atlas-notify.service"}

def entry(root, pid, *, env=None, role="main", project="atlas", cgroup="/user.slice/user-1.slice/user@1.service/app.slice/tmux-spawn.scope",
          argv=("claude", "--resume"), ppid=1, comm="claude (pane)", missing=(), dir_for=(), uid=None):
    e = Path(root) / str(pid)
    e.mkdir(parents=True)
    if uid is not None:
        UIDS[str(e)] = uid
    environment = dict(env) if env is not None else {"TICKET_BOARD_PROJECT": project, **({"TICKET_BOARD_CALLER_ROLE": role} if role is not None else {})}
    files = {"environ": ("\0".join(f"{k}={v}" for k, v in environment.items()) + "\0").encode(),
             "cmdline": ("\0".join(argv) + ("\0" if argv else "")).encode(),
             "stat": f"{pid} ({comm}) S {ppid} 0 0 0\n".encode(),
             "cgroup": f"1:net_cls:/\n0::{cgroup}\n".encode()}
    for name, data in files.items():
        if name in missing:
            continue
        if name in dir_for:
            (e / name).mkdir()
            continue
        (e / name).write_bytes(data)

TREES = {
    "exact slug only": [dict(pid=101), dict(pid=102, project="atlas-staging"), dict(pid=103, project="atla"), dict(pid=104, project="ATLAS")],
    "a caller role is required": [dict(pid=111, role=None), dict(pid=112, role="  "), dict(pid=113, role="audit")],
    "managed units are out": [dict(pid=121, cgroup="/system.slice/atlas-ticket-board.service"),
                              dict(pid=122, cgroup="/user.slice/user-1.slice/user@1.service/app.slice/atlas-notify.service"),
                              dict(pid=123, cgroup="/user.slice/user-1.slice/user@1.service/app.slice/atlas-other.service"),
                              dict(pid=124, cgroup="/system.slice/atlas-ticket-board.service/"),
                              dict(pid=125, cgroup="/user.slice/user-1.slice/session-3.scope")],
    "this process and its ancestors are out": [dict(pid="SELF", ppid=PARENT, comm="python3 (x) y)"), dict(pid=PARENT, ppid=GRANDPARENT, comm="bash"),
                                                dict(pid=GRANDPARENT, ppid=1, comm="login"), dict(pid=131, ppid="SELF")],
    "unreadable or raced entries": [dict(pid=141, missing=("environ",)), dict(pid=142, missing=("cgroup",)), dict(pid=143, missing=("cmdline",)),
                                    dict(pid=144, dir_for=("cmdline",)), dict(pid=145, dir_for=("environ",))],
    "non-digit entries, sorted by name": [dict(pid=9), dict(pid=100), dict(pid=10), dict(pid="self-dir"), dict(pid="net"), dict(pid="thread-self", full=True)],
    "the uid is the entry owner's": [dict(pid=171, uid=4242), dict(pid=172)],
    "an empty command": [dict(pid=151, argv=())],
    "an explicit exclusion": [dict(pid=161), dict(pid=162)],
}

def build(root, spec):
    me = os.getpid()
    for item in spec:
        item = dict(item)
        pid = item.pop("pid")
        pid = me if pid == "SELF" else pid
        if item.get("ppid") == "SELF":
            item["ppid"] = me
        if isinstance(pid, str) and not item.pop("full", False):
            (Path(root) / pid).mkdir(parents=True)
            continue
        entry(root, pid, **item)

def norm(value, root):
    me = os.getpid()
    if isinstance(value, dict):
        return {k: norm(v, root) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [norm(v, root) for v in value]
    if isinstance(value, str):
        return value.replace(str(root), "<proc>").replace(f"uid {os.getuid()})", "uid ME)").replace(str(me), "SELF")
    if isinstance(value, int) and not isinstance(value, bool):
        return "SELF" if value == me else ("ME" if value == os.getuid() else value)
    return value

SIGNALS = {"stopped": None, "already gone": ProcessLookupError(errno.ESRCH, "No such process"),
           "not ours": PermissionError(errno.EPERM, "Operation not permitted"), "another error": OSError(errno.EIO, "Input/output error")}


def owned_stat(real):
    """Path.stat, except that an entry in UIDS reports that owner."""
    def stat(self, *args, **kwargs):
        result = real(self, *args, **kwargs)
        if str(self) in UIDS:
            fields = list(result[:10]); fields[4] = UIDS[str(self)]
            return os.stat_result(fields)
        return result
    return stat
# --------------------------------------------------------------------------------------------------------------------


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
    """The real signal refused, and every Path read outside the owned temporary root -- the host's process table included."""

    def __init__(self, root: object) -> None:
        self.root = str(Path(root).resolve())

    def __enter__(self) -> None:
        allowed = self.root

        def guarded(name):
            # An entry's owner is read with stat; a synthetic tree reports the owners it was built with (UIDS).
            real = owned_stat(Path.stat) if name == "stat" else getattr(Path, name)

            def call(self, *args, **kwargs):
                resolved = str(Path(os.path.abspath(self)))
                if resolved != allowed and not resolved.startswith(allowed + os.sep):
                    raise AssertionError(f"read outside the owned tree: {name} {self}")
                return real(self, *args, **kwargs)
            return call
        self.parts = [patched(os, kill=refuse("os.kill")),
                      patched(Path, **{name: guarded(name) for name in ("iterdir", "read_bytes", "read_text", "stat")})]
        for part in self.parts:
            part.__enter__()

    def __exit__(self, *exc: object) -> None:
        for part in reversed(self.parts):
            part.__exit__(*exc)


def test_the_guard_itself_refuses_a_read_outside_the_tree_and_a_signal() -> None:
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
        outside = Path(elsewhere) / "stat"
        outside.write_text("1 (x) S 0\n")
        for attempt in (lambda: outside.read_text(), lambda: list(Path(elsewhere).iterdir()), lambda: os.kill(os.getpid(), 0)):
            with contained(tmp):
                try:
                    attempt()
                except AssertionError as exc:
                    refused = "outside the owned tree" in str(exc) or "os.kill was called" in str(exc)
                else:
                    refused = False
            check(refused, "the guard refuses a read outside the owned tree, and a signal")


def discover(label: str, spec, **seams: object) -> object:
    with tempfile.TemporaryDirectory() as tmp:
        build(tmp, spec)
        exclude = (161,) if label == "an explicit exclusion" else ()
        with contained(tmp), patched(t, managed_unit_names=seam("managed_unit_names", lambda config: set(MANAGED)), **seams):
            try:
                found = m.residual_project_processes(SimpleNamespace(project="atlas"), proc_root=Path(tmp), exclude=exclude)
                return json.loads(json.dumps(norm([[p.pid, p.uid, p.command, type(p).__name__] for p in found], tmp)))
            except AssertionError:
                raise
            except BaseException as exc:  # noqa: BLE001 -- whatever a mutant raises is an answer to compare
                return repr(exc)


def contain(error, **seams: object) -> object:
    with tempfile.TemporaryDirectory() as tmp:
        build(tmp, [dict(pid=201, argv=("x" * 200,)), dict(pid=202, argv=("claude", "--long", "y" * 150))])
        sent: list = []
        said: list = []

        def signaller(pid, sig):
            sent.append([pid, int(sig)])
            if error is not None and pid == 202:
                raise error

        with contained(tmp), patched(t, managed_unit_names=lambda config: set(MANAGED), **seams):
            try:
                problems = m.contain_residual_project_processes(SimpleNamespace(project="atlas"), proc_root=Path(tmp), signaller=signaller, print_func=said.append)
            except AssertionError:
                raise
            except BaseException as exc:  # noqa: BLE001
                problems = repr(exc)
        return json.loads(json.dumps(norm({"problems": problems, "sent": sent, "said": said}, tmp)))


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.residual_processes as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_the_defaults() -> None:
    for order in (("scripts.residual_processes", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.residual_processes")):
        result = python("import importlib, inspect, os, dataclasses; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.residual_processes as m; "
                        "c = inspect.signature(m.contain_residual_project_processes).parameters; r = inspect.signature(m.residual_project_processes).parameters; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "c['signaller'].default is os.kill and c['print_func'].default is print and c['proc_root'].default is None "
                        "and r['proc_root'].default is None and r['exclude'].default == () "
                        "and m.ResidualProcess.__dataclass_params__.frozen and [f.name for f in dataclasses.fields(m.ResidualProcess)] == ['pid', 'uid', 'command'], "
                        "not hasattr(m, 'managed_unit_names') and not hasattr(m, 'ProjectConfig') and not hasattr(m, 'stat'))")
        check(result.stdout.strip() == "True True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    check(m.os is os and m.signal is signal and m.Path is Path, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "residual_processes.py").read_text(encoding="utf-8"))
    for name in MOVED:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through: dict[str, int] = {}
        for x in ast.walk(node):
            if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher":
                through[x.attr] = through.get(x.attr, 0) + 1
        expected = SEAMS.get(name, {})
        check(through == expected, f"{name}: each launcher name read through it exactly as often as before: {through}")
        if not isinstance(node, ast.FunctionDef):
            continue
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        if expected:
            first = 1 if ast.get_docstring(node) is not None else 0
            check(imports == ["from scripts import team_launcher as launcher"] and ast.unparse(node.body[first]) == imports[0],
                  f"{name}: the launcher imported once, first thing when it runs: {imports}")
        else:
            check(imports == [], f"{name}: reads nothing of the launcher's and imports nothing: {imports}")
        # Annotations are never evaluated here (`from __future__ import annotations`, and a local's annotation never is):
        # the signature's and those of annotated assignments in the body.
        skip = {id(y) for part in [node.returns, *(a.annotation for a in node.args.args + node.args.kwonlyargs), *node.args.defaults,
                                   *[d for d in node.args.kw_defaults if d],
                                   *(x.annotation for x in ast.walk(node) if isinstance(x, ast.AnnAssign))] if part is not None for y in ast.walk(part)}
        bare = sorted({x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and x.id in expected and id(x) not in skip})
        check(bare == [], f"{name}: none of them read past it: {bare}")
    record = next(n for n in tree.body if getattr(n, "name", None) == "ResidualProcess")
    check([ast.unparse(d) for d in record.decorator_list] == ["dataclass(frozen=True)"], "the record is frozen by the decorator bound when it is defined")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import os", "import signal", "from dataclasses import dataclass", "from pathlib import Path",
                  "from typing import TYPE_CHECKING, Callable, Iterable"] and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"only the standard library at the top, and the annotation's type under TYPE_CHECKING: {top} {tc}")
    order = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    check(order == list(MOVED), f"the six in the launcher's order, and nothing else: {order}")


def test_the_launcher_reexports_the_six_above_every_reader() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.residual_processes"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the six, the private-looking helpers included, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))),
          "at the top, above every definition that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    # A neighbour stays reachable on the launcher: defined there, or -- once a later slice moves it on -- re-exported there, unaliased.
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"_plan_data_from_config", "TENANT_RUNTIME_STATES", "suspend_tenant"} <= defined | exported,
          "the launcher defines none of them, and keeps its neighbours and the suspension, its own or re-exported")
    suspend = next(n for n in tree.body if getattr(n, "name", None) == "suspend_tenant")
    check([ast.unparse(x.func) for x in ast.walk(suspend) if isinstance(x, ast.Call) and ast.unparse(x.func).endswith("contain_residual_project_processes")]
          == ["contain_residual_project_processes"], "the suspension contains through its launcher global")


# --- behaviour -----------------------------------------------------------------------------------------------------


def test_every_discovery_is_the_baselines() -> None:
    check(sorted(f"discover:{k}" for k in TREES) == sorted(k for k in GOLDEN if k.startswith("discover:")), "every measured tree is asserted")
    for label, spec in TREES.items():
        got = discover(label, spec)
        check(got == GOLDEN[f"discover:{label}"], f"{label}: the baseline's selection, in order: {got}")


def test_every_containment_is_the_baselines() -> None:
    check(sorted(f"contain:{k}" for k in SIGNALS) == sorted(k for k in GOLDEN if k.startswith("contain:")), "every measured signal outcome is asserted")
    for label, error in SIGNALS.items():
        got = contain(error)
        check(got == GOLDEN[f"contain:{label}"], f"{label}: the baseline's signals, problems and messages: {got}")
        check(all(sig == int(signal.SIGTERM) for _pid, sig in got["sent"]), f"{label}: SIGTERM only: {got['sent']}")


def test_the_proc_readers_are_the_baselines() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        cases = {"a value with =": b"A=1=2\0B=\0", "no separator": b"JUNK\0C=3\0", "invalid utf-8": b"D=\xff\xfe\0", "empty": b""}
        for label, raw in cases.items():
            (root / label).mkdir()
            (root / label / "environ").write_bytes(raw)
            check(m._process_environ(root / label) == GOLDEN[f"environ:{label}"], f"environ {label}")
        (root / "none").mkdir()
        check(m._process_environ(root / "none") == GOLDEN["environ:unreadable"], "an unreadable environ is empty")
        shapes = {"system service": "0::/system.slice/atlas-ticket-board.service\n", "user scope": "0::/user.slice/user-1.slice/session-3.scope\n",
                  "v1 lines first": "12:pids:/\n1:name=systemd:/system.slice/cron.service\n0::/\n", "root only": "0::/\n", "trailing slash": "0::/system.slice/a.service/\n",
                  "a slice": "0::/user.slice\n"}
        for label, raw in shapes.items():
            (root / ("cg-" + label)).mkdir()
            (root / ("cg-" + label) / "cgroup").write_text(raw)
            check(m.process_systemd_unit(root / ("cg-" + label)) == GOLDEN[f"unit:{label}"], f"unit {label}")
        check(m.process_systemd_unit(root / "none") == GOLDEN["unit:unreadable"], "an unreadable cgroup is no unit")
        chains = {"a funny comm": [(500, "a (b) c)", 501), (501, "bash", 1)], "a cycle": [(510, "x", 511), (511, "y", 510)],
                  "a short tail": [(520, "x", None)], "a non-numeric parent": [(530, "x", "zz")], "a missing parent": [(540, "x", 541)],
                  "a long chain": [(600, "a", 601), (601, "b", 602), (602, "c) d (e", 603), (603, "f", 1)]}
        for label, chain in chains.items():
            proc = root / ("chain-" + label.replace(" ", "-"))
            for pid, comm, ppid in chain:
                (proc / str(pid)).mkdir(parents=True)
                (proc / str(pid) / "stat").write_text(f"{pid} ({comm}) S" + (f" {ppid} 0 0\n" if ppid is not None else "\n"))
            with contained(tmp):
                check(sorted(m._process_ancestry(chain[0][0], proc_root=proc)) == GOLDEN[f"ancestry:{label}"], f"ancestry {label}")


def test_each_sibling_is_read_through_the_launcher() -> None:
    spec = TREES["this process and its ancestors are out"]
    made: list = []

    class Recording(m.ResidualProcess):
        def __init__(self, *args: object) -> None:
            made.append(args[0])
            super().__init__(*args)

    got = discover("this process and its ancestors are out", spec,
                   _process_ancestry=seam("_process_ancestry", m._process_ancestry), _process_environ=seam("_process_environ", m._process_environ),
                   process_systemd_unit=seam("process_systemd_unit", m.process_systemd_unit),
                   ResidualProcess=seam("ResidualProcess", Recording))
    check([row[:3] for row in got] == [row[:3] for row in GOLDEN["discover:this process and its ancestors are out"]]
          and [row[3] for row in got] == ["Recording"] and made == [131],
          f"patched on the launcher, each sibling is the one discovery uses: {got} {made}")
    asked: list = []
    got = contain(None, residual_project_processes=seam("residual_project_processes", lambda config, *, proc_root: asked.append(proc_root) or []))
    check(got == {"problems": [], "sent": [], "said": []} and len(asked) == 1, f"and containment asks the launcher's discovery: {got}")


def test_every_launcher_seam_is_reached() -> None:
    expected = {name for reads in SEAMS.values() for name in reads}
    check(REACHED == expected, f"a stand-in on the launcher reached every seam: missing {sorted(expected - REACHED)}, extra {sorted(REACHED - expected)}")


STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import", "test_either_import_order_gives_one_set_of_objects_and_the_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_six_above_every_reader")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"residual_processes_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
