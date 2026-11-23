#!/usr/bin/env python3
"""SYRD-854: prove a release's served board and listener from its own installed tree, before a deploy switches to it.

bceacabf passed every suite, the reviews and the deploy's own checks, then crash-looped syrd's listener 31 s after
deploy-release: the served processes run with the release's `scripts/` on the path, where `scripts.ticket_board` is
no package, and no check ever ran them that way before they were live. The deploy's canary only asked a throwaway
board for `/api/board`; every workflow dry run goes to the running board, which is the old release until the switch.

This program runs the release the way its units do -- `<python> -E -s -B`, from the release directory, with nothing
of the repository on the path -- against a database of its own, and answers READY or NOT READY:

1. Every import statement in every module the board server or the listener can import, inside functions too,
   resolves in the release layout, and every one of those modules imports there. (An import that has an ImportError
   fallback beside it is the fallback's business.)
2. A throwaway PostgreSQL cluster, in a private directory and on a Unix socket only, is built the way provisioning
   builds a board: the release's schema.sql, its migrations (`ticket-board-migrate`), then its rbac.sql.
3. The release's real `scripts/ticket-board.py` serves it, and answers `configure_workflow` dry runs for the tenant's
   declared workflow and for that workflow with each optional composer the release ships -- the documentation gate,
   Router, the Publisher, the research stage and the plan gate -- composed by the release's own code. A composer
   that declines the workflow (it is already enabled, say) is reported and is not a failure; anything else is.
   The throwaway board is seeded with the default stages, and the tenant's live board has the stages its workflow
   left it (SYRD-866: sapi removed `inspection` long ago, and every dry run was refused for omitting it). So each
   trial, and the apply in 4, also removes the default stages the live board does not have -- on the throwaway board
   alone -- and a trial that omits a stage the live board does have is refused, as the live board would refuse it.
4. With the Publisher declared and one ticket waiting in publication, the release's real
   `scripts/ticket-board-notify-listener` establishes LISTEN and its Publisher records an outcome for that ticket,
   which it reaches only after the workflow validated (`validate_publisher`), and it is still running.

It runs as an unprivileged account (PostgreSQL refuses root): `run_as` is root's entry, which reads the tenant's
declared workflow from its database as `postgres` and starts this program as the tenant owner with `python3 -I`, so
it uses the standard library alone and nothing beside it. It changes nothing outside its own temporary directory,
which it removes. Any failure is NOT READY, with what failed and the traceback.
"""

from __future__ import annotations

import argparse
import ast
import http.client
import json
import os
import pwd
import re
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

#: The programs a release's units run the board and the listener with.
SERVICE_PROGRAMS = ("ticket-board.py", "ticket-board-notify-listener")
#: The optional composers a release may ship: name -> (module under scripts.ticket_board, function).
COMPOSERS = {
    "documentation gate": ("documentation_gate", "with_documentation_gate"),
    "Router": ("router_setup", "with_router"),
    "Publisher": ("publisher_stage", "with_publisher"),
    "research stage": ("research_stage", "with_research_stage"),
    "plan gate": ("plan_gate", "with_plan_gate"),
}
TICKET = "SMOKE-1"
CALLER_ROLE_HEADER = "X-Ticket-Board-Caller-Role"
#: Hang guards, each well past what the step takes on a loaded host; none is a limit on a healthy run.
START_SECONDS = 120.0
LISTENER_SECONDS = 180.0
STEP_SECONDS = 600.0


class NotReady(Exception):
    """One failed step: what failed, and its evidence."""


# -- 1. imports ---------------------------------------------------------------------------------------------------


def _guarded(tree: ast.Module) -> set[int]:
    """Line numbers of imports in a `try` that handles ImportError, and of the fallbacks in its handlers: either one
    is reached only when the other did not import."""
    lines = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Try) and any(
                handler.type is None or re.search(r"\b(ImportError|ModuleNotFoundError|Exception)\b",
                                                  ast.unparse(handler.type)) for handler in node.handlers):
            for statement in [*node.body, *(line for handler in node.handlers for line in handler.body)]:
                for inner in ast.walk(statement):
                    if isinstance(inner, (ast.Import, ast.ImportFrom)):
                        lines.add(inner.lineno)
    return lines


