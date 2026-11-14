#!/usr/bin/env python3
"""SYRD-769: root's readers of a tenant board ask the board root's own unit names, and show a board's words inert.

As root, `start`, `resume-provision`, `replace-window`, `adopt-workflow`, `release-status --close` and
`deploy-release` read a tenant's board. Each took the board's address from the tenant's configuration
(`board_socket`, `board_url`), which the tenant can point at a socket or URL it controls. Now, as root, each asks the
socket root's unit for that project names, answered (the kernel says) by the unit's service account in the unit's
main process (`board_identity.unit_connection`), and never connects to what the configuration names.

Here, with no privilege: root's unit directory and anchor are a temporary tree this account owns, the board's service
account is this account, and "root" is `board_identity.geteuid` answering 0. Every board is a real server on a real
socket, so every connection is the kernel's, SO_PEERCRED included:
- the unit's board, on the socket the unit names;
- a foreign board on the configuration's socket, and another on its URL, each counting what it is asked.

Also: a board answering with an escape-laden status line, or an HTTP reason phrase, is shown as plain text (or our
own words) by every reader, root's or not.
"""

from __future__ import annotations

import http.server
import json
import os
import pwd
import shutil
import socket
import socketserver
import sys
import tempfile
import threading
import unicodedata
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import board_authority_preflight, board_supervisor_migration, board_workflow_readers  # noqa: E402
from scripts import presentation_role_recovery, presentation_runtime_assignments  # noqa: E402
from scripts import provider_runtime_state, release_alignment  # noqa: E402
from scripts.ticket_board import board_identity  # noqa: E402

PROJECT = "p769"
ME = pwd.getpwuid(os.getuid()).pw_name
ESCAPE = "\x1b]0;owned\x07\x1b[2K\x9b31m‮"
CHECKS = 0


def check(condition: bool, detail: str) -> None:
    global CHECKS
    CHECKS += 1
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")


def texts(answer: object) -> list[str]:
    """Every string in an answer, as it would be printed: a tuple's repr escapes what printing its parts would not."""
    if isinstance(answer, str):
        return [answer]
    if isinstance(answer, dict):
        return [t for key, value in answer.items() for part in (key, value) for t in texts(part)]
    if isinstance(answer, (tuple, list, set, frozenset)):
        return [t for part in answer for t in texts(part)]
    return [str(answer)]


def controls(answer: object) -> list[str]:
    return sorted({f"U+{ord(c):04X}" for text in texts(answer) for c in text if unicodedata.category(c)[0] == "C"})


# -- boards ---------------------------------------------------------------------------------------------------------
def answers(name: str) -> dict[str, Any]:
    return {
        "/api/runtime-assignments": {"project": PROJECT, "authority_mode": "process", "assignments": {}},
        "/api/workflow": {"document": {"roles": [], "said_by": name}, "revision": 3},
        "/api/board": {"build_id": f"{name}build"},
    }


#: What a board's `/api/image/<frame dir path>` serves: the bytes of a file the tenant owner wrote, here shaped as every
#: answer root reads. Root must never ask for it (Audit of 8f0874b0).
TENANT_WRITTEN = {"build_id": "TENANT-CHOSEN-BUILD", "project": PROJECT, "authority_mode": "process",
                  "assignments": {}, "document": {"roles": [], "said_by": "tenant"}, "revision": 9}


class Board:
    """A board on a Unix socket or loopback TCP, counting every request. `raw` answers bytes instead of HTTP."""

    def __init__(self, name: str, *, unix: str = "", raw: bytes = b"") -> None:
        self.name, self.asked, self.raw = name, [], raw
        board = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - the stdlib's name
                board.asked.append(self.path)
                if board.raw:
                    self.wfile.write(board.raw)
                    return
                body = json.dumps(TENANT_WRITTEN if self.path.startswith("/api/image/")
                                  else answers(board.name).get(self.path, {})).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def address_string(self) -> str:
                return "local"

            def log_message(self, *_args: object) -> None:
                return None

        if unix:
            class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
                daemon_threads = True
            self.server: socketserver.BaseServer = Server(unix, Handler)
            self.where = unix
        else:
            class TCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
                daemon_threads = True
                allow_reuse_address = True
            self.server = TCPServer(("127.0.0.1", 0), Handler)
            self.port = self.server.server_address[1]
            self.where = f"http://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


