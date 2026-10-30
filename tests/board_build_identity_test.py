#!/usr/bin/env python3
"""SYRD-703: a privileged action confirms its board's build from a fixed-size answer, at any board size.

SYRD-699 had `board_identity.board_get_for` read the proven board's whole snapshot, `/api/board`, for its
`build_id`, under the 8 MiB cap every answer has. mefp's snapshot is 12.5 MB and syrd's 24.1 MB, so every
`board_from_unit` action on them was refused, as "answered /api/board with HTTP 200".

Here, over a Unix socket the kernel proves (SO_PEERCRED, the effective unit's user and main process), main first
from a git archive and then this tree:

- the real board handler (`TicketBoardUnixServer`) with a snapshot over 8 MiB: the build check, and the helper's
  `authorizing_board`, pass without the snapshot being built; `/api/build` answers the build alone;
- a board whose release serves `/api/build` and answers it too large, without JSON, with a malformed or wrong
  build, with an HTTP failure or 404, is refused, each said as what it is, and never asked for anything else;
- a board on an exact release from before `/api/build` (every one since SYRD-699, mefp's among them) is proven from
  its bounded `/api/client-config`, main's real handler in a process of its own included, and its snapshot is never
  asked for; that answer oversized, malformed, missing, failing or naming a wrong build is refused; the write token
  it carries appears in no answer, refusal or state the check keeps;
- the release's own code decides which: root-owned and writable by no one else, and its route, not a mention;
- any other answer over the cap is said to be over the cap, not "HTTP 200".
"""

from __future__ import annotations

import http.server
import io
import json
import os
import pwd
import socketserver
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
#: Main's commit this ticket is cut from: the before-run's tree, and a release that predates `/api/build`.
BEFORE = "12398aabdd0456f0f180c86213ec623c7a985938"
SHA = "c" * 40
OTHER = "d" * 40
#: The write token a pre-/api/build board's client-config carries: it must surface nowhere.
TOKEN = "tok-SYRD703-do-not-surface-5b1f0c"
CHECKS = 0

#: Main's real board handler, in a process of its own: a pre-/api/build exact release whose snapshot is over 8 MiB.
OLD_BOARD = r'''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from scripts.ticket_board import server
socket_path, asked, built = Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4])
tickets = [{"id": f"P-{n}", "comments": ["x" * 4000]} for n in range(3000)]
class App:
    project, project_name, ticket_prefix, commit_git_dirs = "old", "Old", "O", ("/var/lib/old/commits.git",)
    def snapshot(self):
        built.write_text("built")
        return {"tickets": tickets}
real_get = server.TicketBoardHandler.do_GET
def recording_get(self):
    with asked.open("a") as log:
        log.write(self.path + "\n")
    return real_get(self)
server.TicketBoardHandler.do_GET = recording_get
board = server.TicketBoardUnixServer(socket_path, App(), events=object(), director_notifier=object(),
                                     role_authority=object())
print("ready", flush=True)
board.serve_forever()
'''


def check(condition: object, detail: object) -> None:
    global CHECKS
    assert condition, detail
    CHECKS += 1