def import_targets(package: Path, module: str) -> tuple[set[str], list[tuple[int, str]]]:
    """A module's sibling modules (anywhere in its body), and its unguarded absolute imports as (line, dotted name).

    A relative import, or one of `ticket_board.x`, is a sibling; anything else is absolute, `scripts.` included."""
    tree = ast.parse((package / f"{module}.py").read_text(encoding="utf-8"))
    guarded = _guarded(tree)
    siblings: set[str] = set()
    absolute: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            name = node.module or ""
            if node.level > 1:
                absolute.append((node.lineno, "." * node.level + name))  # beyond the package: never resolves
            elif node.level == 1:
                siblings |= {name.split(".")[0]} if name else {alias.name for alias in node.names}
            elif name == "ticket_board":
                siblings |= {alias.name for alias in node.names}
            elif name.startswith("ticket_board."):
                siblings.add(name.split(".")[1])
            elif node.lineno not in guarded:
                absolute.append((node.lineno, name))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("ticket_board."):
                    siblings.add(alias.name.split(".")[1])
                elif node.lineno not in guarded:
                    absolute.append((node.lineno, alias.name))
    return siblings, absolute


def served_modules(scripts: Path) -> set[str]:
    """Every `ticket_board` module the two service programs can import: the closure of every import, anywhere."""
    package = scripts / "ticket_board"
    present = {path.stem for path in package.glob("*.py")}
    todo = []
    for program in SERVICE_PROGRAMS:
        for node in ast.walk(ast.parse((scripts / program).read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("ticket_board."):
                todo.append(node.module.split(".")[1])
    seen: set[str] = set()
    while todo:
        module = todo.pop()
        if module in seen or module not in present:
            continue
        seen.add(module)
        todo.extend(sorted(import_targets(package, module)[0] - seen))
    return seen


#: Run by the release's interpreter in the release layout: which top-level names do not resolve, and which served
#: modules do not import. argv: scripts dir, JSON {"top": [names], "modules": [names]}.
_RESOLVE = r"""
import importlib, importlib.util, json, sys, traceback
sys.path[0] = sys.argv[1]  # where a service program's own directory stands; never the working directory
asked = json.loads(sys.argv[2])
missing = [name for name in asked["top"] if importlib.util.find_spec(name) is None]
failed = {}
for module in asked["modules"]:
    try:
        importlib.import_module("ticket_board." + module)
    except BaseException:
        failed[module] = traceback.format_exc(limit=6)
print(json.dumps({"missing": missing, "failed": failed}))
"""


def check_imports(release: Path, python: str) -> list[str]:
    scripts = release / "scripts"
    modules = served_modules(scripts)
    if not modules:
        return [f"{scripts} names no ticket_board module for {' or '.join(SERVICE_PROGRAMS)}"]
    package = scripts / "ticket_board"
    problems, absolute = [], {}
    for module in sorted(modules):
        for line, name in import_targets(package, module)[1]:
            if name.startswith("."):
                problems.append(f"ticket_board/{module}.py:{line} imports {name}, beyond the package")
            else:
                absolute.setdefault(name.split(".")[0], []).append(f"ticket_board/{module}.py:{line} imports {name}")
    asked = json.dumps({"top": sorted(absolute), "modules": sorted(modules)})
    done = subprocess.run([python, "-E", "-s", "-B", "-c", _RESOLVE, str(scripts), asked], cwd=str(release),
                          capture_output=True, text=True, timeout=STEP_SECONDS)
    if done.returncode != 0:
        return problems + [f"the release's interpreter could not check its imports: {done.stderr.strip()[-1500:]}"]
    answer = json.loads(done.stdout)
    for top in answer["missing"]:
        problems += [f"{where}, and {top!r} does not import in the release layout" for where in absolute[top]]
    for module, trace in sorted(answer["failed"].items()):
        problems.append(f"ticket_board.{module} does not import in the release layout:\n{trace.rstrip()}")
    return problems


# -- 2. the database ----------------------------------------------------------------------------------------------


def _run(argv: list[str], what: str, **kwargs: Any) -> str:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=STEP_SECONDS, **kwargs)
    if done.returncode != 0:
        raise NotReady(f"{what} failed (exit {done.returncode}): {(done.stderr or done.stdout).strip()[-1500:]}")
    return done.stdout


def _dies_with_parent(sig: int) -> Callable[[], None]:
    """A child's last step before exec: `sig` when the smoke dies (PR_SET_PDEATHSIG), so nothing outlives a smoke
    that is killed before it can clean up."""
    def arm() -> None:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(1, sig)  # PR_SET_PDEATHSIG
    return arm


class Cluster:
    """A throwaway PostgreSQL cluster under `root`, reachable only on its Unix socket there.

    The postmaster is this process's own child, never `pg_ctl`'s daemon: if the smoke dies, it gets SIGQUIT (an
    immediate shutdown) instead of running on (Audit, SYRD-854)."""

    def __init__(self, root: Path):
        self.data, self.socket = root / "pg", root / "s"
        self.socket.mkdir()
        _run(["initdb", "-D", str(self.data), "-A", "trust", "--no-locale", "-E", "UTF8", "--username=postgres"],
             "initdb")
        self.log = root / "postgres.log"
        with self.log.open("wb") as sink:
            self.process = subprocess.Popen(
                ["postgres", "-D", str(self.data), "-k", str(self.socket), "-h", "", "-c", "fsync=off"],
                stdin=subprocess.DEVNULL, stdout=sink, stderr=subprocess.STDOUT,
                preexec_fn=_dies_with_parent(signal.SIGQUIT))  # noqa: PLW1509 - the point of it
        deadline = time.monotonic() + START_SECONDS
        while subprocess.run(["pg_isready", "-q", "-h", str(self.socket)], timeout=STEP_SECONDS).returncode != 0:
            if self.process.poll() is not None or time.monotonic() > deadline:
                raise NotReady(f"the throwaway cluster did not start:\n{tail(self.log)}")
            time.sleep(0.2)

    def url(self, database: str = "postgres", user: str = "postgres") -> str:
        return f"postgresql:///{database}?host={self.socket}&user={user}"

    def sql(self, statement: str, database: str = "board") -> str:
        return _run(["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", self.url(database), "-c", statement],
                    "psql").strip()

    def stop(self) -> None:
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGQUIT)  # an immediate shutdown: nothing here is kept
            try:
                self.process.wait(timeout=STEP_SECONDS)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()


