#!/usr/bin/env python3
"""SYRD-766: root's `switchyard status` asks root's own board, and shows what a board says as plain text.

`switchyard status` reads each project's pause switch (`project_status.read_work_pause`, SYRD-756) with a tokenless
`GET /api/work-pause` to the Unix socket the tenant's configuration names (`board_socket`). It printed the answer's
`reason`, `began_by`, `began_at` and held role names as they came. Run as root, that is a socket the tenant chose, and
text the tenant or its board chose -- terminal escapes included (an OSC title or clipboard write, a CSI that erases the
line it is on) -- into an operator's root terminal.

Here, as namespace root (`unshare --user --map-root-user`, euid 0), a "foreign" Unix socket the configuration names
answers with a pause full of escapes, and counts every connection:

- **Root, real code.** `read_work_pause` for a project with that configuration. Main's code connects to the foreign
  socket and returns its answer. The candidate asks only the board root's own unit names (`board_identity.
  board_get_for`); this project has no such unit, so it says nothing, and the foreign socket is never connected to.
- **Root, root's board.** With `board_get_for` standing in for a board root's unit names, answering the same escapes:
  the candidate asks it for this project, and still never the foreign socket.
- **What is printed.** The answer through `work_pause_lines` and `print`, as `switchyard status` prints it: main's
  output carries ESC, BEL and C1 bytes; the candidate's carries no control, format or bidirectional character, and
  the words are still there.
- **Not root.** As the account itself (no namespace), `read_work_pause` still reads the configured socket: a tenant
  reading its own board is unchanged.
- **Root's other readers of board text.** The same escapes as a board's word at each root-run site that printed one
  raw: the live build id (`release-status`, `deploy-release`, root's journal), the runtime-assignment probe's non-200
  body (`start`), a role's assigned target (`resume-provision`), the live workflow's role and `reassign` names
  (`adopt-workflow`), and a publication request's ref (`switchyard-publish-ref`, under sudo). Main's code prints each
  with its ESC, BEL and C1; the candidate prints plain text, or, for a publication request, refuses it showing the
  value escaped.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import unicodedata
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "5a890473dbdf1ef8ecf1f144b63601bcba61eccc"  # main before SYRD-766
PROJECT = "pie766"
ESCAPES = {"mode": "stop", "began_at": "2026-11-13T10:00:00\x1b[2K", "began_by": "\x1b]0;owned\x07director",
           "reason": "release \x1b]52;c;ZWNobyBvd25lZA==\x07broke\x1b[1A\x1b[2K hidden \x9b31m tail ‮evil",
           "held": {"main\x1b[31m": 2}}
ANSWER = {"paused": True, "pause": ESCAPES, "recent": []}
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        label = re.split(r": [\[{(]", detail, maxsplit=1)[0]
        raise AssertionError(f"{detail}\nFAILED CHECK: {label}")
    CHECKS += 1


class ForeignBoard:
    """A Unix socket that answers any request with ANSWER, as HTTP, and counts who connected."""

    def __init__(self, path: Path) -> None:
        self.path, self.connections = path, 0
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(path))
        self.server.listen(8)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.connections += 1
            with conn:
                conn.settimeout(3)
                try:
                    conn.recv(65536)
                except OSError:
                    pass
                body = json.dumps(ANSWER).encode()
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)

    def close(self) -> None:
        self.server.close()


def printed(project_status, answer) -> str:
    out = io.StringIO()
    with redirect_stdout(out):
        for line in project_status.work_pause_lines(PROJECT, answer):
            print(line)
    return out.getvalue()


def controls(text: str) -> list[str]:
    return sorted({f"U+{ord(c):04X}" for c in text if c != "\n" and unicodedata.category(c)[0] == "C"})


def inner(tree: Path) -> dict:
    sys.path.insert(0, str(tree))
    from scripts import project_status
    from scripts.ticket_board import board_identity

    out: dict = {"euid": os.geteuid()}
    with tempfile.TemporaryDirectory(prefix="syrd766.") as tmp:
        foreign = ForeignBoard(Path(tmp) / "foreign.sock")
        config = SimpleNamespace(project=PROJECT, board_socket=str(foreign.path))
        try:
            got = project_status.read_work_pause(config)
            out["real"] = {"answer": got, "foreign_connections": foreign.connections}
            out["real_printed"] = printed(project_status, got) if got else ""

            asked: list[str] = []

            def board_get_for(project, **_kwargs):
                asked.append(project)
                return lambda _url, path, *_a: ANSWER if path == "/api/work-pause" else None

            real_get_for = board_identity.board_get_for
            board_identity.board_get_for = board_get_for
            before_count = foreign.connections
            try:
                got = project_status.read_work_pause(config)
            finally:
                board_identity.board_get_for = real_get_for
            out["roots_board"] = {"answer": got, "asked": asked, "foreign_connections": foreign.connections - before_count}
            out["printed"] = printed(project_status, ANSWER)
        finally:
            foreign.close()
    return out


def not_root(tree: Path) -> dict:
    """As this account, in this process's child: the configured socket is still the board read."""
    code = (f"import sys, json; sys.path[:0] = [{str(tree)!r}, {str(Path(__file__).parent)!r}]\n"
            "from status_board_text_test import ForeignBoard, PROJECT\n"
            "from scripts import project_status\n"
            "import tempfile, os\nfrom pathlib import Path\nfrom types import SimpleNamespace\n"
            "with tempfile.TemporaryDirectory(prefix='syrd766u.') as tmp:\n"
            "    board = ForeignBoard(Path(tmp) / 'own.sock')\n"
            "    got = project_status.read_work_pause(SimpleNamespace(project=PROJECT, board_socket=str(board.path)))\n"
            "    print(json.dumps({'euid': os.geteuid(), 'paused': (got or {}).get('paused'), "
            "'connections': board.connections}))\n")
    done = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