def scenario(tree: Path, scratch: Path, legacy_server: Path, old_tree: Path) -> dict:
    sys.path[:0] = [str(tree)]
    from scripts.ticket_board import board_identity, privileged_actions, privileged_helper, server

    board_identity.TRUSTED_OWNER_UID = os.getuid()  # the suite's tree stands in for root's
    user = pwd.getpwuid(os.getuid()).pw_name
    out: dict = {"kept": []}
    number = [0]

    def release(serves: bool, *, writable: bool = False, mention: bool = False) -> Path:
        """A root release, as board_identity reads it: this tree's board code, or one from before /api/build --
        `mention`ing the route in a comment only, or `writable` by its group."""
        number[0] += 1
        where = scratch / f"r{number[0]}" / "releases" / SHA
        (where / "scripts" / "ticket_board").mkdir(parents=True)
        source = (tree / "scripts/ticket_board/server.py") if serves else legacy_server
        code = where / "scripts" / "ticket_board" / "server.py"
        code.write_bytes(source.read_bytes() + (b"\n# A later release answers GET /api/build.\n" if mention else b""))
        if writable:
            code.chmod(0o664)
        return where

    def kept(value: object, seen: set) -> list:
        """Every string reachable from `value` through closures and containers: what the check keeps."""
        if id(value) in seen:
            return []
        seen.add(id(value))
        if isinstance(value, (str, bytes)):
            return [value if isinstance(value, str) else value.decode("utf-8", "replace")]
        found: list = []
        if isinstance(value, dict):
            for item in value.items():
                found += kept(item, seen)
        elif isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                found += kept(item, seen)
        elif callable(value) and getattr(value, "__closure__", None):
            for cell in value.__closure__:
                try:
                    found += kept(cell.cell_contents, seen)
                except ValueError:  # an empty cell
                    pass
        return found

    def proven(name: str, socket_path: Path, release_dir: Path, pid: int | None = None) -> dict:
        """board_get_for's answer for a board on `socket_path` whose effective unit runs `release_dir`."""
        units = scratch / name / "units"
        units.mkdir(parents=True)
        (units / f"{name}-ticket-board.service").write_text(
            f"[Service]\nUser={user}\nExecStart=/root/python -E -s -B {release_dir}/scripts/ticket-board.py "
            f"--unix-socket {socket_path}\n", encoding="utf-8")
        runtime = board_identity.EffectiveUnit(
            user, ("/root/python", "-E", "-s", "-B", f"{release_dir}/scripts/ticket-board.py"), pid or os.getpid(),
            SHA)

        def get_for(project: str):
            return board_identity.board_get_for(project, unit_dir=units, owner_uid=os.getuid(), anchor=scratch,
                                                service_uid=lambda _user: os.getuid(),
                                                runtime_unit=lambda _project: runtime)

        try:
            get = get_for(name)
            answer: dict = {"ok": True}
            out["kept"] += kept(get, set())
        except board_identity.BoardIdentityError as exc:
            get, answer = None, {"refused": str(exc)}
            out["kept"] += [repr(exc), repr(exc.args), str(exc.__cause__), str(exc.__context__)]
        answer["get"] = get
        answer["get_for"] = get_for
        return answer

    def public(answer: dict) -> dict:
        return {k: v for k, v in answer.items() if k not in ("get", "get_for")}

    class Fake(http.server.BaseHTTPRequestHandler):
        """A board that answers each path from `routes`: (status, body)."""
        routes: dict = {}
        asked: list = []

        def do_GET(self):
            self.asked.append(self.path)
            status, body = self.routes.get(self.path, (404, b"Not found"))
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for at in range(0, len(body), 1 << 16):
                self.wfile.write(body[at:at + (1 << 16)])

        def log_message(self, *_args):
            pass

    def fake(name: str, routes: dict, *, serves: bool, **code) -> dict:
        socket_path = scratch / f"{name}.sock"
        handler = type(f"Fake_{name.replace('-', '_')}", (Fake,), {"routes": routes, "asked": []})
        board = socketserver.UnixStreamServer(str(socket_path), handler)
        threading.Thread(target=board.serve_forever, daemon=True).start()
        try:
            answer = proven(name, socket_path, release(serves, **code))
            if answer["get"] is not None and "/api/workflow" in routes:
                try:
                    answer["workflow"] = str(answer["get"]("ignored", "/api/workflow"))[:80]
                except board_identity.BoardIdentityError as exc:
                    answer["workflow"] = "refused: " + str(exc)
        finally:
            board.shutdown()
            board.server_close()
        answer["asked"] = handler.asked
        return public(answer)

    build = lambda value: json.dumps({"build_id": value}).encode()  # noqa: E731
    # Main's /api/client-config, as every exact release since SYRD-699 answers it, with the write token in it.
    config = lambda value, **extra: json.dumps({  # noqa: E731
        "build_id": value, "write_token": TOKEN, "project": "old", "project_name": "Old", "ticket_prefix": "O",
        "commit_repositories": ["/var/lib/old/commits.git"], **extra}).encode()
    # A snapshot over the 8 MiB cap, its build last, exactly as every release since SYRD-699 sends /api/board.
    tickets = [{"id": f"P-{n}", "comments": ["x" * 4000]} for n in range(3000)]
    big = json.dumps({"tickets": tickets, "project": "p", "build_id": SHA}).encode()
    out["big_board_bytes"] = len(big)

    # -- this tree's real board handler, its snapshot over 8 MiB --------------------------------------------------
    class App:
        project, project_name, ticket_prefix = "p703", "P703", "P"
        snapshots = 0

        def snapshot(self):
            App.snapshots += 1
            return {"tickets": tickets}

    os.environ["TICKET_BOARD_BUILD_ID"] = SHA
    socket_path = scratch / "real" / "board.sock"
    real = server.TicketBoardUnixServer(socket_path, App(), events=object(), director_notifier=object(),
                                        role_authority=object())
    threading.Thread(target=real.serve_forever, daemon=True).start()
    try:
        answer = proven("real", socket_path, release(True))
        out["real"] = public(answer)
        out["real_snapshots"] = App.snapshots
        action = privileged_actions.BY_NAME["upgrade-tenant"]
        try:
            privileged_helper.authorizing_board(action, "real", board_get_for=answer["get_for"])
            out["real_helper"] = "ok"
        except privileged_helper.Refused as exc:
            out["real_helper"] = "refused: " + str(exc)
        out["real_helper_snapshots"] = App.snapshots
        import socket as socket_module

        with socket_module.socket(socket_module.AF_UNIX, socket_module.SOCK_STREAM) as raw:
            raw.connect(str(socket_path))
            raw.sendall(b"GET /api/build HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")
            reply = b""
            while chunk := raw.recv(1 << 16):
                reply += chunk
        head, _, body = reply.partition(b"\r\n\r\n")
        out["build_route"] = {"status": head.split(b"\r\n")[0].decode(), "body": body.decode(errors="replace")[:300]}
        out["build_route_snapshots"] = App.snapshots
    finally:
        real.shutdown()
        real.server_close()

    # -- main's real board handler, a pre-/api/build exact release, in a process of its own -----------------------
    old_socket, asked, built = scratch / "old" / "board.sock", scratch / "old-asked.txt", scratch / "old-built.txt"
    child = subprocess.Popen([sys.executable, "-c", OLD_BOARD, str(old_tree), str(old_socket), str(asked), str(built)],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL, text=True,
                             env={**os.environ, "TICKET_BOARD_BUILD_ID": SHA, "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        out["old_ready"] = child.stdout.readline().strip()
        answer = proven("old", old_socket, release(False), pid=child.pid)
        out["old"] = public(answer)
        out["old_asked"] = asked.read_text().split() if asked.exists() else []
        out["old_built"] = built.exists()
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()

    # -- a board that serves /api/build, answering it badly -----------------------------------------------------
    for name, route in {
        "oversized": (200, json.dumps({"build_id": SHA, "pad": "x" * 8000}).encode()),
        "not-json": (200, b"build " + SHA.encode()),
        "not-a-string": (200, json.dumps({"build_id": 7}).encode()),
        "too-long": (200, build("e" * 200)),
        "bad-syntax": (200, build(SHA[:20] + "/../" + SHA[:16])),
        "wrong": (200, build(OTHER)),
        "failing": (500, b"boom"),
        "missing": (404, b"Not found"),
    }.items():
        out[f"bad_{name}"] = fake(f"b-{name}", {"/api/build": route, "/api/board": (200, big),
                                                "/api/client-config": (200, config(SHA))}, serves=True)

    # -- a board on a release from before /api/build -----------------------------------------------------------
    for name, route in {
        "ok": (200, config(SHA)),
        "oversized": (200, config(SHA, pad="x" * (20 << 10))),
        "not-json": (200, b"write_token=" + TOKEN.encode() + b" build_id=" + SHA.encode()),
        "no-build": (200, json.dumps({"write_token": TOKEN}).encode()),
        "not-a-string": (200, json.dumps({"build_id": [SHA], "write_token": TOKEN}).encode()),
        # The token is a well-formed build id (`secrets.token_urlsafe`): only its being wrong refuses it, and that
        # refusal must not repeat it (Audit on SYRD-703).
        "token-as-build": (200, json.dumps({"build_id": TOKEN, "write_token": TOKEN}).encode()),
        "token-only": (200, json.dumps({"build_id": TOKEN}).encode()),
        "wrong": (200, config(OTHER)),
        "failing": (500, b"boom " + TOKEN.encode()),
        "missing": (404, b"Not found"),
    }.items():
        out[f"compat_{name}"] = fake(f"c-{name}", {"/api/client-config": route, "/api/board": (200, big)},
                                     serves=False)
    out["compat_mention"] = fake("c-mention", {"/api/client-config": (200, config(SHA)), "/api/board": (200, big)},
                                 serves=False, mention=True)
    out["compat_writable"] = fake("c-writable", {"/api/client-config": (200, config(SHA)), "/api/board": (200, big)},
                                  serves=False, writable=True)

    # -- any other answer over the cap ----------------------------------------------------------------------------
    out["big_workflow"] = fake("workflow", {"/api/build": (200, build(SHA)), "/api/board": (200, build(SHA)),
                                            "/api/workflow": (200, big)}, serves=True)
    out["surfaced"] = [text[:120] for text in out.pop("kept") if TOKEN in text]
    return out


def tree_at(commit: str, into: Path) -> Path:
    into.mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "tests"], check=True,
                             capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, main_tree: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd703.", dir="/tmp") as scratch:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_"))}
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree), scratch,
                                str(main_tree / "scripts/ticket_board/server.py"), str(main_tree)],
                               capture_output=True, text=True, timeout=300,
                               env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, stdin=subprocess.DEVNULL, cwd=scratch)
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    return json.loads(child.stdout.strip().splitlines()[-1])


