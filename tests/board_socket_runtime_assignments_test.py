#!/usr/bin/env python3
"""SYRD-684: a process-authority tenant's launch asks its board's socket for runtime assignments, and is answered.

SYRD-677 added `report_credentials` to `GET /api/runtime-assignments`, read from
an attribute only the HTTP server set. The same handler serves the board's Unix
socket, where the read raised, and the connection closed with no response.
Every launch of a process-authority tenant asks exactly that route over that
socket before it changes anything, so `switchyard pie` refused:

    team-launcher: refusing to launch pie before changing local state: its
    running board does not provide project-account process authority (Remote
    end closed connection without response)

and a start from stopped waited out its bound for a board that "never answered".

Here the board is the real board process (`scripts/ticket-board.py`, as its unit
runs it, with the unit's environment) on a production-built database, with a
real Unix socket and a real HTTP port. For a board holding a binding key, one
holding only the shared report secret, one holding neither, and one whose
binding key is its report secret, which is no key (SYRD-677):

* a stopped tenant's resume starts it with a stand-in `systemctl start` and
  waits until it answers (SYRD-670), and the launch that follows gets past its
  authority preflight;
* a running tenant's attach, start and reload get past it too;
* the socket answers HTTP 200 with the same `report_credentials`, project and
  authority mode as the HTTP port, and the board logs no traceback;
* the socket still refuses report filing, whatever the credential;
* every server attribute the handler reads directly is set by both servers.

The before-run is main's code, from a git archive.
"""

from __future__ import annotations

import ast
import io
import json
import os
import pwd
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BEFORE = "9dc08a7a87adc19c542d6786c9b0a3d95b8487db"  # main with SYRD-677, before SYRD-684
SECRET = "pie-report-secret-must-never-print"
KEY = "pie-binding-key-must-never-print"
MODES = ("attach", "attach-or-start", "reload")
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"{detail}\nFAILED CHECK: {detail.split(': ', 1)[0]}")
    CHECKS += 1


class PastPreflight(Exception):
    """The launch went on past its board-authority preflight: nothing after it runs here."""


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def handler_reads(tree: Path) -> dict[str, list[str]]:
    """Each server class's missing attributes among those the handler reads as `self.server.<name>`."""
    module = ast.parse((tree / "scripts/ticket_board/server.py").read_text())
    classes = {node.name: node for node in module.body if isinstance(node, ast.ClassDef)}
    read = {node.attr for node in ast.walk(classes["TicketBoardHandler"])
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
            and node.value.attr == "server" and isinstance(node.value.value, ast.Name) and node.value.value.id == "self"}

    def sets(name: str) -> set[str]:
        names: set[str] = set()
        for node in ast.walk(classes[name]):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target] if isinstance(node, ast.AnnAssign) else []
            for target in targets:
                for leaf in (target.elts if isinstance(target, ast.Tuple) else [target]):
                    if isinstance(leaf, ast.Attribute) and isinstance(leaf.value, ast.Name) and leaf.value.id == "self":
                        names.add(leaf.attr)
        return names

    return {name: sorted(read - sets(name)) for name in ("TicketBoardServer", "TicketBoardUnixServer")}


