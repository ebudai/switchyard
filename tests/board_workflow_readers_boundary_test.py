#!/usr/bin/env python3
"""SYRD-437: the board workflow readers, against the launcher they came out of.

`read_board_declared_workflow` and `read_board_workflow_state` -- asking a
tenant's running board for its workflow document, and its revision, over the
board's own socket -- moved unchanged into `scripts/board_workflow_readers.py`;
the launcher re-exports both. This pins what makes that safe:

- **No cycle, one set of objects**, whichever module is imported first; the
  module alone loads no other Switchyard module and never imports the launcher
  when it runs (the config type is named for annotations only); the one
  default, the connection factory, is None, as before.
- **Seams:** the two read nothing from the launcher, as before; each keeps its
  own call-time import of `UnixHTTPConnection` from `write_client`, so a suite
  that stands the connection in on that module still reaches it.
- **Readers:** `pane_rebind.py`, `tenant_config_records.py`,
  `workflow_adoption.py` and `workflow_presence.py` read them through the
  launcher.
- **The behaviour is the baseline's:** a declared workflow and none, every
  malformed answer, non-200 statuses, and a failure at every step of the
  conversation -- each answered, never raised, word for word -- with the
  connection always closed once opened; the default connection and the
  launcher's names. `GOLDEN` below was produced by running the BASELINE
  launcher's own definitions over the very cases embedded here (`gold437.py`),
  not typed; it is byte-identical under `env -i`, in a normal role pane and
  with another HOME, USER and COLUMNS.

No live board is asked: the board is always a stand-in, and nothing connects
to a socket. Spawns, every exec, signals, account and group lookups and socket
connections are refused for each case.
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
from scripts import board_workflow_readers as m  # noqa: E402

CHECKS = 0
MOVED = ('read_board_declared_workflow', 'read_board_workflow_state')
#: Measured on the baseline launcher: each moved body's call-time reads of launcher globals (none).
SEAMS = {
}
#: Measured on the baseline launcher: every launcher definition outside the two that names them, and how often (none).
DISPATCH = {}
#: Measured on the baseline: every production module that reads one of them, and how.
READERS = {'scripts/pane_rebind.py': ['launcher.read_board_workflow_state', 'launcher.read_board_workflow_state'], 'scripts/tenant_config_records.py': ['launcher.read_board_declared_workflow'], 'scripts/workflow_adoption.py': ['launcher.read_board_declared_workflow', 'launcher.read_board_declared_workflow', 'launcher.read_board_declared_workflow', 'launcher.read_board_declared_workflow', 'launcher.read_board_workflow_state', 'launcher.read_board_workflow_state'], 'scripts/workflow_presence.py': ['launcher.read_board_declared_workflow']}
#: The BASELINE's own behaviour for the cases below (`gold437.py`, run on the baseline launcher under the guard).
GOLDEN = {
    'declared: a declared workflow': {'result': [{'roles': [{'name': 'director'}], 'stages': ['backlog']}, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: no declared workflow': {'result': [None, 'the board is running no declared workflow'], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: no document key': {'result': [None, 'the board is running no declared workflow'], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: a document that is not an object': {'result': [None, "the board's workflow response carries no document"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: a revision that is a string': {'result': [{'roles': [{'name': 'director'}], 'stages': ['backlog']}, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: a revision that is a boolean': {'result': [{'roles': [{'name': 'director'}], 'stages': ['backlog']}, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: no revision': {'result': [{'roles': [{'name': 'director'}], 'stages': ['backlog']}, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: an answer that is a list': {'result': [None, "the board's workflow response is not a document"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: an answer that is not JSON': {'result': [None, "the board's workflow could not be read: Expecting value: line 1 column 1 (char 0)"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: an answer that is not UTF-8': {'result': [None, "the board's workflow could not be read: Expecting value: line 1 column 1 (char 0)"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: an empty answer': {'result': [None, "the board's workflow could not be read: Expecting value: line 1 column 1 (char 0)"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: HTTP 201 with a document': {'result': [None, 'the board answered HTTP 201 for its workflow'], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: HTTP 204': {'result': [None, 'the board answered HTTP 204 for its workflow'], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: HTTP 404': {'result': [None, 'the board answered HTTP 404 for its workflow'], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: HTTP 503': {'result': [None, 'the board answered HTTP 503 for its workflow'], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: the connection cannot be made': {'result': [None, "the board's workflow could not be read: [Errno 111] Connection refused"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3]]},
    'declared: the request fails': {'result': [None, "the board's workflow could not be read: [Errno 32] Broken pipe"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['close']]},
    'declared: the response cannot be read': {'result': [None, "the board's workflow could not be read: timed out"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: a declared workflow': {'result': [7, {'roles': [{'name': 'director'}], 'stages': ['backlog']}, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: no declared workflow': {'result': [3, None, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: no document key': {'result': [2, None, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: a document that is not an object': {'result': [4, None, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: a revision that is a string': {'result': [0, {'roles': [{'name': 'director'}], 'stages': ['backlog']}, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: a revision that is a boolean': {'result': [1, {'roles': [{'name': 'director'}], 'stages': ['backlog']}, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: no revision': {'result': [0, {'roles': [{'name': 'director'}], 'stages': ['backlog']}, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: an answer that is a list': {'result': [0, None, "the board's workflow response is not a document"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: an answer that is not JSON': {'result': [0, None, "the board's workflow could not be read: Expecting value: line 1 column 1 (char 0)"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: an answer that is not UTF-8': {'result': [0, None, "the board's workflow could not be read: Expecting value: line 1 column 1 (char 0)"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: an empty answer': {'result': [0, None, "the board's workflow could not be read: Expecting value: line 1 column 1 (char 0)"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: HTTP 201 with a document': {'result': [0, None, 'the board answered HTTP 201 for its workflow'], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: HTTP 204': {'result': [0, None, 'the board answered HTTP 204 for its workflow'], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: HTTP 404': {'result': [0, None, 'the board answered HTTP 404 for its workflow'], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: HTTP 503': {'result': [0, None, 'the board answered HTTP 503 for its workflow'], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: the connection cannot be made': {'result': [0, None, "the board's workflow could not be read: [Errno 111] Connection refused"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3]]},
    'state: the request fails': {'result': [0, None, "the board's workflow could not be read: [Errno 32] Broken pipe"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['close']]},
    'state: the response cannot be read': {'result': [0, None, "the board's workflow could not be read: timed out"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: the default connection': {'result': [{'roles': [{'name': 'director'}], 'stages': ['backlog']}, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'state: the default connection': {'result': [1, {'roles': [{'name': 'director'}], 'stages': ['backlog']}, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    'declared: the default connection refused': {'result': [None, "the board's workflow could not be read: [Errno 111] Connection refused"], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3]]},
    "declared: through the launcher's name": {'result': [{'roles': [{'name': 'director'}], 'stages': ['backlog']}, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
    "state: through the launcher's name": {'result': [9, {'roles': [{'name': 'director'}], 'stages': ['backlog']}, ''], 'type': 'tuple', 'calls': [['connect', '/nonexistent/syrd437/board.sock', 3], ['request', 'GET', '/api/workflow'], ['getresponse'], ['read'], ['close']]},
}
REACHED: set[str] = set()
#: Measured on the candidate: what importing the module alone loads -- no other Switchyard module.
DEFAULT_MODULES_LOADED = []

# --- the cases, shared verbatim with `gold437.py` (which ran them on the baseline) ------------------------------------
# A case asks one of the two readers for a synthetic board's workflow. The board is always a stand-in: either the
# caller's `connection_factory`, or -- for the default path -- `UnixHTTPConnection` stood in on
# `scripts.ticket_board.write_client`, the module the readers import it from when they run. Nothing connects to a
# socket and no live board is asked. Recorded, in order: every connection made (socket path and timeout), every request,
# every close, and the answer.
DOC = {"roles": [{"name": "director"}], "stages": ["backlog"]}
BOARDS = {
    "a declared workflow": {"status": 200, "body": {"document": DOC, "revision": 7}},
    "no declared workflow": {"status": 200, "body": {"document": None, "revision": 3}},
    "no document key": {"status": 200, "body": {"revision": 2}},
    "a document that is not an object": {"status": 200, "body": {"document": ["roles"], "revision": 4}},
    "a revision that is a string": {"status": 200, "body": {"document": DOC, "revision": "7"}},
    "a revision that is a boolean": {"status": 200, "body": {"document": DOC, "revision": True}},
    "no revision": {"status": 200, "body": {"document": DOC}},
    "an answer that is a list": {"status": 200, "body": ["document"]},
    "an answer that is not JSON": {"status": 200, "raw": b"<html>not json</html>"},
    "an answer that is not UTF-8": {"status": 200, "raw": b"\xff\xfe{\"document\": null}"},
    "an empty answer": {"status": 200, "raw": b""},
    "HTTP 201 with a document": {"status": 201, "body": {"document": DOC, "revision": 5}},
    "HTTP 204": {"status": 204, "raw": b""},
    "HTTP 404": {"status": 404, "body": {"error": "no such route"}},
    "HTTP 503": {"status": 503, "raw": b"unavailable"},
    "the connection cannot be made": {"fail": "connect"},
    "the request fails": {"fail": "request"},
    "the response cannot be read": {"fail": "read"},
}
CASES = {
    **{f"declared: {k}": {"call": "declared", **v} for k, v in BOARDS.items()},
    **{f"state: {k}": {"call": "state", **v} for k, v in BOARDS.items()},
    "declared: the default connection": {"call": "declared", "status": 200, "body": {"document": DOC, "revision": 1}, "default": True},
    "state: the default connection": {"call": "state", "status": 200, "body": {"document": DOC, "revision": 1}, "default": True},
    "declared: the default connection refused": {"call": "declared", "fail": "connect", "default": True},
    "declared: through the launcher's name": {"call": "declared", "status": 200, "body": {"document": DOC}, "via": "launcher"},
    "state: through the launcher's name": {"call": "state", "status": 200, "body": {"document": DOC, "revision": 9}, "via": "launcher"},
}
FUNCTIONS = ("read_board_declared_workflow", "read_board_workflow_state")


def run_case(t: object, holder: object, spec: dict, reached: set) -> dict:
    """One case against `holder`'s readers, the board a stand-in; the default connection stood in on write_client."""
    import json
    from types import SimpleNamespace
    from scripts.ticket_board import write_client
    calls: list = []

    def note(what, *args):
        reached.add(what)
        calls.append([what, *[a if isinstance(a, (str, int, float, bool, type(None))) else repr(a) for a in args]])

    class Board:
        def __init__(self, socket_path, timeout):
            note("connect", socket_path, timeout)
            if spec.get("fail") == "connect":
                raise ConnectionRefusedError(111, "Connection refused")

        def request(self, method, path):
            note("request", method, path)
            if spec.get("fail") == "request":
                raise BrokenPipeError(32, "Broken pipe")

        def getresponse(self):
            note("getresponse")
            raw = spec["raw"] if "raw" in spec else json.dumps(spec.get("body")).encode("utf-8")

            def read():
                note("read")
                if spec.get("fail") == "read":
                    raise TimeoutError("timed out")
                return raw
            return SimpleNamespace(status=spec.get("status", 200), read=read)

        def close(self):
            note("close")

    saved_connection = write_client.UnixHTTPConnection
    try:
        reader = getattr(t if spec.get("via") == "launcher" else holder, "read_board_declared_workflow" if spec["call"] == "declared" else "read_board_workflow_state")
        config = SimpleNamespace(board_socket="/nonexistent/syrd437/board.sock")
        try:
            if spec.get("default"):
                write_client.UnixHTTPConnection = lambda socket_path, *, timeout: Board(socket_path, timeout)
                got = reader(config)
            else:
                write_client.UnixHTTPConnection = lambda *a, **k: (_ for _ in ()).throw(AssertionError("the default connection was used"))
                got = reader(config, connection_factory=lambda socket_path, timeout: Board(socket_path, timeout))
        except AssertionError:
            raise
        except BaseException as exc:  # noqa: BLE001 -- the baseline never raises; an answer that does is recorded, not crashed on
            return {"result": {"raised": type(exc).__name__, "message": str(exc)}, "calls": calls}
        return {"result": json.loads(json.dumps(got)), "type": type(got).__name__, "calls": calls}
    finally:
        write_client.UnixHTTPConnection = saved_connection
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
    result = python("import sys, scripts.board_workflow_readers as m; "
                    "print(sorted(n for n in sys.modules if n.startswith('scripts.') and n != m.__name__))")
    check(result.returncode == 0 and result.stdout.strip() == str(DEFAULT_MODULES_LOADED),
          f"it imports on its own, loading no other Switchyard module, never the launcher: {result.stdout}{result.stderr[-400:]}")


