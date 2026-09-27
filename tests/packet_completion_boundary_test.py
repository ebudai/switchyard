#!/usr/bin/env python3
"""SYRD-363: the privileged packet's completion probes, against the launcher they came out of.

`PacketCompletion`, `_owner_user_unit_args`, `_owner_unit_is_active`,
`_board_answers` and `privileged_packet_completion` moved into
`scripts/packet_completion.py` unchanged. `_system_unit_is_active`, which
`postgres_availability_remedy` also reads, stayed. This pins what makes that
safe:

- **No cycle.** The module imports only the standard library at its top.
- **One set of objects.** The launcher re-exports every name -- the class
  included, so construction, equality and `isinstance` are unchanged --
  whichever module is imported first.
- **Seams (rule 24).** The uid lookup, the shared system-unit probe, the board
  opener, the moved helpers and the result class are read from the launcher
  when they run -- inside the lazy default lambdas too, which still read
  nothing until called. Nothing they bind is read through it (rule 27).
- **The behaviour is unchanged:**
  - the result is frozen, empty by default, and done only when empty;
  - the owner's user manager is driven with the exact sudo/env argv;
  - unit probes pass the exact argv and DEVNULL pipes and treat a missing
    return code as failure;
  - the board answers only if opening, entering and leaving its URL all
    succeed;
  - the five installed artifacts are probed in order, then the two services
    and the board, with no short circuit, and the problems come back in that
    order.

Every facility is this test's own fake. `exists` is always a recording fake --
the verifier's paths are `/etc` and the owner's home -- and so are every runner,
uid lookup and opener, so nothing here reaches systemctl, sudo, a board or a
real file.
"""

from __future__ import annotations

import ast
import dataclasses
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0
MOVED = ("PacketCompletion", "_owner_user_unit_args", "_owner_unit_is_active", "_board_answers", "privileged_packet_completion")
OWN = ("subprocess", "dataclass", "Path", "Any", "Callable")
SEAMS = {
    "_owner_user_unit_args": {"uid_for_user": 1},
    "_owner_unit_is_active": {"_owner_user_unit_args": 1},
    "privileged_packet_completion": {"_system_unit_is_active": 1, "_owner_unit_is_active": 1, "_board_answers": 1,
                                     "_open_board_url": 1, "PacketCompletion": 1},
}
PLAN = SimpleNamespace(owner_user="syrd363-owner", owner_home="/fixture/home/syrd363-owner", board_unit="p363-ticket-board.service",
                       tmpfiles_name="p363.conf", polkit_name="50-p363.rules", listener_unit="p363-notify-listener.service",
                       board_current="/fixture/srv/p363/current", port=8363)
PATHS = [
    (Path("/etc/systemd/system/p363-ticket-board.service"), "the board unit p363-ticket-board.service is not installed"),
    (Path("/etc/tmpfiles.d/p363.conf"), "the tmpfiles configuration p363.conf is not installed"),
    (Path("/etc/polkit-1/rules.d/50-p363.rules"), "the polkit rule 50-p363.rules is not installed"),
    (Path("/fixture/home/syrd363-owner/.config/systemd/user/p363-notify-listener.service"),
     "the listener unit p363-notify-listener.service is not installed for syrd363-owner"),
    (Path("/fixture/srv/p363/current/scripts/ticket-board.py"), "no board release is exported at /fixture/srv/p363/current"),
]


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


#: Every launcher facility the probes could reach for real, refused. A test
#: installs a fake for the one it means to exercise.
LIVE = dict(uid_for_user=refuse("the passwd uid lookup"), _system_unit_is_active=refuse("the real system-unit probe"),
            _open_board_url=refuse("the real board opener"))