def scenario(tree: Path, credentials: str) -> dict:
    sys.path[:0] = [str(tree), str(tree / "tests")]
    import live_boundary

    live_boundary.isolate()
    import ticket_board_write_api_test as t
    from temporary_cluster import temporary_cluster
    from scripts import board_authority_preflight, board_services, tenant_release_deploy
    from scripts import team_launcher as launcher
    from scripts.ticket_board.write_client import UnixHTTPConnection

    out: dict = {"credentials": credentials}
    with temporary_cluster(prefix="syrd684-", shutdown="immediate") as cluster, \
            tempfile.TemporaryDirectory(prefix="s684.", dir="/tmp") as tmp:
        root = Path(tmp)
        admin = t.conninfo(cluster.socket_dir, cluster.port, "pie")
        t.run(["createdb", "-h", str(cluster.socket_dir), "-p", str(cluster.port), "-U", "postgres", "pie"])
        t.psql(admin, (tree / "scripts/ticket_board/schema.sql").read_text())
        if t.psql(admin, "SELECT count(*) FROM pg_roles WHERE rolname = 'director';") == "0":
            t.create_roles(admin)
        t.psql(admin, (tree / "scripts/ticket_board/rbac.sql").read_text())
        migrated = subprocess.run(["bash", str(tree / "scripts/ticket-board-migrate")], capture_output=True, text=True,
                                  env={**os.environ, "TICKET_BOARD_ADMIN_DATABASE_URL": admin}, timeout=300)
        assert migrated.returncode == 0, migrated.stderr[-800:]
        t.psql(admin, (tree / "scripts/ticket_board/rbac.sql").read_text())

        sock, port, log = root / "run" / "b.sock", free_port(), root / "board.log"
        (root / "director").mkdir()
        (root / "layout.json").write_text("{}\n")
        config_path = root / "pie.json"
        config_path.write_text(json.dumps({
            "project": "pie", "run_as_user": pwd.getpwuid(os.geteuid()).pw_name, "role_state_isolation": True,
            "layout": str(root / "layout.json"), "board_url": f"http://127.0.0.1:{port}", "board_socket": str(sock),
            "session_dir": str(root / "sessions"),
            "roles": [{"role": "director", "slot": 0, "cli": ["claude"], "target": "pie-director:0.0",
                       "workdir": str(root / "director")}],
        }) + "\n")
        config = launcher.load_project_config("pie", config_path)
        # The unit's command line, and what its environment and EnvironmentFile give the board.
        env = {"PATH": "/usr/bin:/bin", "HOME": str(root), "LANG": "C.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
               "TICKET_BOARD_DATABASE_URL": t.conninfo(cluster.socket_dir, cluster.port, "pie", t.SERVICE_ROLE),
               "TICKET_BOARD_PROJECT": "pie", "TICKET_BOARD_TICKET_PREFIX": "PIE",
               "TICKET_BOARD_PROCESS_AUTHORITY": "1", "TICKET_BOARD_TENANT_USER": config.run_as_user,
               "TICKET_BOARD_DIRECTORCTL": "/usr/bin/true",
               **({"TICKET_BOARD_TENANT_REPORT_TOKEN": SECRET} if credentials in ("bound", "shared", "same") else {}),
               **({"TICKET_BOARD_REPORT_BINDING_KEY": {"bound": KEY, "same": SECRET}[credentials]}
                  if credentials in ("bound", "same") else {})}
        argv = [sys.executable, str(tree / "scripts/ticket-board.py"), "--host", "127.0.0.1", "--port", str(port),
                "--unix-socket", str(sock), "--frames", str(root / "frames"), "--assets", str(root / "assets"),
                "--commit-git-dir", str(root)]
        board: list[subprocess.Popen] = []

        def runner(command, **_kwargs):
            if command[:2] == ["systemctl", "start"]:
                with log.open("ab") as sink:
                    board.append(subprocess.Popen(argv, env=env, stdout=sink, stderr=subprocess.STDOUT,
                                                  stdin=subprocess.DEVNULL, cwd=str(root)))
                return subprocess.CompletedProcess(command, 0, "", "")
            if command[:2] == ["systemctl", "is-active"]:
                up = bool(board) and board[-1].poll() is None
                return subprocess.CompletedProcess(command, 0 if up else 3, "active\n" if up else "inactive\n", "")
            return subprocess.CompletedProcess(command, 0, "", "")

        def launch(mode: str) -> dict:
            said: list[str] = []
            calls: list = []
            saved = launcher.refuse_restart_on_another_model

            def past(*_args, **_kwargs):
                raise PastPreflight

            launcher.refuse_restart_on_another_model = past
            try:
                result = launcher.launch_project(config, config_path=config_path, mode=mode,
                                                 script_path=tree / "scripts/team-launcher",
                                                 runner=lambda command, **_kw: calls.append(command), print_func=said.append)
            except PastPreflight:
                result = "past preflight"
            finally:
                launcher.refuse_restart_on_another_model = saved
            return {"result": result, "said": said, "calls": len(calls)}

        def resume() -> dict:
            said: list[str] = []
            began = time.monotonic()
            problems = launcher.resume_tenant(config, config_path=config_path, runner=runner, print_func=said.append)
            return {"problems": problems, "said": said, "took": round(time.monotonic() - began, 2)}

        saved = {n: getattr(launcher, n) for n in ("ensure_staged_role_tooling", "capture_listener_state",
                                                   "start_owner_listener")}
        saved_supervised = board_services.board_is_supervised
        saved_bound = (tenant_release_deploy.BOARD_READY_SECONDS, tenant_release_deploy.BOARD_READY_POLL_SECONDS)
        launcher.ensure_staged_role_tooling = lambda _config, **_kwargs: []
        launcher.capture_listener_state = lambda _config, **_kwargs: "active"
        launcher.start_owner_listener = lambda _config, **_kwargs: []
        board_services.board_is_supervised = lambda _project: False
        tenant_release_deploy.BOARD_READY_SECONDS, tenant_release_deploy.BOARD_READY_POLL_SECONDS = 15.0, 0.1
        try:
            # Stopped: the resume starts the board and waits for it; the launch then asks it for authority.
            out["stopped"] = {**resume(), "launch": launch("attach-or-start")}
            # Running: the resume finds it up, and each launch mode asks it.
            if not out["stopped"]["problems"] or board and board[-1].poll() is None:
                deadline = time.monotonic() + 15
                while not sock.exists() and time.monotonic() < deadline:
                    time.sleep(0.1)
                out["running"] = {**resume(), "launches": {mode: launch(mode) for mode in MODES}}
                out["compatibility"] = list(board_authority_preflight.process_authority_board_compatibility(config))
                out["socket_answer"] = list(board_authority_preflight.board_socket_answer(config))

                def ask(method: str, path: str, body: bytes | None = None, headers: dict | None = None) -> dict:
                    connection = UnixHTTPConnection(str(sock), timeout=5)
                    try:
                        connection.request(method, path, body=body, headers=headers or {})
                        response = connection.getresponse()
                        text = response.read().decode("utf-8", errors="replace")
                        return {"status": response.status, "type": response.getheader("Content-Type", ""),
                                "body": text}
                    except Exception as exc:  # noqa: BLE001 - what the client sees is the finding
                        return {"error": f"{type(exc).__name__}: {exc}"}
                    finally:
                        connection.close()

                out["socket"] = ask("GET", "/api/runtime-assignments")
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/runtime-assignments", timeout=5) as response:
                    out["http"] = {"status": response.status, "body": response.read().decode()}
                token = SECRET
                if credentials == "bound":
                    from scripts.ticket_board import report_follow_up
                    token = report_follow_up.bound_token(KEY, "pie")
                out["socket_file_report"] = ask(
                    "POST", "/api/tickets/actions/file_report",
                    body=json.dumps({"title": "x", "origin_project": "pie"}).encode(),
                    headers={"Content-Type": "application/json", t.REPORT_TOKEN_HEADER: token})
        finally:
            for n, value in saved.items():
                setattr(launcher, n, value)
            board_services.board_is_supervised = saved_supervised
            tenant_release_deploy.BOARD_READY_SECONDS, tenant_release_deploy.BOARD_READY_POLL_SECONDS = saved_bound
            for process in board:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
        out["log"] = log.read_text(errors="replace") if log.exists() else ""
        out["missing"] = handler_reads(tree)
    return out