def build_board(cluster: Cluster, release: Path) -> None:
    """The board as provisioning builds it (the operator packet's order), from the release's own files: the service
    and listener roles, the database, schema.sql, the migrations, then rbac.sql."""
    board = release / "scripts" / "ticket_board"
    cluster.sql("CREATE ROLE ticket_board_service LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION; "
                "CREATE ROLE ticket_board_listener LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;",
                database="postgres")
    _run(["createdb", "-h", str(cluster.socket), "-U", "postgres", "board"], "createdb")
    _run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", cluster.url("board"), "-f", str(board / "schema.sql")],
         "the release's schema.sql")
    _run(["bash", str(release / "scripts" / "ticket-board-migrate")], "the release's migrations",
         env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8",
              "TICKET_BOARD_ADMIN_DATABASE_URL": cluster.url("board")})
    _run(["psql", "-X", "-q", "-v", "ON_ERROR_STOP=1", cluster.url("board"), "-f", str(board / "rbac.sql")],
         "the release's rbac.sql")


# -- 3. the board -------------------------------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class UnixHTTPConnection(http.client.HTTPConnection):
    def __init__(self, path: str):
        super().__init__("localhost", timeout=STEP_SECONDS)
        self.path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


def post(sock: Path, path: str, payload: dict[str, Any], role: str = "director") -> tuple[int, Any]:
    """A write through the board's own socket, as `role`, the way the write client makes one."""
    conn = UnixHTTPConnection(str(sock))
    try:
        for target, body, header in (("/api/register-caller", {"role": role}, False), (path, payload, True)):
            headers = {"Content-Type": "application/json", **({CALLER_ROLE_HEADER: role} if header else {})}
            conn.request("POST", target, body=json.dumps(payload if header else body).encode(), headers=headers)
            response = conn.getresponse()
            text = response.read().decode("utf-8", "replace")
            if not header and response.status != 200:
                return response.status, text
        try:
            return response.status, json.loads(text)
        except ValueError:
            return response.status, text
    finally:
        conn.close()