OVER_CAP_AS_200 = "answered /api/board with HTTP 200"


def common(got: dict) -> None:
    check(got["big_board_bytes"] > 8 << 20, f"the snapshot is over the 8 MiB cap: {got['big_board_bytes']}")
    check(got["old_ready"] == "ready", f"main's real board answers on its socket: {got['old_ready']!r}")


def before(got: dict) -> None:
    common(got)
    check(OVER_CAP_AS_200 in got["real"].get("refused", "") and got["real_snapshots"] == 1,
          f"main: a board over 8 MiB is refused, its whole snapshot built and called HTTP 200: {got['real']}")
    check(OVER_CAP_AS_200 in got["real_helper"], f"main: so is every board_from_unit action: {got['real_helper']}")
    check(got["build_route"]["status"].endswith("404 Not Found"), f"main: no /api/build: {got['build_route']}")
    check(OVER_CAP_AS_200 in got["old"].get("refused", "") and got["old_asked"] == ["/api/board"]
          and got["old_built"],
          f"main: likewise main's own board over 8 MiB, as mefp: {got['old']} {got['old_asked']}")
    check("HTTP 200" in got["big_workflow"].get("workflow", ""),
          f"main: any answer over the cap is said to be HTTP 200: {got['big_workflow']}")


def candidate(got: dict) -> None:
    common(got)
    check(got["real"].get("ok") and got["real_snapshots"] == 0,
          f"a board over 8 MiB is proven without its snapshot being built: {got['real']} {got['real_snapshots']}")
    check(got["real_helper"] == "ok" and got["real_helper_snapshots"] == 0,
          f"and the helper's board_from_unit authorization passes the same way: {got['real_helper']}")
    check(got["build_route"]["status"].endswith("200 OK") and json.loads(got["build_route"]["body"]) == {"build_id": SHA}
          and got["build_route_snapshots"] == 0,
          f"/api/build answers the build and nothing else, no token, no snapshot: {got['build_route']}")
    refused = {name[4:]: got[name].get("refused", "") for name in got if name.startswith("bad_")}
    expected = {
        "oversized": "answered /api/build with more than 4096 bytes",
        "not-json": "answered /api/build with no JSON",
        "not-a-string": "answered /api/build with no well-formed build id",
        "too-long": "answered /api/build with no well-formed build id",
        "bad-syntax": "answered /api/build with no well-formed build id",
        "wrong": f"reports build {OTHER}, not its effective root release {SHA}",
        "failing": "answered /api/build with HTTP 500",
        "missing": "answered /api/build with HTTP 404, though its release",
    }
    for name, said in expected.items():
        check(said in refused[name], f"a board serving /api/build that answers it {name} is refused as such: "
                                     f"{refused[name]!r}")
    check(all(got[f"bad_{name}"]["asked"] == ["/api/build"] for name in expected),
          f"and none of them is asked anything else, its snapshot or its client-config: "
          f"{[(n, got['bad_' + n]['asked']) for n in expected]}")
    check(got["old"].get("ok") and got["old_asked"] == ["/api/build", "/api/client-config"] and not got["old_built"],
          f"main's own board over 8 MiB, a pre-/api/build release, is proven from its client-config, its snapshot "
          f"never asked for or built: {got['old']} {got['old_asked']} {got['old_built']}")
    check(got["compat_ok"].get("ok") and got["compat_mention"].get("ok"),
          f"so is a pre-/api/build board, and one whose code only mentions the route: {got['compat_ok']} "
          f"{got['compat_mention']}")
    compat = {
        "oversized": f"answered /api/client-config with more than {16 << 10} bytes",
        "not-json": "answered /api/client-config with no JSON",
        "no-build": "answered /api/client-config with no well-formed build id",
        "not-a-string": "answered /api/client-config with no well-formed build id",
        "token-as-build": f"reports a build other than its effective root release {SHA}",
        "token-only": f"reports a build other than its effective root release {SHA}",
        "wrong": f"reports a build other than its effective root release {SHA}",
        "failing": "answered /api/client-config with HTTP 500",
        "missing": "answered /api/client-config with HTTP 404: its release predates /api/build",
    }
    for name, said in compat.items():
        check(said in got[f"compat_{name}"].get("refused", ""),
              f"a pre-/api/build board answering its client-config {name} is refused as such: {got['compat_' + name]}")
    check(all(got[f"compat_{name}"]["asked"] == ["/api/build", "/api/client-config"]
              for name in ["ok", "mention", *compat]),
          f"and none of them is asked for its snapshot: "
          f"{[(n, got['compat_' + n]['asked']) for n in ['ok', 'mention', *compat]]}")
    check(OTHER not in got["compat_wrong"].get("refused", ""),
          f"a wrong build from the client-config is not repeated, whatever it is: {got['compat_wrong']}")
    check(got["surfaced"] == [], f"the write token surfaces in no refusal and nothing the check keeps: {got['surfaced']}")
    writable = got["compat_writable"]
    check("is not owned by uid" in writable.get("refused", "") and writable["asked"] == ["/api/build"],
          f"a release whose board code another account can write decides nothing, and its board is refused before "
          f"anything else is asked: {writable}")
    check(f"answered /api/workflow with more than {8 << 20} bytes" in got["big_workflow"].get("workflow", "")
          and got["big_workflow"].get("ok"),
          f"any other answer over the cap is said to be over it, not HTTP 200: {got['big_workflow']}")


