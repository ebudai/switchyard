#!/usr/bin/env python3
"""SYRD-515: every way of starting `ticket-board-write` still sends the same writes.

The command line (parser, free text, dispatch) lives in `write_cli`, while the
client, endpoint resolution and transport stay in `write_client`. Four entry
points must keep reaching it and behave identically: the installed wrapper,
direct execution of `write_client.py`, a staged copy of the wrapper beside the
`ticket_board` tree (the layout role tooling installs), and
`write_client.main` imported from the package. This drives each one against a
local recording board and asserts the exact requests, output and refusals.

It also checks that the CLI reads the client module at call time, under both
package names: rebinding a name on `write_client` must still reach `main()`.

Every write goes to an explicit `--board-url` on 127.0.0.1, so no socket or
ambient endpoint is ever resolved.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

ADVERSARIAL = (
    "Backticks: `id -u` and $(hostname) and $HOME\n"
    "--text-file /etc/passwd\n"
    "Quotes: \"double\" 'single' \\backslash; trailing   \n"
    "Unicode: ✓ —\n"
)

#: What argparse prints as the program for each entry point.
PROGRAM_NAMES = {
    "wrapper": "ticket-board-write",
    "direct": "write_client.py",
    "staged": "ticket-board-write",
    "package": "-c",
}

CALLER_REQUIRED ="ticket board caller role required; pass --caller-role or set TICKET_BOARD_CALLER_ROLE\n"


class Recorder(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.requests: list[dict[str, object]] = []
        super().__init__(("127.0.0.1", 0), RecordingHandler)


class RecordingHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802 - http.server naming
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        self.server.requests.append(
            {
                "path": self.path,
                "role": self.headers.get("X-Ticket-Board-Caller-Role"),
                "token": self.headers.get("X-Ticket-Board-Write-Token"),
                "body": body,
            }
        )
        reply = json.dumps({"ticket": {"id": "REC-1", "path": self.path}, "merged": True}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(reply)))
        self.end_headers()
        self.wfile.write(reply)

    def log_message(self, *_args: object) -> None:
        return


def entry_points(staging: Path) -> dict[str, list[str]]:
    package_main = (
        "import sys; sys.path.insert(0, sys.argv.pop(1)); "
        "from scripts.ticket_board.write_client import main; raise SystemExit(main(sys.argv[1:]))"
    )
    return {
        "wrapper": [sys.executable, str(ROOT / "scripts" / "ticket-board-write")],
        "direct": [sys.executable, str(ROOT / "scripts" / "ticket_board" / "write_client.py")],
        "staged": [sys.executable, str(staging / "ticket-board-write")],
        "package": [sys.executable, "-c", package_main, str(ROOT)],
    }


def stage_bundle(staging: Path) -> None:
    """The role-tooling layout: the wrapper beside a whole copy of the package."""
    staging.mkdir()
    shutil.copy2(ROOT / "scripts" / "ticket-board-write", staging / "ticket-board-write")
    shutil.copytree(
        ROOT / "scripts" / "ticket_board",
        staging / "ticket_board",
        ignore=shutil.ignore_patterns("__pycache__"),
    )


def child_env(home: Path, *, role: str | None = "Main") -> dict[str, str]:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "TICKET_BOARD_WRITE_TOKEN": "entry-token",
    }
    if role is not None:
        env["TICKET_BOARD_CALLER_ROLE"] = role
    return env


def run(argv: list[str], env: dict[str, str], cwd: Path, stdin: str = "") -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(argv, env=env, cwd=cwd, input=stdin.encode("utf-8"), capture_output=True, timeout=60)


def exercise_entry(argv0: list[str], base: str, work: Path, home: Path, server: Recorder) -> list[object]:
    """Run one entry point through writes and refusals; return what the board and caller saw."""
    board = ["--board-url", base]
    text_file = work / "comment.txt"
    seen: list[object] = []

    def record(label: str, args: list[str], *, stdin: str = "", role: str | None = "Main") -> None:
        before = len(server.requests)
        proc = run(argv0 + board + args, child_env(home, role=role), work, stdin)
        seen.append(
            {
                "case": label,
                "rc": proc.returncode,
                "stdout": proc.stdout.decode("utf-8"),
                "stderr": proc.stderr.decode("utf-8"),
                "requests": server.requests[before:],
            }
        )

    record("comment from a file", ["add-comment", "syrd-1", "--text-file", str(text_file), "--urgent"])
    record("ticket body from stdin", ["create-ticket", "--title", "T", "--body", "-", "--draft"], stdin=ADVERSARIAL)
    record("override without notify", ["override-move", "SYRD-2", "--state", "backlog", "--assignee", "ops", "--no-notify"])
    record("merge prints the response", ["merge", "SYRD-3", "--target-id", "SYRD-4"])
    record("director edit types its values", ["director-edit", "SYRD-5", "--set", "needs_audit=false",
                                              "--set", "title=true story", "--set", "flag=true", "--reason", "r"])
    record("no caller role", ["add-comment", "SYRD-1", "--text", "x"], role=None)
    record("malformed --set", ["director-edit", "SYRD-1", "--set", "novalue", "--reason", "r"])
    record("malformed --json", ["edit-fields", "SYRD-1", "--json", "{"])
    record("empty required text", ["add-comment", "SYRD-1", "--text", "  "])
    return seen


def assert_entry_points_send_the_same_writes(tmp: Path) -> int:
    work, home, staging = tmp / "work", tmp / "home", tmp / "staging"
    work.mkdir()
    home.mkdir()
    (work / "comment.txt").write_text(ADVERSARIAL, encoding="utf-8")
    stage_bundle(staging)
    server = Recorder()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        results = {name: exercise_entry(argv0, base, work, home, server) for name, argv0 in entry_points(staging).items()}
    finally:
        server.shutdown()
        thread.join(timeout=5)

    wrapper = results["wrapper"]
    cases = {entry["case"]: entry for entry in wrapper}

    comment = cases["comment from a file"]
    assert comment["rc"] == 0, comment
    assert comment["requests"] == [
        {
            "path": "/api/tickets/SYRD-1/actions/add_comment",
            "role": "main",
            "token": "entry-token",
            "body": {"text": ADVERSARIAL, "urgent": True},
        }
    ], comment["requests"]
    assert json.loads(comment["stdout"]) == {"id": "REC-1", "path": "/api/tickets/SYRD-1/actions/add_comment"}

    created = cases["ticket body from stdin"]
    assert created["rc"] == 0, created
    [request] = created["requests"]
    assert request["path"] == "/api/tickets/actions/create_ticket", request
    assert request["body"]["body"] == ADVERSARIAL and request["body"]["state"] == "draft", request["body"]

    override = cases["override without notify"]
    [request] = override["requests"]
    assert request["path"] == "/api/tickets/SYRD-2/actions/override_move", request
    assert request["body"] == {"state": "backlog", "assignee": "ops", "suppress_notification": True}, request["body"]

    edited = cases["director edit types its values"]
    [request] = edited["requests"]
    assert request["body"] == {
        "patch": {"needs_audit": False, "title": "true story", "flag": True},
        "reason": "r",
    }, request["body"]

    merged = cases["merge prints the response"]
    assert json.loads(merged["stdout"])["merged"] is True, merged

    refusals = {
        "no caller role": (1, CALLER_REQUIRED),
        "malformed --set": (1, "--set expects FIELD=VALUE, got 'novalue'\n"),
        "malformed --json": (1, "invalid edit-fields --json: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)\n"),
    }
    for label, (rc, stderr) in refusals.items():
        entry = cases[label]
        assert (entry["rc"], entry["stderr"], entry["requests"]) == (rc, stderr, []), entry
    empty = cases["empty required text"]
    assert empty["rc"] == 2 and empty["requests"] == [], empty
    assert "--text (or --text-file) is required and must not be empty" in empty["stderr"], empty["stderr"]

    # argparse names the program after argv[0] and indents its usage lines to
    # match; apart from that, every entry point must match the installed wrapper.
    def normalised(name: str) -> list[object]:
        prog = PROGRAM_NAMES[name]

        def program_neutral(stderr: str) -> str:
            stderr = stderr.replace(f"usage: {prog} ", "usage: PROG ").replace(f"\n{prog}: error:", "\nPROG: error:")
            return " ".join(stderr.split())

        return [dict(entry, stderr=program_neutral(entry["stderr"])) for entry in results[name]]

    expected = normalised("wrapper")
    assert expected[-1]["stderr"].startswith("usage: PROG [-h]"), expected[-1]["stderr"]
    for name in results:
        for got, want in zip(normalised(name), expected, strict=True):
            assert got == want, (name, got, want)
    return len(results) * len(wrapper)


REBIND_PROBE = r"""
import importlib, json, sys
sys.path.insert(0, sys.argv[1])
package = sys.argv[2]
client = importlib.import_module(package + ".write_client")