#: Run by the release's interpreter as the repository-root CLI runs the composers (`scripts.ticket_board`, from the
#: release root). argv: release root, workflow JSON file. Prints {name: document | {"declined": why} | {"error": tb}}.
_COMPOSE = r"""
import importlib, json, sys, traceback
sys.path[0] = sys.argv[1]  # where the `switchyard` program's own directory stands
document = json.load(open(sys.argv[2]))
composers = json.loads(sys.argv[3])
roles = [r for r in document.get("roles", []) if r.get("active", True)]
try:
    from scripts.ticket_board.role_kinds import IMPLEMENTER, has_kind
    implementers = [r["name"] for r in roles if has_kind(r, IMPLEMENTER)]
except Exception:
    implementers = []
runtime = next((r["runtime"] for r in roles if r.get("runtime")), "claude")
owner = implementers[0] if implementers else None
arguments = {"with_router": {"runtime": runtime}, "with_research_stage": {"role": owner} if owner else {},
             "with_plan_gate": {"owner": owner} if owner else {}}
out = {}
for name, (module, function) in composers.items():
    try:
        compose = getattr(importlib.import_module("scripts.ticket_board." + module), function)
    except ModuleNotFoundError as exc:
        if exc.name == "scripts.ticket_board." + module:
            continue  # the release does not ship this composer
        out[name] = {"error": traceback.format_exc(limit=8)}
        continue
    except BaseException:
        out[name] = {"error": traceback.format_exc(limit=8)}
        continue
    try:
        out[name] = compose(json.loads(json.dumps(document)), **arguments.get(function, {}))
    except ValueError as exc:
        out[name] = {"declined": str(exc)}
    except BaseException:
        out[name] = {"error": traceback.format_exc(limit=8)}
print(json.dumps(out))
"""


def compose(release: Path, python: str, document: dict[str, Any], scratch: Path) -> dict[str, Any]:
    source = scratch / "declared.json"
    source.write_text(json.dumps(document))
    done = subprocess.run([python, "-E", "-s", "-B", "-c", _COMPOSE, str(release), str(source), json.dumps(COMPOSERS)],
                          cwd=str(release), capture_output=True, text=True, timeout=STEP_SECONDS)
    if done.returncode != 0:
        raise NotReady(f"the release's composers did not run: {done.stderr.strip()[-1500:]}")
    return json.loads(done.stdout)


def start(argv: list[str], *, env: dict[str, str], cwd: Path, log: Path) -> subprocess.Popen:
    with log.open("wb") as sink:
        return subprocess.Popen(argv, env=env, cwd=str(cwd), stdin=subprocess.DEVNULL, stdout=sink,
                                stderr=subprocess.STDOUT, start_new_session=True,
                                preexec_fn=_dies_with_parent(signal.SIGTERM))  # noqa: PLW1509


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def tail(path: Path, size: int = 3000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-size:]
    except OSError:
        return ""


# -- the smoke ----------------------------------------------------------------------------------------------------


def as_live(candidate: dict[str, Any], seeded: list[str], live: list[str]) -> tuple[dict[str, Any], list[str]]:
    """`candidate` as the tenant's live board takes it, tried on a board seeded with `seeded` (SYRD-866).

    Returns the candidate with the seeded stages the live board does not have added to its `remove_stages` (the
    throwaway board's alone: the tenant's document is not changed), and the live stages it omits without removing
    them, which the live board refuses ("configuration omits an existing stage")."""
    named = {stage.get("name") for stage in candidate.get("stages", [])}
    removed = list(candidate.get("remove_stages") or [])
    omitted = [name for name in live if name not in named and name not in removed]
    return {**candidate, "remove_stages": removed + [name for name in seeded
                                                       if name not in live and name not in removed]}, omitted