def marker() -> None:
    """What `_release_serves_build` reads from a release: this tree's board code carries it, main's does not."""
    from scripts.ticket_board import board_identity

    route = f'parsed.path == "{board_identity.BUILD_PATH}"'.encode()
    check(route in (ROOT / "scripts/ticket_board/server.py").read_bytes(),
          "this tree's server.py carries the /api/build route board_identity looks for in a release")
    main_server = subprocess.run(["git", "-C", str(ROOT), "show", f"{BEFORE}:scripts/ticket_board/server.py"],
                                 check=True, capture_output=True).stdout
    check(route not in main_server, "main's server.py does not")


def main() -> int:
    if len(sys.argv) == 6 and sys.argv[1] == "--scenario":
        got = scenario(Path(sys.argv[2]), Path(sys.argv[3]), Path(sys.argv[4]), Path(sys.argv[5]))
        print(json.dumps(got, default=str))
        return 0
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix="syrd703-before.", dir="/tmp") as tmp:
        main_tree = tree_at(BEFORE, Path(tmp) / "before")
        if os.environ.get("SYRD703_MUTATION_SKIP_BEFORE") != "1":
            before(run(main_tree, main_tree))
        marker()
        candidate(run(ROOT, main_tree))
    print(f"board_build_identity_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