class Rig:
    """Root's unit tree, the unit's board, and the configuration's foreign boards."""

    def __init__(self, *, user: str = ME, unit_board: bool = True, unit_raw: bytes = b"") -> None:
        self.tree = Path(tempfile.mkdtemp(prefix="s769-", dir="/tmp"))  # short: a socket path has 107 bytes
        os.chmod(self.tree, 0o755)
        (self.tree / "units").mkdir(mode=0o755)
        self.unit_socket = str(self.tree / "board.sock")
        self.unit_file = self.tree / "units" / f"{PROJECT}-ticket-board.service"
        self.unit_file.write_text(
            "[Service]\n"
            f"User={user}\n"
            "ExecStart=/usr/bin/python3 -E -s -B /opt/r/scripts/ticket-board.py --host 127.0.0.1 --port 1 "
            f"--unix-socket {self.unit_socket}\n", encoding="utf-8")
        os.chmod(self.unit_file, 0o644)
        self.unit = Board("unit", unix=self.unit_socket, raw=unit_raw) if unit_board else None
        self.foreign_socket = Board("foreign", unix=str(self.tree / "foreign.sock"))
        self.foreign_url = Board("foreign")
        self.main_pid = os.getpid()
        self.saved = {name: getattr(board_identity, name)
                      for name in ("ANCHOR", "UNIT_DIR", "TRUSTED_OWNER_UID", "unit_main_pid", "geteuid")}

    def __enter__(self) -> Rig:
        board_identity.ANCHOR = self.tree
        board_identity.UNIT_DIR = self.tree / "units"
        board_identity.TRUSTED_OWNER_UID = os.getuid()
        board_identity.unit_main_pid = lambda unit: self.main_pid if unit == f"{PROJECT}-ticket-board.service" else 0
        board_identity.geteuid = lambda: 0
        return self

    def __exit__(self, *_exc: object) -> None:
        for name, value in self.saved.items():
            setattr(board_identity, name, value)
        for board in (self.unit, self.foreign_socket, self.foreign_url):
            if board is not None:
                board.close()
        shutil.rmtree(self.tree, ignore_errors=True)

    def config(self, *, board_url_override: str = "", **extra: Any) -> SimpleNamespace:
        return SimpleNamespace(project=PROJECT, board_socket=self.foreign_socket.where,
                               board_url=board_url_override or self.foreign_url.where, role_state_isolation=True,
                               roles=[], **extra)

    def foreign_asked(self) -> list[str]:
        return self.foreign_socket.asked + self.foreign_url.asked


def caught(call: Callable[[], Any]) -> Any:
    try:
        return call()
    except SystemExit as stop:
        return f"SystemExit: {stop.code}"
    except Exception as exc:  # noqa: BLE001 - what escaped is the finding
        return f"raised {type(exc).__name__}: {exc}"


#: Each reader, the path it asks, and what it answers when the unit's board answers.
READERS: list[tuple[str, Callable[[Any], Any], str, Any]] = [
    ("start's authority preflight", board_authority_preflight.process_authority_board_compatibility,
     "/api/runtime-assignments", (True, "process authority ready")),
    ("resume's socket answer", board_authority_preflight.board_socket_answer,
     "/api/runtime-assignments", ("answered", "")),
    ("adopt-workflow's declared workflow", board_workflow_readers.read_board_declared_workflow,
     "/api/workflow", ({"roles": [], "said_by": "unit"}, "")),
    ("adopt-workflow's workflow state", board_workflow_readers.read_board_workflow_state,
     "/api/workflow", (3, {"roles": [], "said_by": "unit"}, "")),
    ("resume's runtime assignments", provider_runtime_state.read_runtime_assignments,
     "/api/runtime-assignments", (set(), "")),
    ("the runtime assignment details", provider_runtime_state.read_runtime_assignment_details,
     "/api/runtime-assignments", ({}, "")),
    ("release-status's live build", release_alignment._live_board_build_id, "/api/board", ("unitbuild", "")),
    ("replace-window's assignments", presentation_runtime_assignments._resolved_runtime_assignments,
     "/api/runtime-assignments", ([], [])),
    ("recover's declared role", lambda config: presentation_role_recovery._declared_role(config, "main", opener=None),
     "/api/workflow", ({}, "")),
]


# -- root asks root's unit's board ------------------------------------------------------------------------------------
def test_root_asks_the_unit_board_never_the_configured_one() -> None:
    for name, reader, path, expected in READERS:
        with Rig() as rig:
            answer = caught(lambda: reader(rig.config()))
            check(answer == expected, f"{name}: as root, the unit's board's answer: {answer!r}")
            check(rig.unit.asked == [path], f"{name}: the unit's socket asked {path} once: {rig.unit.asked}")
            check(rig.foreign_asked() == [],
                  f"{name}: nothing the configuration names is connected to: {rig.foreign_asked()}")