SITES_PROBE = 'import importlib.machinery\nimport io\nimport json\nimport sys\nfrom contextlib import redirect_stderr\nfrom pathlib import Path\nfrom types import SimpleNamespace\n\ntree = Path(sys.argv[1])\nsys.path.insert(0, str(tree))\nESC = "x\\x1b]0;owned\\x07y\\x1b[2K\\x9b31m"\nout = {}\n\nfrom scripts import release_alignment\nconfig = SimpleNamespace(project="pie766", board_url="http://127.0.0.1:1", board_socket="/nonexistent", role_state_isolation=False, run_as_user="pie-agent")\n\n\nclass Resp(io.BytesIO):\n    def __enter__(self):\n        return self\n\n    def __exit__(self, *a):\n        return False\n\n\ntry:\n    out["build"] = release_alignment._live_board_build_id(config, opener=lambda url: Resp(json.dumps({"build_id": ESC}).encode()))\nexcept Exception as exc:\n    out["build"] = f"{type(exc).__name__}: {exc}"\n\nfrom scripts import board_authority_preflight\n\n\nclass Conn:\n    def request(self, *a, **k):\n        pass\n\n    def getresponse(self):\n        return SimpleNamespace(status=500, read=lambda: ESC.encode())\n\n    def close(self):\n        pass\n\n\nout["preflight"] = board_authority_preflight.process_authority_board_compatibility(SimpleNamespace(**{**vars(config), "role_state_isolation": True}), connection_factory=lambda p, t: Conn())\n\nfrom scripts import pane_liveness_checks\nrole = SimpleNamespace(role="main", target="pie766:main.0")\ntry:\n    out["liveness"] = pane_liveness_checks.pane_liveness(config, role, tmux_targets={"pie766:main.0"},\n                                                        assignments={"main": {"actual_target": ESC}}, owner_uid=None).why\nexcept Exception as exc:\n    out["liveness"] = f"{type(exc).__name__}: {exc}"\n\nfrom scripts import workflow_reconcile\ntry:\n    _merged, left = workflow_reconcile.with_role_added({"roles": [], "stages": [], "transitions": []},\n                                                       {"roles": [{"name": "main"}], "reassign": {ESC: "main"}}, "main")\n    out["reassign"] = left\nexcept Exception as exc:\n    out["reassign"] = f"{type(exc).__name__}: {exc}"\n\nsaid = []\ntry:\n    workflow_reconcile.reconcile_recorded_workflow(\n        "pie766", {"roles": []}, config=config, config_path=None, apply=False, from_live="", replacing="",\n        operator_name="root", board_reader=lambda c: ({"schema": "switchyard.workflow.v1", "roles": [{"name": ESC}], "stages": [{}], "transitions": [{}]}, ""), say=said.append,\n        write_record=lambda *a: Path("/nonexistent"), read_record=lambda s: (None, ""))\nexcept Exception as exc:\n    said.append(f"{type(exc).__name__}: {exc}")\nout["adopt"] = [line for line in said if "not one root will record" in line or "Error" in line]\n\nloader = importlib.machinery.SourceFileLoader("switchyard_publish_ref", str(tree / "scripts" / "switchyard-publish-ref"))\nref = loader.load_module()\nref.validate_project = lambda p: p\nref.board_unit = lambda p: {"board_url": "http://127.0.0.1:1"}\nref.require_control_caller = lambda url: "director"\nref.fetch_request = lambda url, rid: {"requested_by": "ops", "ref": "ops/" + ESC, "commit_hash": "a" * 40,\n                                      "bundle_path": "/tmp/b.bundle"}\nerr = io.StringIO()\ntry:\n    with redirect_stderr(err):\n        ref.main(["switchyard-publish-ref", "--project", "pie766", "--request", "1"])\n    out["publish_ref"] = "returned " + err.getvalue()\nexcept SystemExit as stop:\n    out["publish_ref"] = f"{stop.code} {err.getvalue()}"\nexcept Exception as exc:\n    out["publish_ref"] = f"{type(exc).__name__}: {exc}"\nprint(json.dumps(out))\n'


