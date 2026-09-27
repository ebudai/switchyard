#!/usr/bin/env python3
"""SYRD-364: pane liveness, against the launcher it came out of.

`PaneLiveness`, `process_start_ticks`, `process_owner_uid`,
`owner_tmux_targets` and `pane_liveness` moved into
`scripts/pane_liveness_checks.py` unchanged. This pins what makes that safe:

- **No cycle.** The module imports only the standard library at its top.
- **One set of objects.** The launcher re-exports every name -- the class
  included, so equality, hashing and construction are unchanged -- whichever
  module is imported first, so `project_status` and resume-provision's
  readiness check reach them.
- **Seams (rule 24).** The caller's account, the two /proc helpers and the
  result class are read from the launcher when they run, so a patch on the
  launcher still intercepts. Nothing they bind is read through it (rule 27).
- **The behaviour is unchanged:**
  - /proc stat is split on the LAST `)`, field index 19, decoded with
    replacement, and anything unreadable or malformed is None;
  - the owner's tmux server is asked with the exact argv -- through sudo only
    for somebody else, `-n` only when not interactive -- with a deadline only
    when one is given, and every failure is said rather than read as empty;
  - a pane is live only on the evidence, in the same order, with the same
    messages.

Every facility is this test's own fixture or fake: /proc is a temporary tree
this test writes, every runner records rather than runs, and the caller's
account is patched. No tmux, sudo or signal runs, and no real process is read.
"""

from __future__ import annotations

import ast
import dataclasses
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
MOVED = ("PaneLiveness", "process_start_ticks", "process_owner_uid", "owner_tmux_targets", "pane_liveness")
OWN = ("subprocess", "dataclass", "Path", "Any", "Callable")
SEAMS = {
    "owner_tmux_targets": {"current_user_name": 1},
    "pane_liveness": {"PaneLiveness": 8, "process_start_ticks": 1, "process_owner_uid": 1},
}
OWNER = "syrd364-owner"
CONFIG = SimpleNamespace(run_as_user=OWNER)
ROLE = SimpleNamespace(role="app", target="p364-app:0.0")
FORMAT = "#{session_name}:#{window_index}.#{pane_index}"
LIST_PANES = ("list-panes", "-a", "-F", FORMAT)


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def judged(function, *args: object, **kwargs: object) -> object:
    """What a call returned, or what it raised, as a value to compare."""
    try:
        return function(*args, **kwargs)
    except AssertionError:
        raise
    except BaseException as exc:  # noqa: BLE001 -- anything a mutant raises is judged, not a crash
        return exc


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