def smoke(release: Path, *, project: str, document: dict[str, Any] | None, python: str = "/usr/bin/python3",
          say: Callable[[str], None] = print, live_stages: list[str] | None = None) -> list[str]:
    """Every step against `release`; returns what failed, empty when it is READY. Runs as an unprivileged account.

    `live_stages` are the stages of the tenant's live board, in order; by default, the declared workflow's own,
    which a board that applied it has. With no declared workflow the example's board is the release's own."""
    release = release.resolve()
    problems = check_imports(release, python)
    if problems:
        return ["a module the board server or listener can import does not import from the release layout:"]\
            + problems
    say("release smoke: every module the board server and listener can import imports from the release layout")
    if document is None:
        # The example as it ships: its roles' targets name its own project, so the throwaway board takes that name
        # and `project` names nothing here (Audit, SYRD-854: renamed, every dry run was refused).
        document = json.loads((release / "examples" / "workflows" / "inspection.json").read_text(encoding="utf-8"))
        say(f"release smoke: the tenant declares no workflow; using the release's example workflow, whose board is "
            f"{document.get('project')!r}")
        live_stages = None
    elif live_stages is None:
        live_stages = [str(stage.get("name")) for stage in document.get("stages", [])]
    board_project = str(document.get("project") or project)
    scratch = Path(tempfile.mkdtemp(prefix="sy-smoke-", dir="/tmp"))  # short: a Unix socket path lives under it
    say(f"release smoke: working in {scratch}")
    processes: list[subprocess.Popen] = []
    cluster = None
    try:
        os.chmod(scratch, 0o700)
        for sub in ("home", "tmux", "panes", "frames", "assets", "shim"):
            (scratch / sub).mkdir()
        for tool in ("tmux", "systemctl"):  # no live tmux server or unit is ever reached from here
            (scratch / "shim" / tool).write_text("#!/bin/sh\nexit 1\n")
            (scratch / "shim" / tool).chmod(0o755)
        (scratch / "directorctl").write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {scratch}/delivered\n")
        (scratch / "directorctl").chmod(0o755)
        cluster = Cluster(scratch)
        build_board(cluster, release)
        say("release smoke: a throwaway board was built from the release's schema, migrations and grants")
        seeded = [name for name in cluster.sql("SELECT string_agg(name, ',' ORDER BY rank) "
                                               "FROM ticket_board.workflow_stages").split(",") if name]
        live = seeded if live_stages is None else list(live_stages)
        gone = [name for name in seeded if name not in live]
        if gone:
            say(f"release smoke: the tenant's live board has no {', '.join(gone)}; each trial removes "
                f"{'it' if len(gone) == 1 else 'them'} from the throwaway board")
        env = {"PATH": f"{scratch / 'shim'}:/usr/bin:/bin", "HOME": str(scratch / "home"), "LANG": "C.UTF-8",
               "TMUX_TMPDIR": str(scratch / "tmux"), "TMPDIR": str(scratch), "TICKET_BOARD_PROJECT": board_project}
        port, sock = _free_port(), scratch / "board.sock"
        account = pwd.getpwuid(os.getuid()).pw_name
        board_log = scratch / "board.log"
        processes.append(start(
            [python, "-E", "-s", "-B", str(release / "scripts" / "ticket-board.py"), "--host", "127.0.0.1",
             "--port", str(port), "--unix-socket", str(sock), "--frames", str(scratch / "frames"),
             "--assets", str(scratch / "assets")],
            env={**env, "TICKET_BOARD_DATABASE_URL": cluster.url("board", "ticket_board_service"),
                 "TICKET_BOARD_ROLE_ACCOUNTS": f"director={account}"},
            cwd=release, log=board_log))
        deadline = time.monotonic() + START_SECONDS
        while not sock.exists() and processes[0].poll() is None and time.monotonic() < deadline:
            time.sleep(0.2)
        if not sock.exists():
            raise NotReady(f"the release's board did not serve its socket:\n{tail(board_log)}")
        say("release smoke: the release's board serves")

        composed = compose(release, python, document, scratch)
        trials = [("the declared workflow", document)]
        for name, result in composed.items():
            if "error" in result:
                problems.append(f"the release's {name} composer failed on the declared workflow:\n{result['error']}")
            elif "declined" in result:
                say(f"release smoke: the {name} composer declines the declared workflow ({result['declined']})")
            else:
                trials.append((f"the declared workflow with the {name}", result))
        for label, candidate in trials:
            candidate, omitted = as_live(candidate, seeded, live)
            if omitted:
                problems.append(f"{label} omits the live board's stage{'s' if len(omitted) > 1 else ''} "
                                f"{', '.join(omitted)}, which its board refuses: configuration omits an existing "
                                "stage; preserve it or explicitly request removal")
                continue
            status, answer = post(sock, "/api/tickets/actions/configure_workflow",
                                  {"document": candidate, "expected_revision": 0, "dry_run": True})
            if status != 200 or not isinstance(answer, dict) or answer.get("dry_run") is not True:
                problems.append(f"configure_workflow's dry run of {label} failed (HTTP {status}): {str(answer)[-1500:]}")
            else:
                say(f"release smoke: configure_workflow dry-runs {label}")
        if problems:
            return problems + [f"the board's log:\n{tail(board_log)}"]

        publishing = next((doc for label, doc in trials if label.endswith("Publisher")), None) or document
        publishing = as_live(publishing, seeded, live)[0]
        status, answer = post(sock, "/api/tickets/actions/configure_workflow",
                              {"document": publishing, "expected_revision": 0, "dry_run": False})
        if status != 200:
            raise NotReady(f"applying the workflow with the Publisher failed (HTTP {status}): {str(answer)[-1500:]}")
        now = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
        source = json.dumps({"id": TICKET, "title": "The release smoke's ticket", "body": "", "state": "publication",
                             "assignee": "publisher", "comments": [], "created": now, "updated": now})
        cluster.sql("INSERT INTO ticket_board.tickets (id, title, state, "
                    "assignee, audit_signoff, commit_hash, created_text, updated_text, source_json) VALUES "
                    f"('{TICKET}', 'The release smoke''s ticket', 'publication', 'publisher', true, '{'0' * 40}', "
                    f"'{now}', '{now}', '{source.replace(chr(39), chr(39) * 2)}'::jsonb);")
        listener_log = scratch / "listener.log"
        listener = start(
            [python, "-E", "-s", "-B", str(release / "scripts" / "ticket-board-notify-listener"),
             "--database", cluster.url("board", "ticket_board_listener"), "--directorctl", str(scratch / "directorctl"),
             "--pane-state-dir", str(scratch / "panes"), "--poll-seconds", "0.5", "--reconnect-seconds", "30",
             "--board-url", f"http://127.0.0.1:{port}"],
            env={**env, "TICKET_BOARD_URL": f"http://127.0.0.1:{port}"}, cwd=release, log=listener_log)
        processes.append(listener)
        outcome = re.compile(rf"Publisher: {TICKET} (?!was not recorded)(\S+)")
        deadline = time.monotonic() + LISTENER_SECONDS
        while not outcome.search(tail(listener_log, 200000)) and listener.poll() is None \
                and time.monotonic() < deadline:
            time.sleep(0.2)
        said = tail(listener_log, 200000)
        found = outcome.search(said)
        if not found or listener.poll() is not None or "Traceback" in said or "No module named" in said:
            raise NotReady("the release's listener did not establish LISTEN and record a Publisher outcome for "
                           f"{TICKET} while staying up (exit {listener.poll()}):\n{said[-3000:]}")
        say(f"release smoke: the release's listener validated the workflow and its Publisher recorded "
            f"{found.group(1)} for {TICKET}")
        # The notice that outcome queued, claimed and routed by the listener's delivery path. No pane exists here,
        # so the pane gate fails closed and the notice waits: that is the gate working, and the smoke stops there.
        claimed = f"SELECT count(*) FROM ticket_board.notification_trace WHERE ticket_id = '{TICKET}' " \
                  "AND event = 'listener_claim'"
        deadline = time.monotonic() + LISTENER_SECONDS
        while cluster.sql(claimed) == "0" and listener.poll() is None and time.monotonic() < deadline:
            time.sleep(0.5)
        said = tail(listener_log, 200000)
        events = cluster.sql(f"SELECT string_agg(event || coalesce(':' || busy_reason, ''), ', ' ORDER BY id) "
                             f"FROM ticket_board.notification_trace WHERE ticket_id = '{TICKET}'")
        if cluster.sql(claimed) == "0" or listener.poll() is not None or "Traceback" in said:
            raise NotReady(f"the release's listener did not claim and route the notice {TICKET}'s outcome queued "
                           f"while staying up (exit {listener.poll()}; traced: {events or 'nothing'}):\n{said[-3000:]}")
        say(f"release smoke: the release's listener claimed and routed the notice it queued ({events}), "
            "and it is still running")
        return []
    except NotReady as exc:
        return problems + [str(exc)]
    finally:
        for process in processes:
            stop(process)
        if cluster is not None:
            cluster.stop()
        shutil.rmtree(scratch, ignore_errors=True)