def tree_at(commit: str, into: Path) -> Path:
    archive = subprocess.run(["git", "-C", str(ROOT), "archive", commit, "scripts", "config", "examples", "tests",
                              "skills", "docs"], check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(into, filter="data")
    return into


def run(tree: Path, credentials: str) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("TICKET_BOARD_", "PGU_TICKET_BOARD_", "DIRECTORCTL",
                                                                     "TMUX"))}
    child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--scenario", str(tree), credentials],
                           capture_output=True, text=True, env={**env, "PYTHONDONTWRITEBYTECODE": "1"}, timeout=240,
                           stdin=subprocess.DEVNULL, cwd=str(tree))
    assert child.returncode == 0, child.stdout[-2000:] + child.stderr[-4000:]
    got = json.loads(child.stdout.strip().splitlines()[-1])
    check(SECRET not in child.stdout + child.stderr and KEY not in child.stdout + child.stderr + got["log"],
          f"{credentials}: no report secret or binding key in any output")
    return got


def before(tree: Path) -> None:
    for credentials in ("bound", "shared", "none", "same"):
        got = run(tree, credentials)
        stopped = got["stopped"]
        check(stopped["problems"] and "never answered" in " ".join(stopped["problems"]) and stopped["took"] >= 14,
              f"before ({credentials}): a stopped tenant's resume waits out its bound for a board that never answers: "
              f"{stopped}")
        for mode, launched in got["running"]["launches"].items():
            check(launched["result"] == 1 and launched["calls"] == 0
                  and "Remote end closed connection without response" in launched["said"][0]
                  and "refusing to launch pie before changing local state" in launched["said"][0],
                  f"before ({credentials}): {mode} of the running tenant refuses, as the operator saw: {launched}")
        check(got["socket"].get("error", "").startswith("RemoteDisconnected")
              and got["socket_answer"][0] == "silent" and got["compatibility"][0] is False,
              f"before ({credentials}): the socket drops the request with no response: {got['socket']}")
        check("AttributeError: 'TicketBoardUnixServer' object has no attribute 'report_binding_key'" in got["log"],
              f"before ({credentials}): the board logs why: {got['log'][-600:]}")
        check(got["missing"] == {"TicketBoardServer": [], "TicketBoardUnixServer": ["report_binding_key"]},
              f"before ({credentials}): the socket server lacks an attribute the handler reads: {got['missing']}")