def test_not_root_asks_the_configured_board_as_before() -> None:
    for name, reader, path, _expected in READERS:
        with Rig() as rig:
            board_identity.geteuid = lambda: 1000
            caught(lambda: reader(rig.config()))
            check(rig.unit.asked == [] and rig.foreign_asked() == [path],
                  f"{name}: not root, the configured board, as before: unit {rig.unit.asked} "
                  f"configured {rig.foreign_asked()}")


def test_a_caller_named_connection_is_still_used() -> None:
    """A caller's own connection is honoured, root or not: production passes none but an in-memory replay."""
    with Rig() as rig:
        used: list[str] = []

        class Conn:
            def request(self, method: str, path: str, **_kw: object) -> None:
                used.append(path)

            def getresponse(self) -> SimpleNamespace:
                return SimpleNamespace(status=200, read=lambda: json.dumps(answers("given")["/api/workflow"]).encode())

            def close(self) -> None:
                pass

        answer = board_workflow_readers.read_board_declared_workflow(rig.config(), connection_factory=lambda p, t: Conn())
        check(answer == ({"roles": [], "said_by": "given"}, "") and used == ["/api/workflow"]
              and rig.unit.asked == [] and rig.foreign_asked() == [],
              f"the caller's connection, and nobody else's: {answer} {used} {rig.unit.asked}")


def test_root_refuses_a_unit_socket_answered_by_anything_but_its_board() -> None:
    cases = [
        ("another process", {"main_pid": os.getpid() + 1}, "main process"),
        ("a unit that is not running", {"main_pid": 0}, "not running"),
    ]
    for label, change, said in cases:
        for name, reader, _path, expected in READERS:
            with Rig() as rig:
                rig.main_pid = change["main_pid"]
                answer = caught(lambda: reader(rig.config()))
                check(answer != expected and said in str(answer),
                      f"{label}, {name}: refused, saying so: {answer!r}")
                check(rig.foreign_asked() == [], f"{label}, {name}: nor is the configured board asked instead")
    for name, reader, _path, expected in READERS:
        with Rig(user="nobody") as rig:
            answer = caught(lambda: reader(rig.config()))
            check(answer != expected and "is answered by uid" in str(answer),
                  f"another account's answer, {name}: refused, saying so: {answer!r}")
            check(rig.foreign_asked() == [], f"another account, {name}: nor is the configured board asked")
    with Rig() as rig:
        os.chmod(rig.unit_file, 0o664)
        answer = board_authority_preflight.board_socket_answer(rig.config())
        check(answer[0] == "refused" and "writable only by it" in answer[1] and rig.unit.asked == [],
              f"a unit file another account can write is not root's word, and no reason to wait: {answer}")


def test_board_get_for_still_asks_who_answered() -> None:
    """`board_get_for` shares the peer check `unit_connection` makes (SYRD-769 moved it into `_check_peer`)."""
    for label, uid, pid, said in (("this board", os.getuid(), os.getpid(), ""),
                                  ("another process", os.getuid(), os.getpid() + 1, "main process"),
                                  ("another account", os.getuid() + 1, os.getpid(), "is answered by uid")):
        with Rig() as rig:
            get = board_identity.board_get_for(PROJECT, main_pid=lambda _unit, pid=pid: pid,
                                               service_uid=lambda _user, uid=uid: uid)
            answer = caught(lambda: get("", "/api/workflow"))
            if said:
                check(said in str(answer) and rig.unit.asked == [],
                      f"{label}: board_get_for refuses before asking anything: {answer!r} {rig.unit.asked}")
            else:
                check(answer == answers("unit")["/api/workflow"] and rig.unit.asked == ["/api/workflow"],
                      f"{label}: board_get_for asks it: {answer!r}")


def test_root_waits_while_the_unit_socket_is_silent() -> None:
    with Rig(unit_board=False) as rig:
        answer = board_authority_preflight.board_socket_answer(rig.config())
        check(answer[0] == "silent" and "does not answer" in answer[1] and rig.foreign_asked() == [],
              f"nothing on the unit's socket yet is silence, as for any socket, and only that socket: {answer}")
        build = release_alignment._live_board_build_id(rig.config())
        check(build[0] == "" and "does not answer" in build[1] and rig.foreign_asked() == [],
              f"and no live build is proven from the configured url instead: {build}")