def test_either_import_order_gives_one_set_of_objects_and_no_defaults() -> None:
    for order in (("scripts.board_workflow_readers", "scripts.team_launcher"), ("scripts.team_launcher", "scripts.board_workflow_readers")):
        result = python("import importlib, inspect; "
                        f"[importlib.import_module(n) for n in {order!r}]; "
                        "import scripts.team_launcher as t, scripts.board_workflow_readers as m; "
                        f"print(all(getattr(t, n) is getattr(m, n) for n in {MOVED!r}), "
                        "sorted(f'{n}.{k}={v.default!r}' for n in " + repr(FUNCTIONS) + " for k, v in inspect.signature(getattr(m, n)).parameters.items() "
                        "if v.default is not inspect.Parameter.empty), "
                        "not hasattr(m, 'launcher') and not hasattr(m, 'team_launcher') and not hasattr(m, 'ProjectConfig'))")
        check(result.stdout.strip() == "True ['read_board_declared_workflow.connection_factory=None', 'read_board_workflow_state.connection_factory=None'] True",
              f"{' then '.join(order)}: {result.stdout}{result.stderr[-600:]}")
    import typing
    check(m.json is json and m.Any is typing.Any and m.Callable is typing.Callable, "the standard-library names are the module's own")


def test_the_seams_read_through_the_launcher_and_nothing_bound() -> None:
    tree = ast.parse((ROOT / "scripts" / "board_workflow_readers.py").read_text(encoding="utf-8"))
    for name in FUNCTIONS:
        node = next(n for n in tree.body if getattr(n, "name", None) == name)
        through = sorted({x.attr for x in ast.walk(node) if isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name) and x.value.id == "launcher"})
        imports = [ast.unparse(x) for x in ast.walk(node) if isinstance(x, (ast.Import, ast.ImportFrom))]
        check(SEAMS.get(name, {}) == {} and through == [] and imports == ["from scripts.ticket_board.write_client import UnixHTTPConnection"],
              f"{name}: reads nothing from the launcher, as before, and keeps its one call-time import of the socket connection: {through} {imports}")
    top = [ast.unparse(n) for n in tree.body if isinstance(n, (ast.Import, ast.ImportFrom))]
    tc = [ast.unparse(n) for n in tree.body if isinstance(n, ast.If)]
    check(top == ["from __future__ import annotations", "import json", "from typing import TYPE_CHECKING, Any, Callable"]
          and tc == ["if TYPE_CHECKING:\n    from scripts.team_launcher import ProjectConfig"],
          f"the standard library, and the config type for annotations only: {top} {tc}")
    runtime = [ast.unparse(x) for x in ast.walk(tree) if isinstance(x, (ast.Import, ast.ImportFrom)) and "team_launcher" in ast.unparse(x)]
    check(runtime == ["from scripts.team_launcher import ProjectConfig"], f"the launcher is named only for the annotation, never imported when it runs: {runtime}")
    names = [n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))]
    check(names == list(MOVED), f"the two in the launcher's order, and nothing else: {names}")