def refuse(label: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{label} must not be reached: {args} {kwargs}")
    return refused


def stat_line(pid: int, comm: str, start: object, *, fields: int = 22) -> str:
    """A /proc/<pid>/stat line: pid, (comm), state, then fields 4.. with 22 (index 19 after the state) the start."""
    rest = [str(n) for n in range(4, fields + 1)]
    if fields >= 22:
        rest[22 - 4] = str(start)
    return f"{pid} ({comm}) S " + " ".join(rest) + "\n"


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.pane_liveness_checks as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.pane_liveness_checks", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.pane_liveness_checks"),
                  ("scripts.project_status", "scripts.team_launcher")):
        result = python("import importlib; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.pane_liveness_checks as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), all(getattr(t, n) is getattr(m, n) for n in {OWN!r}))")
        check(result.stdout.strip() == "True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_class_and_the_defaults() -> None:
    module = ast.parse((ROOT / "scripts" / "pane_liveness_checks.py").read_text(encoding="utf-8"))
    check(not any("team_launcher" in ast.dump(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))),
          "the launcher is never imported at the module's top at run time")
    names = [n.name for n in module.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))]
    check(names == list(MOVED), f"the five, in baseline order: {names}")
    every = {name for reads in SEAMS.values() for name in reads}
    for f in [n for n in module.body if isinstance(n, ast.FunctionDef)]:
        through: dict[str, int] = {}
        for n in ast.walk(f):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher":
                through[n.attr] = through.get(n.attr, 0) + 1
        annotations = {id(x) for x in ast.walk(f.returns)} if f.returns else set()
        bare = sorted({n.id for n in ast.walk(f) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id in every
                       and id(n) not in annotations})
        check(through == SEAMS.get(f.name, {}) and not bare, f"{f.name}: exactly its call-time reads, none bare: {through} {bare}")

    from scripts import pane_liveness_checks as m, team_launcher

    P = m.PaneLiveness
    check(P is team_launcher.PaneLiveness and P.__dataclass_params__.frozen and [f.name for f in dataclasses.fields(P)] == ["role", "live", "why"],
          "one class: frozen, role/live/why")
    check(m.process_start_ticks.__kwdefaults__ == {"proc_root": Path("/proc")} and m.process_owner_uid.__kwdefaults__ == {"proc_root": Path("/proc")}
          and m.pane_liveness.__kwdefaults__ == {"proc_root": Path("/proc")}
          and m.owner_tmux_targets.__kwdefaults__ == {"runner": subprocess.run, "timeout_seconds": None, "interactive": True},
          "the defaults: /proc, subprocess.run, no deadline, interactive")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    defined = {n.name for n in launcher.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    # The readiness check that reads them did not come here. SYRD-365 moved it on
    # to scripts/recovery_readiness.py, which the launcher re-exports.
    readiness_exports = {a.name for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.recovery_readiness"
                         for a in n.names}
    check(not defined & set(MOVED) and "recovery_readiness_problems" in defined | readiness_exports
          and "recovery_readiness_problems" not in {n.name for n in module.body if isinstance(n, ast.FunctionDef)},
          f"the launcher defines none of them, and still has the readiness check that reads them: {defined & set(MOVED)}")
    exported = [sorted(a.name for a in n.names) for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.pane_liveness_checks"]
    check(exported == [sorted(MOVED)], f"one explicit re-export of all five: {exported}")
    status = ast.parse((ROOT / "scripts" / "project_status.py").read_text(encoding="utf-8"))
    reads = sorted(n.attr for n in ast.walk(status) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                   and n.value.id == "launcher" and n.attr in MOVED)
    bare = [n.id for n in ast.walk(status) if isinstance(n, ast.Name) and n.id in MOVED]
    check(reads == ["owner_tmux_targets", "pane_liveness"] and not bare, f"project_status still reads them through the launcher: {reads} {bare}")


# --- the result and /proc -----------------------------------------------------------------------------------------------


def test_the_result_is_frozen_and_compared_by_value() -> None:
    from scripts import team_launcher as t

    a = t.PaneLiveness("app", True, "why")
    check(a == t.PaneLiveness("app", True, "why") and hash(a) == hash(t.PaneLiveness("app", True, "why"))
          and a != t.PaneLiveness("app", False, "why"), "equal and hashable by value")
    check(isinstance(judged(setattr, a, "live", False), dataclasses.FrozenInstanceError), "frozen")


def test_process_start_ticks_reads_field_22_after_the_last_parenthesis() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        proc = Path(tmp)

        def write(pid: int, text: str | bytes) -> None:
            (proc / str(pid)).mkdir(exist_ok=True)
            data = text if isinstance(text, bytes) else text.encode()
            (proc / str(pid) / "stat").write_bytes(data)

        write(101, stat_line(101, "sh", 7101))
        check(judged(t.process_start_ticks, 101, proc_root=proc) == 7101, "the start time, field 22")
        write(102, stat_line(102, "a) (b 1 2 3", 7102))
        check(judged(t.process_start_ticks, 102, proc_root=proc) == 7102, "a comm with spaces and parentheses: split on the LAST `)`")
        write(103, stat_line(103, "x", 7103).replace("(x)", "x"))
        got = judged(t.process_start_ticks, 103, proc_root=proc)
        check(got == 21, f"no parenthesis at all: split on whitespace, so index 19 of 'x S 4 ..' is field 21: {got}")
        write(104, stat_line(104, "short", 1, fields=21))
        check(judged(t.process_start_ticks, 104, proc_root=proc) is None, "too few fields: None")
        write(105, stat_line(105, "nan", "soon"))
        check(judged(t.process_start_ticks, 105, proc_root=proc) is None, "not an integer: None")
        write(106, stat_line(106, "bad@", 7106).encode().replace(b"bad@", b"bad\xff\xfe"))
        check(judged(t.process_start_ticks, 106, proc_root=proc) == 7106, "undecodable bytes are replaced, not fatal")
        check(judged(t.process_start_ticks, 999, proc_root=proc) is None, "no such process: None")


def test_process_owner_uid_is_the_proc_directorys_owner() -> None:
    from scripts import team_launcher as t

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "201").mkdir()
        check(judged(t.process_owner_uid, 201, proc_root=Path(tmp)) == os.getuid(), "the directory's owner")
        check(judged(t.process_owner_uid, 202, proc_root=Path(tmp)) is None, "no such process: None")


# --- the owner's tmux server --------------------------------------------------------------------------------------------


def runner_of(answer, calls: list):
    def run(args, **kwargs):
        calls.append((list(args), kwargs))
        if isinstance(answer, BaseException):
            raise answer
        return answer
    return run


def test_the_owners_tmux_server_is_asked_with_the_exact_argv() -> None:
    from scripts import team_launcher as t

    # The argv the code under test builds, as expected data only: nothing here
    # runs tmux. Each expectation is written inline in the cases below, never
    # assigned, so the tmux-invocation lint does not read it as an argv builder.
    captured = {"capture_output": True, "text": True, "check": False}
    for caller, kwargs, argv, extra in (
            (OWNER, {}, ["tmux", *LIST_PANES], {}),
            ("root", {}, ["sudo", "-u", OWNER, "-H", "tmux", *LIST_PANES], {}),
            ("root", {"interactive": False}, ["sudo", "-n", "-u", OWNER, "-H", "tmux", *LIST_PANES], {}),
            (OWNER, {"interactive": False}, ["tmux", *LIST_PANES], {}),
            ("root", {"timeout_seconds": 2.5}, ["sudo", "-u", OWNER, "-H", "tmux", *LIST_PANES], {"timeout": 2.5}),
            ("root", {"timeout_seconds": 0}, ["sudo", "-u", OWNER, "-H", "tmux", *LIST_PANES], {}),
            ("root", {"timeout_seconds": None}, ["sudo", "-u", OWNER, "-H", "tmux", *LIST_PANES], {})):
        calls: list = []
        with patched(t, current_user_name=lambda: caller):
            got = judged(t.owner_tmux_targets, CONFIG, runner=runner_of(subprocess.CompletedProcess([], 0, " p364-app:0.0 \n\np364-app:0.0\np364-ops:0.1\n", ""), calls), **kwargs)
        check(calls == [(argv, {**captured, **extra})] and got == ({"p364-app:0.0", "p364-ops:0.1"}, ""),
              f"as {caller} with {kwargs}: {calls}; stripped unique targets: {got}")


def test_a_tmux_that_cannot_be_asked_says_so() -> None:
    from scripts import team_launcher as t

    for answer, kwargs, expected in (
            (subprocess.TimeoutExpired(["tmux"], 2.5), {"timeout_seconds": 2.5}, "the owner's tmux server did not answer within 2.5s"),
            (subprocess.TimeoutExpired(["tmux"], 3), {"timeout_seconds": 3.0}, "the owner's tmux server did not answer within 3s"),
            (FileNotFoundError(2, "No such file or directory", "sudo"), {}, "the owner's tmux server could not be asked: [Errno 2] No such file or directory: 'sudo'"),
            # Only the whole output is stripped, so the last line keeps its own indent.
            (subprocess.CompletedProcess([], 1, "out line\n", "first\n  no server running  \n"), {}, "the owner's tmux server could not be asked:   no server running"),
            (subprocess.CompletedProcess([], 1, "one\nlast out\n", ""), {}, "the owner's tmux server could not be asked: last out"),
            (subprocess.CompletedProcess([], 7, "", None), {}, "the owner's tmux server could not be asked: exit 7")):
        with patched(t, current_user_name=lambda: "root"):
            got = judged(t.owner_tmux_targets, CONFIG, runner=runner_of(answer, []), **kwargs)
        check(got == (set(), expected), f"{answer!r}: {got}")
    with patched(t, current_user_name=lambda: "root"):
        got = judged(t.owner_tmux_targets, CONFIG, runner=runner_of(ValueError("not a tmux failure"), []))
    check(isinstance(got, ValueError), f"only a timeout and OSError are answers: {got!r}")


# --- pane liveness ------------------------------------------------------------------------------------------------------


def liveness(assignment: object, *, owner_uid: int | None = 1364, started: int | None = 500, actual_uid: int | None = 1364,
             targets: set[str] | None = None, log: list | None = None):
    from scripts import team_launcher as t

    log = log if log is not None else []
    helpers = dict(process_start_ticks=lambda pid, *, proc_root: log.append(("start", pid, proc_root)) or started,
                   process_owner_uid=lambda pid, *, proc_root: log.append(("uid", pid, proc_root)) or actual_uid)
    with patched(t, **helpers):
        return judged(t.pane_liveness, CONFIG, ROLE, tmux_targets={"p364-app:0.0"} if targets is None else targets,
                      assignments={} if assignment is None else {"app": assignment}, owner_uid=owner_uid, proc_root=Path("/fixture/proc"))


def test_every_liveness_branch_in_order() -> None:
    from scripts import team_launcher as t

    L = t.PaneLiveness
    live = lambda result: isinstance(result, L) and result.live is True
    good = {"actual_target": " p364-app:0.0 ", "process_pid": "42", "process_start_time": 500, "process_uid": 1364}
    log: list = []
    check(liveness(good, targets=set(), log=log) == L("app", False, f"no pane p364-app:0.0 in {OWNER}'s tmux server") and log == [],
          "no tmux target: not live, and nothing else asked")
    check(liveness(None) == L("app", True, "tmux holds p364-app:0.0; the board has no assignment yet")
          and liveness(["not", "a", "dict"]) == L("app", True, "tmux holds p364-app:0.0; the board has no assignment yet"),
          "no assignment (or not a dict): live on the tmux evidence alone")
    check(liveness({**good, "actual_target": "p364-ops:0.0"}) == L("app", False, "the board assigns app to p364-ops:0.0, not p364-app:0.0")
          and liveness({**good, "actual_target": None}) == L("app", False, "the board assigns app to nothing, not p364-app:0.0"),
          "an assignment for another target, or none")
    for pid in (None, 0, -3, "x", [1]):
        check(liveness({**good, "process_pid": pid}) == L("app", False, "the board's assignment for app names no process"), f"pid {pid!r}: no process")
    log = []
    check(liveness(good, started=None, log=log) == L("app", False, "the process 42 the board assigned app is gone")
          and log == [("start", 42, Path("/fixture/proc"))], f"gone, asked by pid with the given /proc: {log}")
    check(liveness({**good, "process_start_time": "499"}) == L("app", False, "process 42 started at 500, not 499: the pid has been reused"),
          "a reused pid")
    check(isinstance(liveness({**good, "process_start_time": "later"}), ValueError), "a malformed start time propagates, as before")
    log = []
    check(live(liveness({**good, "process_start_time": None}, log=log)) and [e[0] for e in log] == ["start", "uid"],
          "no recorded start time: not compared")
    check(live(liveness({**good, "process_start_time": 500.0})) and live(liveness({**good, "process_start_time": "500"})),
          "the recorded start time is compared as an integer: 500.0 and '500' are the same start")
    check(liveness(good, actual_uid=0) == L("app", False, f"process 42 runs as uid 0, not {OWNER}'s 1364"), "somebody else's process")
    check(live(liveness({**good, "process_uid": 0}, owner_uid=1364)), "the kernel-checked owner takes precedence over the recorded uid")
    check(liveness({**good, "process_uid": 0}, owner_uid=None) == L("app", False, f"process 42 runs as uid 1364, not {OWNER}'s 0"),
          "no owner uid: the recorded one is expected")
    check(live(liveness({**good, "process_uid": None}, owner_uid=None, actual_uid=0)), "nothing expected: not compared")
    check(live(liveness(good, actual_uid=None)), "the actual uid unknown: not compared")
    check(liveness(good) == L("app", True, "tmux holds p364-app:0.0 and pid 42 still registered it"), "live, and why")


def test_liveness_reads_its_helpers_and_class_through_the_launcher_end_to_end() -> None:
    from scripts import team_launcher as t

    built: list = []
    with patched(t, process_start_ticks=lambda pid, *, proc_root: 500, process_owner_uid=lambda pid, *, proc_root: 1364,
                 PaneLiveness=lambda *a: built.append(a) or "built"):
        got = judged(t.pane_liveness, CONFIG, ROLE, tmux_targets={"p364-app:0.0"},
                     assignments={"app": {"actual_target": "p364-app:0.0", "process_pid": 42}}, owner_uid=1364)
    check(got == "built" and built == [("app", True, "tmux holds p364-app:0.0 and pid 42 still registered it")], f"the launcher's class: {built}")
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "42").mkdir()
        (Path(tmp) / "42" / "stat").write_text(stat_line(42, "claude", 777))
        with patched(t, current_user_name=refuse("the caller's account")):
            got = judged(t.pane_liveness, CONFIG, ROLE, tmux_targets={"p364-app:0.0"},
                         assignments={"app": {"actual_target": "p364-app:0.0", "process_pid": 42, "process_start_time": 777}},
                         owner_uid=os.getuid(), proc_root=Path(tmp))
    check(got == t.PaneLiveness("app", True, "tmux holds p364-app:0.0 and pid 42 still registered it"),
          f"the real helpers over a fixture /proc agree: {got}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_class_and_the_defaults")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"pane_liveness_checks_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
