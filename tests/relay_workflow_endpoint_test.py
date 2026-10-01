#!/usr/bin/env python3
"""SYRD-535 Audit: the acceptance-relay command reads, validates and applies on ONE board.

The first candidate read the workflow from the client's HTTP board URL but sent
the dry run and the apply through `_post`, which prefers the Unix socket. With
both configured, it previewed one board and wrote that board's document into
another: Audit's reproduction applied board A's workflow (project "http-board")
to the socket's board. Now the read uses the same resolved endpoint as the
writes -- the socket where writes go to the socket -- and a socket that cannot
be read, or refuses, stops the command before any configure call, never falling
back to the HTTP board.

Two disposable boards record every request: board A over HTTP and board B over a
Unix socket, serving different projects' documents at the SAME revision. The
command is driven through `write_cli`'s own handler with a real
`TicketBoardWriteClient`. The reproduction runs the rejected candidate's code in
a child, from a git worktree.
"""

from __future__ import annotations

import copy
import json
import os
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REJECTED = "a0ef7738e8391e49d2dbee9e520734616601d862"  # the candidate Audit returned
REVISION = 7
PANE_ENV_PREFIXES = ("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def clean_env() -> dict[str, str]:
    for key in [k for k in os.environ if k.startswith(PANE_ENV_PREFIXES)]:
        os.environ.pop(key)
    return dict(os.environ)


def board_handler(name: str, document: dict, log: list, *, refuse_read: bool = False, vanish: Path | None = None):
    class Board(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # noqa: D401 - quiet
            return

        def reply(self, status: int, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            log.append(("GET", self.path, None))
            if self.path == "/api/workflow":
                if refuse_read:
                    return self.reply(403, {"error": f"{name} refuses"})
                self.reply(200, {"revision": REVISION, "document": document})
                if vanish is not None:
                    # Gone the moment after the read: the next connection fails.
                    vanish.unlink(missing_ok=True)
                return None
            return self.reply(404, {"error": "no"})

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            log.append(("POST", self.path, body))
            if self.path == "/api/register-caller":
                return self.reply(200, {"role": body.get("role")})
            if self.path.endswith("/actions/configure_workflow"):
                return self.reply(200, {"revision": REVISION + (0 if body.get("dry_run") else 1),
                                        "document": body.get("document"), "dry_run": body.get("dry_run")})
            return self.reply(404, {"error": "no"})

    return Board


class UnixHTTPServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def get_request(self):
        request, _ = super().get_request()
        return request, ("unix", 0)


def scenarios(tree: Path) -> dict:
    sys.path[:0] = [str(tree), str(tree / "tests")]
    from scripts.ticket_board import write_cli, write_client
    from workflow_document_eras import before_relaying

    sys.path.insert(0, str(ROOT / "tests"))
    from relay_user_acceptance_configuration_test import mefp_shaped

    def document(project: str) -> dict:
        doc = copy.deepcopy(mefp_shaped(before_relaying()))
        doc["project"] = project
        return doc

    seen: dict = {}
    with tempfile.TemporaryDirectory(prefix="syrd535e.") as raw:
        tmp = Path(raw)
        log_a, log_b = [], []
        http_a = HTTPServer(("127.0.0.1", 0), board_handler("board A", document("board-a"), log_a))
        threading.Thread(target=http_a.serve_forever, daemon=True).start()
        url_a = f"http://127.0.0.1:{http_a.server_port}"

        def socket_board(path: Path, log: list, **kwargs) -> UnixHTTPServer:  # noqa: D401
            server = UnixHTTPServer(str(path), board_handler("board B", document("board-b"), log, **kwargs))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            return server

        sock_b = tmp / "b.sock"
        server_b = socket_board(sock_b, log_b)

        def run(name: str, *, socket_path: str | None, apply: bool, expected: int | None, implicit: bool = False) -> None:
            log_a.clear()
            log_b.clear()
            client = write_client.TicketBoardWriteClient(url_a, caller_role="director", socket_path=socket_path,
                                                         socket_disabled=socket_path is None and not implicit)
            try:
                result = write_cli._add_user_acceptance_relay(client, apply=apply, expected_revision=expected)
                outcome = {"ok": True, "endpoint": result.get("endpoint"), "applied": result.get("applied")}
            except Exception as exc:  # noqa: BLE001
                outcome = {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:300]}
            configured = lambda log: [entry[2]["document"]["project"] for entry in log
                                      if entry[0] == "POST" and entry[1].endswith("/actions/configure_workflow")]
            seen[name] = {**outcome, "a": [e[:2] for e in log_a], "b": [e[:2] for e in log_b],
                          "a configured": configured(log_a), "b configured": configured(log_b)}

        run("socket preview", socket_path=str(sock_b), apply=False, expected=None)
        run("socket apply", socket_path=str(sock_b), apply=True, expected=REVISION)
        run("socket stale apply", socket_path=str(sock_b), apply=True, expected=REVISION - 1)
        run("socket missing", socket_path=str(tmp / "nobody.sock"), apply=True, expected=REVISION)
        refusing = tmp / "refusing.sock"
        server_r = socket_board(refusing, log_b, refuse_read=True)
        run("socket refuses the read", socket_path=str(refusing), apply=True, expected=REVISION)
        run("http only", socket_path=None, apply=True, expected=REVISION)
        # The socket resolved implicitly -- the ambient default, not an argument --
        # which is the case `_post` falls back to TCP for. Board B answers the
        # read and is gone before the dry run: the write must not land on A.
        vanishing = tmp / "vanishing.sock"
        server_v = socket_board(vanishing, log_b, vanish=vanishing)
        saved = write_client.DEFAULT_BOARD_URL, write_client.DEFAULT_BOARD_SOCKET
        write_client.DEFAULT_BOARD_URL, write_client.DEFAULT_BOARD_SOCKET = url_a, str(vanishing)
        try:
            run("implicit socket gone after the read", socket_path=None, apply=True, expected=REVISION, implicit=True)
        finally:
            write_client.DEFAULT_BOARD_URL, write_client.DEFAULT_BOARD_SOCKET = saved
        server_v.shutdown()
        server_v.server_close()
        for server in (http_a, server_b, server_r):
            server.shutdown()
            server.server_close()
    return seen


def main() -> int:
    clean_env()
    if len(sys.argv) == 3 and sys.argv[1] == "--scenarios":
        print("RESULT " + json.dumps(scenarios(Path(sys.argv[2]))))
        return 0

    def child(tree: Path) -> dict:
        proc = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenarios", str(tree)], text=True,
                              capture_output=True, env={**clean_env(), "PYTHONDONTWRITEBYTECODE": "1"})
        line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
        assert proc.returncode == 0 and line, proc.stdout[-3000:] + proc.stderr[-3000:]
        return json.loads(line[len("RESULT "):])

    with tempfile.TemporaryDirectory(prefix="syrd535e-before.") as raw:
        before_tree = Path(raw) / "tree"
        subprocess.run(["git", "-C", str(ROOT), "worktree", "add", "--detach", "-q", str(before_tree), REJECTED], check=True)
        try:
            before = child(before_tree)
        finally:
            subprocess.run(["git", "-C", str(ROOT), "worktree", "remove", "--force", str(before_tree)], check=False)
    split = before["socket apply"]
    check(split["ok"] and ["GET", "/api/workflow"] in split["a"] and split["b configured"]
          and set(split["b configured"]) == {"board-a"},
          f"reproduced: the rejected candidate read board A and wrote A's document into board B: {split}")

    now = child(ROOT)
    for name in ("socket preview", "socket apply"):
        result = now[name]
        check(result["ok"] and result["a"] == [] and result["endpoint"].startswith("unix:")
              and ["GET", "/api/workflow"] in result["b"] and set(result["b configured"]) == {"board-b"},
              f"{name}: read, validated{' and applied' if 'apply' in name else ''} on the socket's board alone, "
              f"with its own document: {result}")
    check(now["socket apply"]["applied"] is True and now["socket preview"]["applied"] is False,
          f"the apply applies and the preview does not: {now['socket apply']} {now['socket preview']}")
    for name in ("socket stale apply", "socket missing", "socket refuses the read"):
        result = now[name]
        check(not result["ok"] and result["a"] == [] and result["a configured"] == [] and result["b configured"] == [],
              f"{name}: refused before any configure call, and the HTTP board is never touched: {result}")
    check("not falling back" in now["socket missing"]["error"] and "refused the workflow read" in now["socket refuses the read"]["error"],
          f"and each says why: {now['socket missing']['error']} / {now['socket refuses the read']['error']}")
    gone = now["implicit socket gone after the read"]
    check(not gone["ok"] and gone.get("endpoint") is None and gone["a"] == [] and gone["a configured"] == []
          and "not falling back" in gone["error"],
          f"an implicit socket that vanishes after the read stops the command; nothing falls back to the HTTP board: {gone}")
    http = now["http only"]
    check(http["ok"] and http["applied"] is True and http["b"] == [] and set(http["a configured"]) == {"board-a"}
          and http["endpoint"].startswith("http://"),
          f"with no socket, HTTP still reads and writes the one HTTP board: {http}")
    print(f"relay_workflow_endpoint_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