def unreachable(*args, **kwargs):
    raise SystemExit("the CLI reached the original client instead of the rebound one")

# If the CLI ever holds its own reference to the client, fail here, never on a transport.
client.TicketBoardWriteClient._post = unreachable
client.TicketBoardWriteClient._post_report = unreachable
loaded_early = package + ".write_cli" in sys.modules
cli = importlib.import_module(package + ".write_cli")
seen = []

class Recording:
    def __init__(self, *args, **kwargs):
        seen.append(["init", list(args), sorted(kwargs.items())])

    def add_comment(self, ticket_id, **kwargs):
        seen.append(["add_comment", ticket_id, sorted(kwargs.items())])
        if kwargs["text"] == "refuse":
            raise client.TicketBoardWriteError("refused by the rebound client")
        return {"ticket": {"id": ticket_id}}

client.TicketBoardWriteClient = Recording
client.resolve_endpoint = lambda url, sock: ("http://rebound.invalid", None)
client.default_caller_role = lambda *args, **kwargs: "rebound-role"
client.DEFAULT_WRITE_TOKEN = "rebound-write-token"
client.DEFAULT_REPORT_TOKEN = "rebound-report-token"
ok = client.main(["add-comment", "X-1", "--text", "hello"])
refused = cli.main(["add-comment", "X-2", "--text", "refuse"])
print(json.dumps({"loaded_early": loaded_early, "same_module": cli.write_client is client,
                  "ok": ok, "refused": refused, "seen": seen}))