def test_the_launcher_reexports_the_two_and_its_readers_reach_them_there() -> None:
    tree = ast.parse((ROOT / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and n.module == "scripts.board_workflow_readers"]
    check(len(imports) == 1 and sorted(a.name for a in imports[0].names) == sorted(MOVED) and all(a.asname is None for a in imports[0].names),
          "one explicit import of exactly the two, unaliased")
    check(imports[0].lineno < min(n.lineno for n in tree.body if isinstance(n, (ast.FunctionDef, ast.ClassDef, ast.Assign))),
          "at the top, above every definition and constant that could read them")
    defined = {getattr(n, "name", None) for n in tree.body} | {x.id for n in tree.body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
    exported = {a.name for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "").startswith("scripts.")
                for a in n.names if a.asname is None}
    check(not defined & set(MOVED) and {"ProjectConfig", "load_project_config"} <= defined | exported,
          "the launcher defines neither, and keeps the config type they are annotated with")
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
    for path, uses in READERS.items():
        source = ast.parse((ROOT / path).read_text(encoding="utf-8"))
        got = sorted(ast.unparse(x) for x in ast.walk(source) if (isinstance(x, ast.Attribute) and x.attr in MOVED) or (isinstance(x, ast.Name) and x.id in MOVED))
        check(got == uses, f"{path} still reads them through the launcher: {got}")


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

    def steps(label):
        return [c[0] for c in GOLDEN[label]["calls"]]

    check(result("declared: a declared workflow") == [DOC, ""] and result("state: a declared workflow") == [7, DOC, ""],
          "a declared workflow: the document, and the revision with it")
    check(result("declared: no declared workflow") == result("declared: no document key") == [None, "the board is running no declared workflow"]
          and result("declared: a document that is not an object") == [None, "the board's workflow response carries no document"]
          and result("declared: an answer that is a list") == [None, "the board's workflow response is not a document"],
          "the declared-workflow reader says why there is no document, word for word")
    check([result(f"state: {k}")[0] for k in ("a revision that is a string", "a revision that is a boolean", "no revision", "no declared workflow")] == [0, 1, 0, 3]
          and result("state: a document that is not an object") == [4, None, ""],
          "the state reader: an integer revision as given (a boolean counts), else 0; a non-object document is None, with no reason")
    check(not any(isinstance(GOLDEN[k]["result"], dict) for k in GOLDEN), "neither reader ever raises: every case is answered")
    check(result("declared: HTTP 201 with a document") == [None, "the board answered HTTP 201 for its workflow"] and result("state: HTTP 204")[2] == "the board answered HTTP 204 for its workflow"
          and result("declared: HTTP 404") == [None, "the board answered HTTP 404 for its workflow"] and result("state: HTTP 503")[2] == "the board answered HTTP 503 for its workflow"
          and all(result(f"declared: {k}")[1].startswith("the board's workflow could not be read: ")
                  for k in ("an answer that is not JSON", "an answer that is not UTF-8", "an empty answer", "the connection cannot be made", "the request fails", "the response cannot be read")),
          "a non-200 answer is named; any failure to read is 'cannot say', never raised")
    check(all(steps(k)[-1] == "close" for k in GOLDEN if steps(k) != ["connect"]) and steps("declared: the connection cannot be made") == ["connect"],
          "the connection is closed whenever it was opened, whatever failed")
    check(all(GOLDEN[k]["calls"][0] == ["connect", "/nonexistent/syrd437/board.sock", 3] for k in GOLDEN)
          and all(c == ["request", "GET", "/api/workflow"] for k in GOLDEN for c in GOLDEN[k]["calls"] if c[0] == "request"),
          "the board's own socket, a 3-second timeout, one GET /api/workflow")
    check(result("declared: the default connection") == [DOC, ""] and result("state: the default connection") == [1, DOC, ""]
          and result("declared: through the launcher's name") == [DOC, ""],
          "the default connection is write_client's, imported when the reader runs; the launcher's names answer the same")


def test_every_launcher_seam_is_reached() -> None:
    # The two read nothing from the launcher; what they reach when they run is the socket connection, stood in on
    # write_client for the default path, and every step of it is recorded.
    check(SEAMS == {} and {"connect", "request", "getresponse", "read", "close"} <= REACHED,
          f"no launcher seam to reach, and every step of the board conversation reached: {sorted(REACHED)}")


STRUCTURE = ("test_the_module_loads_only_where_its_defaults_come_from", "test_either_import_order_gives_one_set_of_objects_and_no_defaults",
             "test_the_seams_read_through_the_launcher_and_nothing_bound", "test_the_launcher_reexports_the_two_and_its_readers_reach_them_there")
LAST = ("test_every_launcher_seam_is_reached",)


def main() -> int:
    names = sorted(name for name, value in globals().items() if name.startswith("test_") and callable(value))
    for name in [*STRUCTURE, *(name for name in names if name not in STRUCTURE + LAST), *LAST]:
        globals()[name]()
    print(f"board_workflow_readers_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