# -- root's entry -------------------------------------------------------------------------------------------------


def run_as(release: Path, *, project: str, owner: str, database: str, python: str = "/usr/bin/python3",
           runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
           say: Callable[[str], None] = print) -> list[str]:
    """Root's entry: the tenant's declared workflow and its live board's stages from `database`, read as `postgres`,
    then this program as `owner`.

    Returns what failed, empty when the release is READY. Nothing live is written."""
    read = runner(["runuser", "-u", "postgres", "--", "env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8",
                   "psql", "-X", "-At", "-d", database, "-c",
                   "SELECT document::text FROM ticket_board.workflow_configuration"],
                  cwd="/", stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=STEP_SECONDS)
    if read.returncode != 0:
        return [f"reading {project}'s declared workflow from {database} failed (exit {read.returncode}): "
                f"{str(read.stderr or '').strip()[-600:]}"]
    declared = str(read.stdout or "").strip()
    stages = declared and runner(["runuser", "-u", "postgres", "--", "env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8",
                                  "psql", "-X", "-At", "-d", database, "-c",
                                  "SELECT name FROM ticket_board.workflow_stages ORDER BY rank"],
                                 cwd="/", stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 timeout=STEP_SECONDS)
    if stages and stages.returncode != 0:
        return [f"reading {project}'s live stages from {database} failed (exit {stages.returncode}): "
                f"{str(stages.stderr or '').strip()[-600:]}"]
    with tempfile.TemporaryDirectory(prefix=f"switchyard-{project}-smoke.") as hand:
        os.chmod(hand, 0o755)
        argv = ["runuser", "-u", owner, "--", "env", "-i", "PATH=/usr/bin:/bin", "LANG=C.UTF-8", "TMPDIR=/tmp",
                python, "-I", "-B", str(Path(__file__).resolve()), "--release", str(release), "--project", project,
                "--python", python]
        if declared:
            workflow = Path(hand) / "workflow.json"
            workflow.write_text(declared)
            workflow.chmod(0o644)
            argv += ["--workflow", str(workflow)]
            live = Path(hand) / "stages.json"
            live.write_text(json.dumps(str(stages.stdout or "").split()))
            live.chmod(0o644)
            argv += ["--stages", str(live)]
        done = runner(argv, cwd="/", stdin=subprocess.DEVNULL, capture_output=True, text=True,
                      timeout=STEP_SECONDS * 4)
    out = str(done.stdout or "")
    for line in out.splitlines():
        if line.startswith("release smoke: "):
            say(f"switchyard: {line}")
    if done.returncode == 0 and "release smoke: READY" in out.splitlines():
        return []  # only a smoke that said READY is one: an exit 0 that said nothing proved nothing
    report = out.split("release smoke: NOT READY\n", 1)
    return [report[1].strip() if len(report) == 2 else (str(done.stderr or out).strip()[-3000:]
                                                         or f"the release smoke exited {done.returncode} and never said "
                                                            "READY")]