def sites(tree: Path) -> dict:
    """Each root-run site's printed text for a board word full of escapes, in a child importing `tree`'s code."""
    done = subprocess.run([sys.executable, "-c", SITES_PROBE, str(tree)], capture_output=True, text=True, timeout=120,
                          stdin=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin", "HOME": tempfile.gettempdir(),
                                                         "PYTHONDONTWRITEBYTECODE": "1"})
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-3000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


SITE_TEXT = {"build": lambda got: got["build"][0], "preflight": lambda got: got["preflight"][1],
             "liveness": lambda got: got["liveness"], "reassign": lambda got: " ".join(got["reassign"]),
             "adopt": lambda got: " ".join(got["adopt"]), "publish_ref": lambda got: got["publish_ref"]}


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts"], check=True,
                             capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path) -> dict:
    with tempfile.TemporaryDirectory(prefix="syrd766-stage.") as tmp:
        stage = Path(tmp)
        shutil.copy2(Path(__file__), stage / "suite.py")
        env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "DIRECTORCTL", "TMUX",
                                                                         "SWITCHYARD_"))}
        child = subprocess.run(["unshare", "--user", "--map-root-user", sys.executable, str(stage / "suite.py"),
                                "--inner", str(tree)], capture_output=True, text=True, timeout=300,
                               env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, stdin=subprocess.DEVNULL)
        assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
        out = json.loads(child.stdout.strip().splitlines()[-1])
    out["not_root"] = not_root(tree)
    out["sites"] = sites(tree)
    return out


def before(out: dict) -> None:
    check(out["euid"] == 0, f"before: the reads run as (namespace) root: {out['euid']}")
    check(out["real"]["foreign_connections"] >= 1 and (out["real"]["answer"] or {}).get("paused") is True,
          f"before, root: read_work_pause connects to the socket the tenant's configuration names and takes its "
          f"answer: {out['real']}")
    check("U+001B" in controls(out["real_printed"]) and "U+0007" in controls(out["real_printed"])
          and "U+009B" in controls(out["real_printed"]),
          f"before: what status prints carries ESC, BEL and C1 from the board's answer: {controls(out['real_printed'])}")
    for site, text in SITE_TEXT.items():
        shown = text(out["sites"])
        check("U+001B" in controls(shown), f"before, {site}: root prints the board's word with its escapes: {shown!r}")


def candidate(out: dict) -> None:
    check(out["euid"] == 0, f"the reads run as (namespace) root: {out['euid']}")
    check(out["real"]["foreign_connections"] == 0 and out["real"]["answer"] is None and out["real_printed"] == "",
          f"root, real code: the socket the tenant's configuration names is never connected to; with no board unit of "
          f"root's for this project, status says nothing about its pause: {out['real']}")
    roots = out["roots_board"]
    check(roots["asked"] == [PROJECT] and roots["foreign_connections"] == 0
          and (roots["answer"] or {}).get("paused") is True,
          f"root: the pause is asked of the board root's own unit names, for this project, and of nothing else: {roots}")
    text = out["printed"]
    check(controls(text) == [], f"what status prints holds no control, format or bidirectional character: "
                                f"{controls(text)} {text!r}")
    check(all(word in text for word in ("WORK PAUSED (stop now)", "director", "release", "broke", "hidden", "tail",
                                        "evil", "main", "(2)")),
          f"and the board's words are still shown: {text!r}")
    mine = out["not_root"]
    check(mine["euid"] != 0 and mine["connections"] >= 1 and mine["paused"] is True,
          f"not root: the account reads its configured socket as before: {mine}")
    for site, text in SITE_TEXT.items():
        shown = text(out["sites"])
        check(controls(shown) == [] and "owned" in shown,
              f"{site}: root prints the board's word as plain text, still readable: {shown!r}")
    check("holds a control character" in out["sites"]["publish_ref"] and "\\x1b" in out["sites"]["publish_ref"],
          f"publish_ref: a publication request whose ref holds a control character is refused, shown escaped: "
          f"{out['sites']['publish_ref']!r}")


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--inner":
        print(json.dumps(inner(Path(sys.argv[2])), default=str))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--dump":
        tree = ROOT if sys.argv[2] == "candidate" else tree_at(BEFORE, Path(tempfile.mkdtemp()))
        print(json.dumps(run(tree), indent=1, default=str))
        return 0
    after = run(ROOT)
    if os.environ.get("SYRD766_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd766-before.") as tmp:
            before(run(tree_at(BEFORE, Path(tmp) / "before")))
    candidate(after)
    print(f"status_board_text_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