# --- structure -----------------------------------------------------------------------------------------------------


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python("import sys, scripts.packet_completion as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == "[]",
          f"it imports on its own and loads nothing else of Switchyard's: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.packet_completion", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.packet_completion"),
                  ("scripts.tenant_config_records", "scripts.team_launcher")):
        result = python("import importlib; "
                        f"[importlib.import_module(m) for m in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.packet_completion as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), all(getattr(t, n) is getattr(m, n) for n in {OWN!r}))")
        check(result.stdout.strip() == "True True", f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")


def test_the_seams_the_lazy_defaults_and_the_class() -> None:
    module = ast.parse((ROOT / "scripts" / "packet_completion.py").read_text(encoding="utf-8"))
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
    verifier = next(n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "privileged_packet_completion")
    eager = [d for d in verifier.args.kw_defaults if isinstance(d, ast.Lambda)]
    check(len(eager) == 1 and ast.unparse(eager[0]) == "lambda path: path.exists()"
          and not any(isinstance(n, ast.Name) and n.id == "launcher" for n in ast.walk(eager[0])),
          "the eager `exists` default is still the plain lambda, reading no launcher name")

    from scripts import packet_completion as m, team_launcher

    P = m.PacketCompletion
    fields = dataclasses.fields(P)
    check(P is team_launcher.PacketCompletion and P.__dataclass_params__.frozen and [f.name for f in fields] == ["problems"]
          and fields[0].default == () and isinstance(P.__dict__["done"], property),
          "one class: frozen, one field defaulting to (), done a property")
    check(m.privileged_packet_completion.__kwdefaults__["runner"] is subprocess.run
          and {k: v for k, v in m.privileged_packet_completion.__kwdefaults__.items() if k not in ("exists", "runner")}
          == {"system_unit_active": None, "owner_unit_active": None, "board_answers": None, "opener": None},
          "the runner defaults to subprocess.run, every callback to None")
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    defined = {n.name for n in launcher.body if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
    check(not defined & set(MOVED) and "_system_unit_is_active" in defined,
          f"the launcher defines none of them, and keeps the shared system-unit probe: {defined & set(MOVED)}")
    exported = [sorted(a.name for a in n.names) for n in launcher.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.packet_completion"]
    check(exported == [sorted(MOVED)], f"one explicit re-export of all five: {exported}")


# --- the result ---------------------------------------------------------------------------------------------------------


def test_the_result_is_frozen_and_done_only_when_empty() -> None:
    from scripts import team_launcher as t

    check(t.PacketCompletion().problems == () and t.PacketCompletion().done is True, "empty by default, and done")
    check(t.PacketCompletion(("x",)).done is False and t.PacketCompletion(problems=("a", "b")).problems == ("a", "b"), "not done with a problem")
    refused = judged(setattr, t.PacketCompletion(), "problems", ("y",))
    check(isinstance(refused, dataclasses.FrozenInstanceError), f"frozen: {refused!r}")
    check(t.PacketCompletion(("a",)) == t.PacketCompletion(("a",)) and hash(t.PacketCompletion(("a",))) == hash(t.PacketCompletion(("a",))),
          "equal and hashable by value")


# --- the probes ---------------------------------------------------------------------------------------------------------


def test_the_owners_user_manager_argv() -> None:
    from scripts import team_launcher as t

    asked: list = []
    with patched(t, **{**LIVE, "uid_for_user": lambda name: asked.append(name) or 1363}):
        got = judged(t._owner_user_unit_args, PLAN, "systemctl", "--user", "is-active", "x.service")
    check(got == ["sudo", "-u", "syrd363-owner", "env", "XDG_RUNTIME_DIR=/run/user/1363",
                  "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1363/bus", "systemctl", "--user", "is-active", "x.service"]
          and asked == ["syrd363-owner"], f"the exact sudo/env argv, the uid read through the launcher: {got}")


def runner_of(answers: list[object], calls: list):
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return answers.pop(0)
    return run


def test_unit_probes_pass_exact_argv_and_fail_without_a_return_code() -> None:
    from scripts import team_launcher as t

    quiet = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    for answer, active in ((SimpleNamespace(returncode=0), True), (SimpleNamespace(returncode=3), False), (SimpleNamespace(), False)):
        calls: list = []
        with patched(t, **{**LIVE, "_owner_user_unit_args": lambda plan, *args: ["OWNER", plan.owner_user, *args]}):
            got = judged(t._owner_unit_is_active, PLAN, "l.service", runner=runner_of([answer], calls))
        check(got is active and calls == [(["OWNER", "syrd363-owner", "systemctl", "--user", "is-active", "--quiet", "l.service"], quiet)],
              f"{answer}: {got}; the owner argv read through the launcher, quiet: {calls}")


class Opened:
    def __init__(self, log: list, *, enter: Exception | None = None, leave: Exception | None = None) -> None:
        self.log, self.enter, self.leave = log, enter, leave

    def __enter__(self):
        self.log.append("enter")
        if self.enter:
            raise self.enter
        return self

    def __exit__(self, *exc):
        self.log.append("exit")
        if self.leave:
            raise self.leave
        return False


def test_the_board_answers_only_if_opening_entering_and_leaving_succeed() -> None:
    from scripts import team_launcher as t

    for label, opener, answer, steps in (
            ("answers", lambda log: (lambda url: log.append(url) or Opened(log)), True, ["http://127.0.0.1:8363/api/board", "enter", "exit"]),
            ("open fails", lambda log: (lambda url: log.append(url) or (_ for _ in ()).throw(OSError("refused"))), False,
             ["http://127.0.0.1:8363/api/board"]),
            ("enter fails", lambda log: (lambda url: log.append(url) or Opened(log, enter=ValueError("bad"))), False,
             ["http://127.0.0.1:8363/api/board", "enter"]),
            ("exit fails", lambda log: (lambda url: log.append(url) or Opened(log, leave=RuntimeError("reset"))), False,
             ["http://127.0.0.1:8363/api/board", "enter", "exit"])):
        log: list = []
        got = judged(t._board_answers, PLAN, opener=opener(log))
        check(got is answer and log == steps, f"{label}: {got}; {log}")


# --- the verifier -------------------------------------------------------------------------------------------------------


def exists_of(missing: set[Path], asked: list):
    def exists(path: Path) -> bool:
        asked.append(path)
        return path not in missing
    return exists


def test_the_installed_paths_are_probed_in_order_with_no_short_circuit() -> None:
    from scripts import team_launcher as t

    asked: list = []
    with patched(t, **LIVE):
        got = judged(t.privileged_packet_completion, PLAN, exists=exists_of({p for p, _ in PATHS}, asked),
                     system_unit_active=lambda unit: asked.append(("system", unit)) or False,
                     owner_unit_active=lambda unit: asked.append(("owner", unit)) or False,
                     board_answers=lambda: asked.append("board") or False)
    check(asked == [*(p for p, _ in PATHS), ("system", "p363-ticket-board.service"), ("owner", "p363-notify-listener.service"), "board"],
          f"five paths in order, then the system unit, the listener, the board -- every one asked: {asked}")
    check(isinstance(got, t.PacketCompletion) and got.problems == (
        *(f"{d} ({p})" for p, d in PATHS),
        "the board service p363-ticket-board.service is not running",
        "the notify listener p363-notify-listener.service is not running for syrd363-owner",
        "the board does not answer on port 8363"), f"every problem, in order: {got}")
    asked.clear()
    with patched(t, **LIVE):
        got = judged(t.privileged_packet_completion, PLAN, exists=exists_of({PATHS[2][0]}, asked),
                     system_unit_active=lambda unit: True, owner_unit_active=lambda unit: True, board_answers=lambda: True)
    check(got == t.PacketCompletion((f"{PATHS[2][1]} ({PATHS[2][0]})",)), f"one missing artifact, and only that: {got}")
    with patched(t, **LIVE):
        got = judged(t.privileged_packet_completion, PLAN, exists=exists_of(set(), []),
                     system_unit_active=lambda unit: True, owner_unit_active=lambda unit: True, board_answers=lambda: True)
    check(got == t.PacketCompletion() and got.done, f"everything there: done: {got}")
    built: list = []
    with patched(t, **{**LIVE, "PacketCompletion": lambda problems: built.append(problems) or "built"}):
        got = judged(t.privileged_packet_completion, PLAN, exists=exists_of(set(), []),
                     system_unit_active=lambda unit: True, owner_unit_active=lambda unit: True, board_answers=lambda: False)
    check(got == "built" and built == [("the board does not answer on port 8363",)], f"the result is the launcher's class: {built}")


def test_the_lazy_defaults_reach_the_launcher_only_when_called() -> None:
    from scripts import team_launcher as t

    calls: list = []
    runner = refuse("the runner itself")
    fakes = dict(
        _system_unit_is_active=lambda unit, *, runner: calls.append(("system", unit, runner)) or True,
        _owner_unit_is_active=lambda plan, unit, *, runner: calls.append(("owner", plan, unit, runner)) or True,
        _board_answers=lambda plan, *, opener: calls.append(("board", plan, opener)) or True,
    )
    opener = refuse("an opener the verifier must only hand on")
    with patched(t, **{**LIVE, **fakes}):
        got = judged(t.privileged_packet_completion, PLAN, exists=exists_of(set(), []), runner=runner, opener=opener)
    check(got == t.PacketCompletion() and calls == [("system", "p363-ticket-board.service", runner),
                                                    ("owner", PLAN, "p363-notify-listener.service", runner), ("board", PLAN, opener)],
          f"each default reads its launcher helper, with the plan, runner and opener it captured: {calls}")
    calls.clear()
    board_opener = refuse("the launcher's board opener, which is only handed on")
    with patched(t, **{**LIVE, **fakes, "_open_board_url": board_opener}):
        judged(t.privileged_packet_completion, PLAN, exists=exists_of(set(), []), runner=runner)
    check(calls[-1] == ("board", PLAN, board_opener), f"no opener: the launcher's, read when the default runs: {calls[-1]}")
    calls.clear()
    with patched(t, **{**LIVE, **fakes}):
        judged(t.privileged_packet_completion, PLAN, exists=exists_of(set(), []), runner=runner, opener=opener,
               system_unit_active=lambda unit: True, owner_unit_active=lambda unit: True, board_answers=lambda: True)
    check(calls == [], f"injected callbacks replace the defaults entirely: {calls}")
    calls.clear()
    with patched(t, **{**LIVE, **fakes}):
        judged(t.privileged_packet_completion, PLAN, exists=exists_of(set(), []), runner=runner, opener=opener,
               system_unit_active=None, owner_unit_active=0, board_answers="")
    check([c[0] for c in calls] == ["system", "owner", "board"], f"a falsy callback is replaced by its default: {calls}")
    calls.clear()
    with patched(t, **{**LIVE, "_owner_user_unit_args": lambda plan, *args: ["OWNER", *args],
                       "_system_unit_is_active": lambda unit, *, runner: True, "_board_answers": lambda plan, *, opener: True}):
        run_calls: list = []
        got = judged(t.privileged_packet_completion, PLAN, exists=exists_of(set(), []),
                     runner=runner_of([SimpleNamespace(returncode=0)], run_calls), opener=opener)
    check(got == t.PacketCompletion() and run_calls and run_calls[0][0][:2] == ["OWNER", "systemctl"],
          f"the listener default runs the moved probe with the given runner, end to end: {run_calls}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_seams_the_lazy_defaults_and_the_class")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"packet_completion_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