def for_tenant(status: Any, project: str, release: Path, *,
               runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
               say: Callable[[str], None] = print) -> list[str]:
    """The smoke for a tenant's release: the database and interpreter its board unit names once rendered for
    `release` (`release_units.render_board_unit`), as the tenant owner. Root's, from preview-upgrade and
    deploy-release, before anything live changes. Returns what failed."""
    from scripts.ticket_board import database_admin, release_units

    try:
        template = Path(status.provisioned_system_unit).read_text(encoding="utf-8")
        exact = release_units.render_board_unit(template, release=release)
        database = database_admin.database_from_unit(exact)
        start = next(line for line in exact.splitlines() if line.startswith("ExecStart="))
        python = shlex.split(start.partition("=")[2])[0]
    except Exception as exc:  # the refusal is the useful message: no smoke means no READY
        return [f"the release smoke could not read {project}'s board unit for {release}: {exc}"]
    return run_as(release, project=project, owner=status.owner_user, database=database, python=python,
                  runner=runner, say=say)


def preview(status: Any, project: str, *, runner: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run,
            say: Callable[[str], None] = print) -> str:
    """preview-upgrade's smoke: '' when root's installed release of the pinned commit is READY, else why it is not.

    A tenant already on that release, or a status that resolved no target, has nothing to switch to; a release root
    has not installed is the preview's existing warning, and deploy-release refuses it."""
    if getattr(status, "unchanged", False) or not getattr(status, "target_sha", ""):
        return ""
    from scripts.ticket_board.privileged_operations import trusted_release_root

    try:
        release = Path(trusted_release_root(status.target_sha))
    except Exception as exc:  # already a warning of the preview's; deploy-release refuses the deploy
        say(f"switchyard: release smoke: not run, root has no installed release of {status.target_sha} ({exc})")
        return ""
    problems = for_tenant(status, project, release, runner=runner, say=say)
    if not problems:
        say(f"switchyard: release smoke: {status.target_sha} is READY")
        return ""
    for problem in problems:
        say(f"switchyard: release smoke: NOT READY: {problem}")
    return f"the release smoke found {status.target_sha} NOT READY: " + " ".join(problems[0].splitlines()[:3])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove a release's served board and listener before a deploy.")
    parser.add_argument("--release", required=True, help="the installed release directory")
    parser.add_argument("--project", required=True,
                        help="a name for the throwaway board (any name; nothing live is read)")
    parser.add_argument("--workflow", default="", help="the tenant's declared workflow (JSON); default: the example")
    parser.add_argument("--stages", default="",
                        help="the tenant's live board's stages (a JSON list); default: the declared workflow's own")
    parser.add_argument("--python", default="/usr/bin/python3", help="the interpreter the release's units name")
    args = parser.parse_args(argv)
    if os.geteuid() == 0:
        print("release smoke: runs as an unprivileged account (PostgreSQL refuses root); root uses run_as",
              file=sys.stderr)
        return 2
    # Root's `run_as` may give up on it (its timeout kills `runuser`): the smoke then gets SIGTERM, and a SIGTERM
    # unwinds it like an exception, so its cluster, board and listener are stopped and its directory removed.
    parent = os.getppid()
    _dies_with_parent(signal.SIGTERM)()
    if os.getppid() != parent:
        return 1
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    document = json.loads(Path(args.workflow).read_text(encoding="utf-8")) if args.workflow else None
    live = json.loads(Path(args.stages).read_text(encoding="utf-8")) if args.stages else None
    problems = smoke(Path(args.release), project=args.project, document=document, python=args.python,
                     say=lambda line: print(line, flush=True), live_stages=live)
    if problems:
        print("release smoke: NOT READY")
        print("\n".join(problems))
        return 1
    print("release smoke: READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
