#!/usr/bin/env python3
"""SYRD-334: the board-authority launch preflight, against the launcher it came out of.

`process_authority_board_compatibility` moved into
`scripts/board_authority_preflight.py` unchanged. It reads nothing from the
launcher, so this pins what makes the move safe:

- **No cycle, and no launcher at all.** The module imports nothing of
  Switchyard's at its top and never names the launcher; the ticket board's
  `UnixHTTPConnection` stays the function's own import, captured by the lambda
  that builds the default connection (rule 27).
- The launcher still exports the very same function, whichever module is
  imported first, and `launch_project` calls it by the launcher's own name --
  so the suites' rebinding reaches the launch, and a refusal stops the launch
  before it changes anything.
- **The decision is unchanged:** the legacy bypass, the socket and timeout,
  the request, the decoding, every refusal and its message, and the connection
  closed however the probe ends.

Every connection here is this test's own fake; no socket is opened and no
board is asked.
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from launcher_main_view import launcher_body  # noqa: E402

CHECKS = 0
NAME = "process_authority_board_compatibility"
SOCKET = "/nonexistent/syrd334/board.sock"


def check(condition: bool, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


#: What launch_project reads through the launcher (SYRD-429).
LAUNCH_READS = ("_launch_runners_and_paths", "_prepare_launch", "_write_layout_and_plan", "_start_workers_and_present", "_report_launch",
                "process_authority_board_compatibility", "migrate_declarative_director_onboarding", "upgrade_generated_project_layout",
                "prepare_project_desktop", "_verify_pane_launcher_path", "WorkerStartup", "load_project_config")


def launcher_with_launch_project() -> ast.Module:
    """The launcher as launch_project's call sites see it: the launcher's own definitions, and launch_project.

    SYRD-429 moved launch_project to scripts/project_launch.py, where it reads each phase and check through the launcher
    when it runs. Checked first, on the source as it is: the launcher no longer defines it, re-exports it unaliased, and
    main and switchyard_main still call it by that name; its first statement is the call-time launcher import; every one
    of its launcher reads goes through the launcher, none bare. Only then is that import dropped, each `launcher.X` read
    as `X`, and the command appended to the launcher's body, so the call sites and positions below are the command's
    own. Before the move (the baseline) it is the launcher as it stands.
    """
    launcher = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    moved = ROOT / "scripts" / "project_launch.py"
    if not moved.exists():
        return launcher
    command = next(n for n in ast.parse(moved.read_text(encoding="utf-8")).body if isinstance(n, ast.FunctionDef) and n.name == "launch_project")
    check(not any(isinstance(n, ast.FunctionDef) and n.name == "launch_project" for n in launcher.body), "the launcher no longer defines launch_project")
    check(any(isinstance(n, ast.ImportFrom) and n.module == "scripts.project_launch"
              and any(a.name == "launch_project" and a.asname is None for a in n.names) for n in launcher.body),
          "the launcher re-exports it, unaliased")
    dispatch = {n.name: sum(isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "launch_project" for c in ast.walk(n))
                for n in launcher_body(ROOT, launcher) if isinstance(n, ast.FunctionDef) and n.name in ("main", "switchyard_main")}
    past = [n for n in ast.walk(launcher) if isinstance(n, ast.Attribute) and n.attr == "launch_project"]
    check(dispatch == {"main": 1, "switchyard_main": 2} and past == [],
          f"main and switchyard_main still call it by the launcher's name, as often as before, and nothing reaches past it: {dispatch}")
    check(ast.unparse(command.body[0]) == "from scripts import team_launcher as launcher",
          f"the command imports the launcher first thing, when it runs: {ast.unparse(command.body[0])}")
    bare = sorted({n.id for n in ast.walk(command) if isinstance(n, ast.Name) and n.id in LAUNCH_READS})
    through = sorted({n.attr for n in ast.walk(command)
                      if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == "launcher"})
    check(bare == [] and through == sorted(LAUNCH_READS), f"every launch_project read goes through the launcher, none bare: {bare} {through}")
    del command.body[0]

    class AsLauncherGlobal(ast.NodeTransformer):
        def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
            self.generic_visit(node)
            if isinstance(node.value, ast.Name) and node.value.id == "launcher":
                return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
            return node

    AsLauncherGlobal().visit(command)
    launcher.body.append(command)
    return launcher


def python(probe: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin"}, check=False)


def config(isolated: bool = True) -> SimpleNamespace:
    return SimpleNamespace(project="p334", role_state_isolation=isolated, board_socket=SOCKET)


class Board:
    """A fake connection and response, recording every call in order."""

    def __init__(self, status: int = 200, body: bytes | None = None, *, payload: object = None,
                 request_error: Exception | None = None, read_error: Exception | None = None) -> None:
        if body is None:
            body = json.dumps({"project": "p334", "authority_mode": "process", "assignments": {}}
                              if payload is None else payload).encode()
        self.status, self.body = status, body
        self.request_error, self.read_error = request_error, read_error
        self.events: list[tuple] = []

    def factory(self, socket_path: str, timeout: float) -> "Board":
        self.events.append(("connect", socket_path, timeout))
        return self

    def request(self, method: str, path: str) -> None:
        self.events.append(("request", method, path))
        if self.request_error:
            raise self.request_error

    def getresponse(self) -> "Board":
        self.events.append(("response",))
        return self

    def read(self) -> bytes:
        self.events.append(("read",))
        if self.read_error:
            raise self.read_error
        return self.body

    def close(self) -> None:
        self.events.append(("close",))


def ask(board: Board, cfg: SimpleNamespace | None = None) -> tuple[bool, str]:
    from scripts import board_authority_preflight

    return board_authority_preflight.process_authority_board_compatibility(cfg or config(),
                                                                         connection_factory=board.factory)


def test_the_module_loads_nothing_of_switchyards_at_import() -> None:
    result = python(
        "import sys, scripts.board_authority_preflight as m; "
        "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))"
    )
    check(result.returncode == 0, f"it imports on its own: {result.stderr[-600:]}")
    check(result.stdout.strip() == "[]",
          f"and loads no other Switchyard module -- not the launcher, not the board client: {result.stdout!r}")


def test_either_import_order_gives_one_set_of_objects() -> None:
    for order in (("scripts.board_authority_preflight", "scripts.team_launcher"),
                  ("scripts.team_launcher", "scripts.board_authority_preflight")):
        result = python(
            "import importlib, json; "
            f"[importlib.import_module(m) for m in {order!r}]; "
            "import scripts.team_launcher as t, scripts.board_authority_preflight as b; "
            f"print(t.{NAME} is b.{NAME}, b.json is t.json is json)"
        )
        check(result.stdout.strip() == "True True",
              f"{' then '.join(order)}: the function is the launcher's too, and json is the one module: "
              f"{result.stdout}{result.stderr[-600:]}")


def test_the_call_site_and_the_functions_own_names() -> None:
    launcher_tree = launcher_with_launch_project()
    calls = [n for n in ast.walk(launcher_tree)
             if isinstance(n, ast.Call) and getattr(n.func, "id", getattr(n.func, "attr", "")) == NAME]
    check(len(calls) == 1 and isinstance(calls[0].func, ast.Name),
          "launch_project calls it at its one baseline site, by the launcher's own (rebound) name")
    moved = ast.parse((ROOT / "scripts" / "board_authority_preflight.py").read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(moved) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(moved) if isinstance(n, ast.Attribute)}
    check("launcher" not in names and "team_launcher" not in {
        a.name.split(".")[-1] for n in ast.walk(moved) if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names},
        "the module never names or imports the launcher")
    function = next(n for n in moved.body if isinstance(n, ast.FunctionDef) and n.name == NAME)
    local = [n for n in ast.walk(function) if isinstance(n, ast.ImportFrom)]
    check([(n.module, [a.name for a in n.names]) for n in local]
          == [("scripts.ticket_board.write_client", ["UnixHTTPConnection"])]
          and not any(isinstance(n, ast.ImportFrom) and n.module == "scripts.ticket_board.write_client"
                      for n in moved.body),
          "the board client stays the function's own import, never the module's")
    lambdas = [n for n in ast.walk(function) if isinstance(n, ast.Lambda)]
    check(len(lambdas) == 1 and [a.arg for a in lambdas[0].args.args] == ["socket_path", "timeout"]
          and any(isinstance(n, ast.Name) and n.id == "UnixHTTPConnection" for n in ast.walk(lambdas[0])),
          "and the default connection's lambda captures that local import, by its bare name")


def test_the_legacy_bypass_asks_no_board() -> None:
    board = Board()
    check(ask(board, config(isolated=False)) == (True, "legacy account authority") and board.events == [],
          f"without role-state isolation it answers at once and opens nothing: {board.events}")


def test_a_ready_board() -> None:
    board = Board()
    check(ask(board) == (True, "process authority ready"), "a process-authority board for this project is ready")
    check(board.events == [("connect", SOCKET, 3), ("request", "GET", "/api/runtime-assignments"), ("response",),
                           ("read",), ("close",)],
          f"the project's board socket, a 3-second timeout, one GET of the runtime assignments, then close: "
          f"{board.events}")


def test_every_refusal_and_its_message() -> None:
    cases = [
        (Board(503, b"busy"), "runtime assignment probe returned HTTP 503: busy"),
        (Board(500, b"bad \xff"), "runtime assignment probe returned HTTP 500: bad �"),
        (Board(payload={"project": "someone-else", "authority_mode": "process", "assignments": {}}),
         "runtime assignment probe returned another project's identity"),
        (Board(payload=["p334"]), "runtime assignment probe returned another project's identity"),
        (Board(payload={"project": "p334", "authority_mode": "uid", "assignments": {}}),
         "running board still uses legacy uid authority"),
        (Board(payload={"project": "p334", "authority_mode": "process"}),
         "runtime assignment probe omitted its assignments object"),
        (Board(payload={"project": "p334", "authority_mode": "process", "assignments": []}),
         "runtime assignment probe omitted its assignments object"),
    ]
    for board, message in cases:
        answer = ask(board)
        check(answer == (False, message) and board.events[-1] == ("close",),
              f"refused with {message!r}, and closed: {answer} {board.events}")
    try:
        json.loads("not json")
    except ValueError as exc:
        unparsable = str(exc)
    board = Board(body=b"not json")
    try:
        answer = ask(board)
    except Exception as exc:  # noqa: BLE001 - what escaped is the finding
        answer = f"raised {exc!r}"
    check(answer == (False, unparsable),
          f"an unparsable body is refused with the parser's own words, never raised: {answer!r}")


def test_the_connection_is_closed_however_the_probe_fails() -> None:
    for board, message in ((Board(request_error=OSError("syrd334: request refused")), "syrd334: request refused"),
                           (Board(read_error=OSError("syrd334: read reset")), "syrd334: read reset")):
        answer = ask(board)
        check(answer == (False, message) and board.events[-1] == ("close",) and board.events.count(("close",)) == 1,
              f"a failure is a refusal in its own words, and the connection is closed once: {answer} {board.events}")

    def unreachable(socket_path: str, timeout: float) -> Board:
        raise OSError("syrd334: no such socket")
    from scripts import board_authority_preflight
    answer = board_authority_preflight.process_authority_board_compatibility(config(), connection_factory=unreachable)
    check(answer == (False, "syrd334: no such socket"), f"a connection that cannot be made is refused: {answer}")


def test_the_default_connection_is_the_board_clients() -> None:
    from scripts import board_authority_preflight
    from scripts.ticket_board import write_client

    made: list[tuple] = []
    board = Board()

    class Recorded:
        def __new__(cls, socket_path: str, *, timeout: float) -> Board:
            made.append((socket_path, timeout))
            return board

    saved = write_client.UnixHTTPConnection
    write_client.UnixHTTPConnection = Recorded
    try:
        answer = board_authority_preflight.process_authority_board_compatibility(config())
    finally:
        write_client.UnixHTTPConnection = saved
    check(answer == (True, "process authority ready") and made == [(SOCKET, 3)],
          f"with no factory given, the board client's connection is made to the socket with timeout 3: {made}")


def test_a_refusal_stops_the_launch_before_it_changes_anything() -> None:
    from scripts import team_launcher

    asked: list[object] = []
    printed: list[str] = []

    def refuse(*args: object, **kwargs: object) -> object:
        raise AssertionError(f"the launch went on past a refused preflight: {args} {kwargs}")

    saved = team_launcher.process_authority_board_compatibility
    team_launcher.process_authority_board_compatibility = lambda cfg: asked.append(cfg) or (False, "syrd334: old board")
    try:
        cfg = config()
        result = team_launcher.launch_project(cfg, config_path=Path("/nonexistent/syrd334/p334.json"), mode="attach",
                                              script_path=Path("/nonexistent/syrd334/switchyard-pane"), runner=refuse,
                                              print_func=printed.append)
    finally:
        team_launcher.process_authority_board_compatibility = saved
    check(result == 1 and asked == [cfg],
          f"the launch asks the launcher's (rebound) preflight, and stops: {result} {asked}")
    check(printed == ["team-launcher: refusing to launch p334 before changing local state: its running board "
                      "does not provide project-account process authority (syrd334: old board)"],
          f"saying why, before any runner is used: {printed}")


#: Run first: a seam taken past the launcher must be caught before any
#: behaviour check runs the real code it reached.
STRUCTURE = ("test_the_module_loads_nothing_of_switchyards_at_import",
             "test_either_import_order_gives_one_set_of_objects",
             "test_the_call_site_and_the_functions_own_names")


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE)]:
        globals()[name]()
    print(f"board_authority_preflight_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