def candidate() -> None:
    for credentials, advertised in (("bound", "bound"), ("shared", "shared"), ("none", "shared"), ("same", "shared")):
        got = run(ROOT, credentials)
        stopped = got["stopped"]
        check(stopped["problems"] == [] and stopped["said"][0].startswith("started board")
              and stopped["launch"] == {"result": "past preflight", "said": [], "calls": 0},
              f"{credentials}: a stopped tenant's resume starts its board, finds it answering, and the launch gets past "
              f"its authority preflight: {stopped}")
        running = got["running"]
        check(running["problems"] == [] and running["said"][0].startswith("already running board"),
              f"{credentials}: the running tenant's board is found running: {running}")
        for mode, launched in running["launches"].items():
            check(launched == {"result": "past preflight", "said": [], "calls": 0},
                  f"{credentials}: {mode} of the running tenant gets past its authority preflight: {launched}")
        check(got["compatibility"] == [True, "process authority ready"] and got["socket_answer"] == ["answered", ""],
              f"{credentials}: both of the launch's probes are answered: {got['compatibility']} {got['socket_answer']}")
        sock, http = got["socket"], got["http"]
        check(sock.get("status") == 200 and sock.get("type", "").startswith("application/json"),
              f"{credentials}: the socket answers with structured HTTP, not a dropped connection: {sock}")
        answer, over_http = json.loads(sock["body"]), json.loads(http["body"])
        check(answer["report_credentials"] == over_http["report_credentials"] == advertised
              and answer["project"] == over_http["project"] == "pie"
              and answer["authority_mode"] == over_http["authority_mode"] == "process"
              and isinstance(answer["assignments"], dict),
              f"{credentials}: the socket and the HTTP port advertise the same: {answer} {over_http}")
        check("Traceback" not in got["log"] and "AttributeError" not in got["log"],
              f"{credentials}: the board logs no exception: {got['log'][-600:]}")
        refused = got["socket_file_report"]
        check(refused.get("status") == 403 and "only available over HTTP" in refused.get("body", ""),
              f"{credentials}: the socket still refuses report filing with its credential: {refused}")
        check(got["missing"] == {"TicketBoardServer": [], "TicketBoardUnixServer": []},
              f"{credentials}: both servers set every attribute the handler reads directly: {got['missing']}")


def main() -> int:
    if len(sys.argv) == 4 and sys.argv[1] == "--scenario":
        print(json.dumps(scenario(Path(sys.argv[2]), sys.argv[3]), default=str))
        return 0
    if os.environ.get("SYRD684_MUTATION_SKIP_BEFORE") != "1":
        with tempfile.TemporaryDirectory(prefix="syrd684-before.") as tmp:
            before(tree_at(BEFORE, Path(tmp) / "before"))
    candidate()
    print(f"board_socket_runtime_assignments_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