def test_root_opener_asks_only_the_readers_own_route() -> None:
    """As root, the route asked is the one the reader appended, from a fixed list: nothing the tenant's url says before
    it -- a path, a query, a fragment -- is asked (Audit of 8f0874b0)."""
    with Rig() as rig:
        opener = board_identity.board_opener(rig.config())
        for url in ("http://example.invalid/etc/passwd", f"http://x/api/image/{rig.tree}/b.png",
                    "http://example.invalid:9/api/board?x=1", "http://x/api/tickets", "http://x/api/board/"):
            answer = caught(lambda: opener(url))
            check("refusing to ask" in str(answer) and rig.unit.asked == [],
                  f"{url}: a url not ending with one of root's routes is refused, and nothing is asked: {answer!r}")
        for url, route in ((f"http://x/api/image/{rig.tree}/b.png?/api/board", "/api/board"),
                           (f"http://x/api/image/{rig.tree}/b.png#/api/workflow", "/api/workflow"),
                           ("http://x:9/?q=/api/runtime-assignments", "/api/runtime-assignments")):
            rig.unit.asked.clear()
            with opener(url) as response:
                payload = json.load(response)
            check(payload == answers("unit")[route] and rig.unit.asked == [route] and rig.foreign_asked() == [],
                  f"{url}: exactly {route}, of the unit's board; nothing before it: {payload} {rig.unit.asked}")


def divergence_read(config: SimpleNamespace) -> Any:
    """replace-window's second read: the declared workflow, asked only when a configured role has no assignment."""
    missing = SimpleNamespace(role="main", detached=False, cli=["claude"], target=f"{PROJECT}-main:0.0")
    return presentation_runtime_assignments.runtime_divergence_refusal(
        SimpleNamespace(**{**vars(config), "roles": [missing]}),
        runner=lambda *_a, **_k: SimpleNamespace(returncode=1, stdout=""))


#: Each URL reader, and exactly the routes it asks.
URL_READERS: list[tuple[str, Callable[[Any], Any], list[str]]] = [
    ("release-status's live build", release_alignment._live_board_build_id, ["/api/board"]),
    ("replace-window's assignments", presentation_runtime_assignments._resolved_runtime_assignments,
     ["/api/runtime-assignments"]),
    ("replace-window's divergence read", divergence_read, ["/api/runtime-assignments", "/api/workflow"]),
    ("recover's declared role", lambda config: presentation_role_recovery._declared_role(config, "main", opener=None),
     ["/api/workflow"]),
    ("recover's registration wait", None, ["/api/runtime-assignments"]),
]


def test_a_tenant_url_cannot_choose_the_route_root_asks() -> None:
    """A board_url whose path or query points elsewhere -- at `/api/image/...`, which serves what the tenant wrote, or
    at a query of its own -- still has root ask each reader's own route of the unit's board, and nothing else."""
    for suffix in ("/api/image/{tree}/b.png?", "/api/image/{tree}/b.png#", "/api/image/{tree}/b.png?x=", "/?x=",
                   "/api/tickets"):
        for name, reader, routes in URL_READERS:
            with Rig() as rig:
                url = rig.foreign_url.where + suffix.format(tree=rig.tree)
                answer = (recovery_wait(rig, board_url=url) if reader is None
                          else caught(lambda: reader(rig.config(board_url_override=url))))
                check(rig.unit.asked == routes and rig.foreign_asked() == [],
                      f"{name}, board_url ...{suffix}: the unit's board asked exactly {routes}: {rig.unit.asked} "
                      f"(configured asked {rig.foreign_asked()})")
                check("TENANT-CHOSEN" not in str(answer) and "tenant" not in str(answer),
                      f"{name}, board_url ...{suffix}: nothing the tenant wrote is answered: {answer!r}")
                if name == "release-status's live build":
                    check(answer == ("unitbuild", ""), f"board_url ...{suffix}: the unit's board's build: {answer!r}")


def recovery_wait(rig: Rig, *, board_url: str = "") -> Any:
    """`present recover`'s bounded wait for the recovered role's registration, against a real ProjectConfig."""
    from scripts import team_launcher as tl

    (rig.tree / "bin").mkdir(exist_ok=True)
    cli = rig.tree / "bin" / "claude"
    cli.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    cli.chmod(0o755)
    (rig.tree / "work").mkdir(exist_ok=True)
    path = rig.tree / f"{PROJECT}.json"
    path.write_text(json.dumps({
        "project": PROJECT, "desktop_access": {"mode": "headless"}, "board_url": board_url or rig.foreign_url.where,
        "board_socket": rig.foreign_socket.where, "session_dir": str(rig.tree / "sessions"),
        "run_as_user": ME, "role_state_isolation": True,
        "roles": [{"role": "main", "cli": str(cli), "slot": 1, "workdir": str(rig.tree / "work")}],
    }), encoding="utf-8")
    config = tl.load_project_config(PROJECT, path)
    return caught(lambda: presentation_role_recovery.await_recovered_registration(
        config, config.roles[0], runner=lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="4242\n"),
        timeout=0, poll=0, sleep=lambda _s: None, monotonic=lambda: 0.0, print_func=lambda _line: None))