"""


def assert_cli_reads_the_client_module_at_call_time(tmp: Path) -> int:
    home = tmp / "rebind-home"
    home.mkdir()
    checks = 0
    for search_root, package in ((ROOT, "scripts.ticket_board"), (ROOT / "scripts", "ticket_board")):
        proc = run([sys.executable, "-c", REBIND_PROBE, str(search_root), package], child_env(home, role=None), tmp)
        assert proc.returncode == 0, (package, proc.stderr.decode("utf-8"))
        lines = proc.stdout.decode("utf-8").splitlines()
        report = json.loads(lines[-1])
        assert report["loaded_early"] is False, (package, "importing write_client must not load write_cli")
        assert report["same_module"] is True, package
        assert (report["ok"], report["refused"]) == (0, 1), (package, report)
        assert lines[0] == json.dumps({"id": "X-1"}), (package, lines)
        assert proc.stderr.decode("utf-8") == "refused by the rebound client\n", (package, proc.stderr)
        init = report["seen"][0]
        assert init[0] == "init" and init[1] == ["http://rebound.invalid", "rebound-role"], (package, init)
        kwargs = dict(init[2])
        assert kwargs["write_token"] == "rebound-write-token", (package, kwargs)
        assert kwargs["report_token"] == "rebound-report-token", (package, kwargs)
        assert kwargs["socket_disabled"] is True and kwargs["socket_path"] is None, (package, kwargs)
        assert report["seen"][1] == ["add_comment", "X-1", [["text", "hello"], ["urgent", False]]], (package, report)
        checks += 1
    return checks


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="syrd515-write-cli.") as tmpdir:
        tmp = Path(tmpdir)
        entries = assert_entry_points_send_the_same_writes(tmp)
        packages = assert_cli_reads_the_client_module_at_call_time(tmp)
    print(f"write_cli_entry_points_test: {entries} entry-point cases and {packages} package names ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
