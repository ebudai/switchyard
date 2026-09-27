#!/usr/bin/env python3
"""SYRD-330: live CLI and model detection and the stale-provider-runtime drop, against the launcher they came out of.

Six definitions moved into `scripts/live_role_runtime.py` unchanged.
`KNOWN_LIVE_CLI_NAMES` did not: it is built from the launcher's
`SUPPORTED_CONFIG_CLI_NAMES` when the launcher is imported, and the moved code
reads it there. Nor did the process and pane primitives they call -- the
process snapshot, the pane pid, the command names -- which the suites rebind
and other modules read. This pins what makes that safe:

- **No cycle.** The module imports nothing of Switchyard's at its top.
- Every moved name is still the launcher's, the very same object whichever
  module is imported first; the launcher calls its three entry points by its
  own name.
- **Seams (rule 24).** Everything the functions call -- each other included --
  is read through the launcher when they run, so a rebinding on the launcher
  reaches them; nothing is read past it.
- **The decisions are unchanged:** the process-tree walk, the model argv
  forms, which live CLI a pane is running, the argv model before the session
  record, which running roles are stale, and the drop's notices, order, account
  runner and unreconciled roles.

The snapshot, pane pids, generations, runners and kill argv are this test's own
fakes; no `ps` runs, no tmux is asked and no process or session is touched.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CHECKS = 0

EXPORTED = ("process_tree_argvs", "_model_from_argv", "live_cli_for_role", "live_model_for_role",
            "roles_with_stale_provider_runtime", "_drop_roles_with_stale_provider_runtime")
STAYED = ("KNOWN_LIVE_CLI_NAMES", "pane_pid_for_role", "_process_snapshot", "process_tree_command_names",
          "tmux_pane_pid_args", "role_process_runner_for")
#: Measured on the baseline: the launcher's own call sites.
LAUNCHER_CALLS = {"live_cli_for_role": 1, "live_model_for_role": 1, "_drop_roles_with_stale_provider_runtime": 1}
#: Measured on the baseline: the moved code's calls, each through the launcher.
MOVED_CALLS = {"_process_snapshot": 1, "pane_pid_for_role": 2, "process_tree_command_names": 1, "_command_name": 1,
               "process_tree_argvs": 1, "_model_from_argv": 1, "_session_payload_model_for_role": 1,
               "_role_cli_name": 2, "provider_state_store_problem": 1, "recorded_provider_state_generation": 1,
               "provider_state_generation": 1, "unreadable_provider_state_roles": 1,
               "roles_with_stale_provider_runtime": 1, "role_process_runner_for": 1, "tmux_kill_session_args": 1}
READS = ("KNOWN_LIVE_CLI_NAMES",)
HOME = Path("/nonexistent/syrd330/owner-home")
SESSIONS = Path("/nonexistent/syrd330/sessions")


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


class patched:
    """Rebind attributes of one module for one block, as the suites do."""

    def __init__(self, module: object, **values: object) -> None:
        self.module = module
        self.values = values

    def __enter__(self) -> None:
        self.saved = {name: getattr(self.module, name) for name in self.values}
        for name, value in self.values.items():
            setattr(self.module, name, value)

    def __exit__(self, *exc: object) -> None:
        for name, value in self.saved.items():
            setattr(self.module, name, value)


def refuse(what: str):
    def refused(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"{what} must not be asked here: {args} {kwargs}")
    return refused


def role(name: str, *, cli: str = "syrd330-cli", model_arg: str = "--model") -> SimpleNamespace:
    return SimpleNamespace(role=name, cli=[f"/opt/syrd330/bin/{cli}"], model_arg=model_arg)


RUNNER = refuse("the caller's runner")
CONFIG = SimpleNamespace(project="p330")


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.live_role_runtime as m; "
        "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]", f"and loads no other Switchyard module, the launcher least of all: "
                                         f"{result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.live_role_runtime", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.live_role_runtime")):
        result = python(
            "import importlib; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.live_role_runtime as r; "
            f"print(all(getattr(t, n) is getattr(r, n) for n in {EXPORTED!r}), "
            f"any(hasattr(r, n) for n in {STAYED!r}), "
            "t.KNOWN_LIVE_CLI_NAMES == set(t.SUPPORTED_CONFIG_CLI_NAMES))"
        )
        check(result.stdout.strip() == "True False True",
              f"{' then '.join(order)}: every moved name is the launcher's too, and the shared ones stayed: "
              f"{result.stdout}{result.stderr[-600:]}")


def test_the_calls_the_seams_and_the_functions_own_names() -> None:
    launcher_tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    for name, count in LAUNCHER_CALLS.items():
        calls = [n for n in ast.walk(launcher_tree)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) == count and all(isinstance(n.func, ast.Name) for n in calls),
              f"the launcher calls {name} at its {count} baseline site, by its own name")
    moved = ast.parse((ROOT / "scripts" / "live_role_runtime.py").read_text(encoding="utf-8"))
    top = [n for n in moved.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    check(not any("team_launcher" in ast.dump(n) for n in top),
          "the launcher is never imported at the module's top, only when a function runs")
    for name, count in MOVED_CALLS.items():
        calls = [n for n in ast.walk(moved)
                 if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == name]
        check(len(calls) == count and all(isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                                          and n.func.value.id == "launcher" for n in calls),
              f"the moved code calls {name} at its {count} baseline sites, each through the launcher")
    reads = [n for n in ast.walk(moved) if isinstance(n, ast.Attribute) and n.attr in READS]
    check(len(reads) == 1 and all(isinstance(n.value, ast.Name) and n.value.id == "launcher" for n in reads),
          "the known live CLI names are read through the launcher, at their one site")
    bare = sorted({n.id for n in ast.walk(moved) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
                   and n.id in (*MOVED_CALLS, *READS)})
    check(bare == [], f"no launcher name is read past it: {bare}")
    for function in (n for n in moved.body if isinstance(n, ast.FunctionDef)):
        bound = {a.arg for a in function.args.args + function.args.kwonlyargs}
        bound |= {n.id for n in ast.walk(function) if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store)}
        through = sorted({n.attr for n in ast.walk(function) if isinstance(n, ast.Attribute)
                          and isinstance(n.value, ast.Name) and n.value.id == "launcher" and n.attr in bound})
        check(through == [], f"{function.name}: nothing it binds itself is read as the launcher's: {through}")


def snapshot(children: dict[int, list[int]], argv: dict[int, list[str]]):
    asked: list[str] = []

    def take() -> tuple[dict, dict, dict, dict]:
        asked.append("snapshot")
        return {}, children, {}, argv
    return take, asked


def test_the_process_tree_walk() -> None:
    from scripts import live_role_runtime, team_launcher

    with patched(team_launcher, _process_snapshot=refuse("the process snapshot")):
        check(live_role_runtime.process_tree_argvs(0) == [] and live_role_runtime.process_tree_argvs(-3) == [],
              "no pane pid walks nothing, and takes no snapshot")
    argv = {10: ["shell"], 11: ["syrd330-cli", "--model", "m1"], 12: [], 13: ["child"]}
    take, asked = snapshot({10: [11, 12], 11: [13], 13: [10]}, argv)
    with patched(team_launcher, _process_snapshot=take):
        records = live_role_runtime.process_tree_argvs(10)
    check(asked == ["snapshot"], f"one snapshot, the launcher's, as rebound: {asked}")
    check(records == [["shell"], ["syrd330-cli", "--model", "m1"], ["child"]],
          f"every descendant once, depth first from the last child, and an empty argv skipped: {records}")
    check(all(record is not argv[pid] for record, pid in zip(records, (10, 11, 13))),
          "each record is a copy, not the snapshot's own list")


def test_the_model_argv_forms() -> None:
    from scripts import live_role_runtime

    m = live_role_runtime._model_from_argv
    check(m(["cli", "--model", " m1 "], model_arg="--model") == "m1", "a model given as the next argument")
    check(m(["cli", "--model= m2 "], model_arg="--model") == "m2", "or joined with =")
    check(m(["cli", "--model=a", "--model", "b"], model_arg="--model") == "a", "the first one given wins")
    check(m(["cli", "--model"], model_arg="--model") == "", "a flag with no value is no model")
    check(m(["cli", "--model", "m1"], model_arg="") == "", "and a CLI with no model flag has none")


def test_which_live_cli_a_pane_runs() -> None:
    from scripts import live_role_runtime, team_launcher

    asked: list[object] = []
    names = {"a": {"syrd330-a", "bash"}, "ab": {"syrd330-a", "syrd330-b"}, "none": {"bash"}}
    known = {"syrd330-a", "syrd330-b"}

    def lookups(pid: int, tree: str) -> dict[str, object]:
        return dict(pane_pid_for_role=lambda role, runner: asked.append(("pid", role.role, runner)) or pid,
                    process_tree_command_names=lambda pane: asked.append(("tree", pane)) or names[tree],
                    KNOWN_LIVE_CLI_NAMES=known,
                    _command_name=lambda value: asked.append(("name", value)) or Path(value).name)

    with patched(team_launcher, **lookups(0, "a")):
        none = live_role_runtime.live_cli_for_role(role("r"), runner=RUNNER)
    check(none == [] and asked == [("pid", "r", RUNNER)],
          f"no pane pid is no live CLI, and the tree is not asked; the runner is handed on: {asked}")
    with patched(team_launcher, **lookups(41, "a")):
        single = live_role_runtime.live_cli_for_role(role("r", cli="other"), runner=RUNNER)
    check(single == ["syrd330-a"], f"one known CLI in the tree is the live one, whatever is configured: {single}")
    with patched(team_launcher, **lookups(42, "ab")):
        tie = live_role_runtime.live_cli_for_role(role("r", cli="syrd330-b"), runner=RUNNER)
        unknown = live_role_runtime.live_cli_for_role(role("r", cli="other"), runner=RUNNER)
    check(tie == ["syrd330-b"], f"of two known CLIs, the configured one: {tie}")
    check(unknown == [], f"and none when the configured one is neither: {unknown}")
    with patched(team_launcher, **lookups(43, "none")):
        check(live_role_runtime.live_cli_for_role(role("r"), runner=RUNNER) == [],
              "a tree with no known CLI names none -- the launcher's known names, as rebound")


def test_the_live_model_prefers_the_process_argv() -> None:
    from scripts import live_role_runtime, team_launcher

    asked: list[object] = []

    def lookups(pid: int, argvs: list[list[str]]) -> dict[str, object]:
        return dict(pane_pid_for_role=lambda role, runner: pid,
                    process_tree_argvs=lambda pane: asked.append(("argvs", pane)) or argvs,
                    _session_payload_model_for_role=lambda role, session_dir: asked.append(
                        ("record", role.role, session_dir)) or "from-record")

    with patched(team_launcher, **lookups(51, [["sh"], ["cli", "--model", "live-m"], ["cli", "--model", "later"]])):
        live = live_role_runtime.live_model_for_role(role("r"), session_dir=SESSIONS, runner=RUNNER)
    check(live == "live-m" and asked == [("argvs", 51)],
          f"the first model in the pane's tree wins, and the record is not read: {live} {asked}")
    asked.clear()
    with patched(team_launcher, **lookups(52, [["sh"]])):
        fallback = live_role_runtime.live_model_for_role(role("r"), session_dir=SESSIONS, runner=RUNNER)
    check(fallback == "from-record" and asked == [("argvs", 52), ("record", "r", SESSIONS)],
          f"with no model in the tree, the session record's: {asked}")
    asked.clear()
    with patched(team_launcher, **lookups(0, [["cli", "--model", "never"]])):
        gone = live_role_runtime.live_model_for_role(role("r"), session_dir=SESSIONS, runner=RUNNER)
    check(gone == "from-record" and asked == [("record", "r", SESSIONS)],
          f"and with no pane, the record's, without walking any tree: {asked}")


def generations(recorded: dict[str, str | None], problems: dict[str, str], cli_names: dict[str, str],
                asked: list[object]) -> dict[str, object]:
    return dict(
        _role_cli_name=lambda r: cli_names[r.role],
        provider_state_store_problem=lambda config, r: asked.append(("store", r.role)) or problems.get(r.role, ""),
        # A role this test gives no record reads as never recorded -- stale --
        # so a role that should not have been asked about shows in the answer.
        recorded_provider_state_generation=lambda config, r: asked.append(("recorded", r.role)) or recorded.get(r.role),
        provider_state_generation=lambda cli, *, owner_home: asked.append(("now", cli, owner_home)) or f"gen-{cli}",
    )


def test_which_running_roles_are_stale() -> None:
    from scripts import live_role_runtime, team_launcher

    roles = [role(n) for n in ("nocli", "unreadable", "fresh", "never", "older")]
    cli_names = {"nocli": "", "unreadable": "claude", "fresh": "claude", "never": "codex", "older": "claude"}
    recorded = {"fresh": "gen-claude", "never": None, "older": "gen-old"}
    asked: list[object] = []
    with patched(team_launcher, **generations(recorded, {"unreadable": "not readable"}, cli_names, asked)):
        stale = live_role_runtime.roles_with_stale_provider_runtime(CONFIG, roles, owner_home=HOME)
    check([r.role for r in stale] == ["never", "older"],
          f"a role never recorded, or recorded against older state, is stale: {[r.role for r in stale]}")
    check(("store", "nocli") not in asked and ("recorded", "unreadable") not in asked,
          f"a role with no CLI is not asked about, and an unreadable store answers nothing: {asked}")
    check(("now", "claude", HOME) in asked and ("now", "codex", HOME) in asked,
          "the account's generation is asked per CLI, of the owner's home")


def dropping(stale: list[str], ended: dict[str, object], asked: list[object],
             unreadable: list[tuple[str, str]] = ()) -> dict[str, object]:
    def runner_for(config: object, r: SimpleNamespace, *, runner: object):
        asked.append(("runner", r.role, runner))
        return lambda argv, **kwargs: asked.append(("run", r.role, argv, kwargs)) or ended[r.role]
    return dict(
        unreadable_provider_state_roles=lambda config, running: [(role(n), p) for n, p in unreadable],
        roles_with_stale_provider_runtime=lambda config, running, *, owner_home: asked.append(
            ("stale?", [r.role for r in running], owner_home)) or [r for r in running if r.role in stale],
        role_process_runner_for=runner_for,
        tmux_kill_session_args=lambda r: ["syrd330-kill", r.role],
        _role_cli_name=lambda r: "claude",
    )


def test_nothing_stale_is_left_alone() -> None:
    from scripts import live_role_runtime, team_launcher

    asked: list[object] = []
    printed: list[str] = []
    running = [role("a"), role("b")]
    with patched(team_launcher, **dropping([], {}, asked, unreadable=[("b", "its store is root's")])):
        kept, unreconciled = live_role_runtime._drop_roles_with_stale_provider_runtime(
            CONFIG, running, owner_home=HOME, runner=RUNNER, print_func=printed.append)
    check(kept == running and kept is not running and unreconciled == set(),
          f"every running role is kept, in a new list, and none is unreconciled: {kept} {unreconciled}")
    check(asked == [("stale?", ["a", "b"], HOME)], f"nothing is ended or even given a runner: {asked}")
    check(printed == ["team-launcher: leaving b running: its store is root's. Its runtime is not checked against "
                      "the account's provider state, and restarting it would settle nothing. Run `sudo switchyard "
                      "upgrade p330` to give the tenant account its own role state back."],
          f"an unreadable store is said once, with its repair: {printed}")


def test_stale_roles_are_ended_through_their_own_runner() -> None:
    from scripts import live_role_runtime, team_launcher

    asked: list[object] = []
    printed: list[str] = []
    running = [role("keep"), role("zeta"), role("stuck"), role("alpha"), role("mute")]
    ended = {"zeta": SimpleNamespace(returncode=0), "stuck": SimpleNamespace(returncode=1),
             "alpha": SimpleNamespace(returncode=0), "mute": object()}
    with patched(team_launcher, **dropping(["zeta", "stuck", "alpha", "mute"], ended, asked,
                                           unreadable=[("other", "unreadable")])):
        kept, unreconciled = live_role_runtime._drop_roles_with_stale_provider_runtime(
            CONFIG, running, owner_home=HOME, runner=RUNNER, print_func=printed.append)
    check([r.role for r in kept] == ["keep", "stuck", "mute"] and unreconciled == {"stuck", "mute"},
          f"a role that could not be ended is kept and unreconciled -- a result with no code included: "
          f"{[r.role for r in kept]} {unreconciled}")
    runs = [a for a in asked if a[0] in ("runner", "run")]
    check(runs == [("runner", "zeta", RUNNER), ("run", "zeta", ["syrd330-kill", "zeta"],
                                                {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}),
                   ("runner", "stuck", RUNNER), ("run", "stuck", ["syrd330-kill", "stuck"],
                                                  {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}),
                   ("runner", "alpha", RUNNER), ("run", "alpha", ["syrd330-kill", "alpha"],
                                                  {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}),
                   ("runner", "mute", RUNNER), ("run", "mute", ["syrd330-kill", "mute"],
                                                 {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL})],
          f"each stale role, in order, is ended by its own account's runner, built from the caller's: {runs}")
    check(printed[0].startswith("team-launcher: leaving other running: unreadable.")
          and printed[1:4] == [
              "team-launcher: stuck is running against older claude state and its session could not be ended; it "
              "will keep showing whatever it was showing until it is restarted",
              "team-launcher: mute is running against older claude state and its session could not be ended; it "
              "will keep showing whatever it was showing until it is restarted",
              "team-launcher: restarting alpha, zeta: their runtimes started against older provider state than "
              "this account now has"] and len(printed) == 4,
          f"the notice first, then each failure, then one restart line, sorted: {printed}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_calls_the_seams_and_the_functions_own_names")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"live_role_runtime_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