def test_the_recovery_wait_asks_the_unit_board_as_root() -> None:
    with Rig() as rig:
        answer = recovery_wait(rig)
        check("did not register" in str(answer) and rig.unit.asked == ["/api/runtime-assignments"]
              and rig.foreign_asked() == [],
              f"as root, the unit's board, never the configured url: {answer!r} {rig.unit.asked} {rig.foreign_asked()}")
    with Rig() as rig:
        board_identity.geteuid = lambda: 1000
        recovery_wait(rig)
        check(rig.unit.asked == [] and rig.foreign_asked() == ["/api/runtime-assignments"],
              f"not root, the configured url, as before: {rig.unit.asked} {rig.foreign_asked()}")
    for as_root in (True, False):
        with Rig(unit_raw=BAD_STATUS) as rig:
            if not as_root:
                board_identity.geteuid = lambda: 1000
                rig.foreign_url.raw = BAD_STATUS
            answer = recovery_wait(rig)
            check("could not be read" in str(answer) and controls(answer) == [],
                  f"{'root' if as_root else 'not root'}: a raw status line is refused as plain text: {answer!r}")


def test_the_root_opener_says_a_malformed_answer_in_its_own_words() -> None:
    with Rig(unit_raw=BAD_STATUS) as rig:
        answer = caught(lambda: board_identity.board_opener(rig.config())("http://x/api/board"))
        check(answer == f"raised OSError: {PROJECT}'s board answered /api/board with no valid HTTP (BadStatusLine)",
              f"the class and our own words, nothing of the status line: {answer!r}")
    with Rig(unit_raw=BAD_REASON) as rig:
        answer = caught(lambda: board_identity.board_opener(rig.config())("http://x/api/board"))
        check(answer == f"raised OSError: {PROJECT}'s board answered /api/board with HTTP 500",
              f"a non-200 answer is its status alone, never its reason phrase: {answer!r}")


# -- a board's raw HTTP words, shown inert ----------------------------------------------------------------------------
BAD_STATUS = f"HTTP/1.1 2{ESCAPE}\r\n\r\n".encode("utf-8")
BAD_REASON = f"HTTP/1.1 500 {ESCAPE}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode("utf-8")


def test_a_raw_status_line_is_shown_inert_by_every_socket_reader() -> None:
    for as_root in (False, True):
        with Rig(unit_raw=BAD_STATUS) as rig:
            if not as_root:
                board_identity.geteuid = lambda: 1000
                rig.foreign_socket.raw = BAD_STATUS
            for name, reader, _path, _expected in READERS:
                answer = caught(lambda: reader(rig.config()))
                check(controls(answer) == [],
                      f"{'root' if as_root else 'not root'}, {name}: a raw status line is shown inert: {answer!r}")


def test_a_reason_phrase_or_status_line_over_a_url_is_shown_inert() -> None:
    for raw in (BAD_STATUS, BAD_REASON):
        with Rig() as rig:
            board_identity.geteuid = lambda: 1000
            rig.foreign_url.raw = raw
            for name, reader in (
                ("release-status's live build", release_alignment._live_board_build_id),
                ("replace-window's assignments", presentation_runtime_assignments._resolved_runtime_assignments),
                ("recover's declared role",
                 lambda config: presentation_role_recovery._declared_role(config, "main", opener=None)),
            ):
                answer = caught(lambda: reader(rig.config()))
                check(controls(answer) == [] and not str(answer).startswith("raised"),
                      f"{name}: the board's raw words are refused as plain text: {answer!r}")
            said = caught(lambda: board_supervisor_migration.default_probe(rig.foreign_url.port, 1.0))
            check(isinstance(said, tuple) and not said[0] and controls(said) == [],
                  f"the supervisor migration's probe says it as plain text: {said!r}")


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_") and callable(value)]
    for test in tests:
        test()
        print(f"  {test.__name__}: ok")
    print(f"root_board_readers_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
