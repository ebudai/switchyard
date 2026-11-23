#!/usr/bin/env python3
"""SYRD-853: the board and its listener run from an installed release, where `scripts` is no package.

bceacabf's deploy took syrd's notification listener into a crash loop, and
every `configure_workflow` failed on the board, both with "No module named
'scripts'". A release runs `scripts/ticket-board.py` and
`scripts/ticket-board-notify-listener`, which put the release's `scripts/` on
the path and import `ticket_board` as the package. A function-level `from
scripts.ticket_board.x import y` therefore imports only from the repository's
root, which is where every suite and every reviewer ran it.

1. Static: no module the board server or the listener can import holds a
   `scripts.`-qualified import, at module level or inside a function. "Can
   import" is the closure of every import, relative, sibling or
   package-absolute (`from ticket_board import x`, SYRD-856), from the two
   programs' own `ticket_board` entry modules. The one exception keeps a
   fallback for the release layout and records why. Each module outside the
   closure that holds one is listed with its reason, and the check fails if one
   of those becomes reachable.
2. At runtime: a release installed by `install-switchyard --apply`, as root in
   a user namespace, the way production installs one. That release's real
   board server and real listener run from it on a board built from that
   release's schema and migrations, with the Publisher, the research stage and
   the plan gate declared:
   - `configure_workflow` as a dry run, then applied, answers through the
     server's own socket;
   - the listener's pass reaches the Publisher, which runs only after the
     workflow validated (`validate_publisher`), and the Publisher records its
     own outcome for the ticket: "was not recorded", which a Publisher that
     failed on an import logs with no traceback, is refused (SYRD-856). The
     listener is still running.

`SYRD853_RELEASE=<commit>` installs that commit instead of HEAD. On bceacabf
both runtime checks fail.
"""

from __future__ import annotations

import live_boundary  # noqa: E402

live_boundary.isolate()  # no live home, pane store or runtime directory, here or in a child (SYRD-819)

import ast
import getpass
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "tests")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

from release_bytecode_boundary_test import as_root  # noqa: E402
from router_role_board_test import build_board, fixture, temporary_cluster  # noqa: E402
from scripts.ticket_board.plan_gate import with_plan_gate  # noqa: E402
from scripts.ticket_board.publisher_stage import with_publisher  # noqa: E402
from scripts.ticket_board.research_stage import with_research_stage  # noqa: E402
from scripts.ticket_board.write_client import TicketBoardWriteClient, TicketBoardWriteError  # noqa: E402

PACKAGE = ROOT / "scripts" / "ticket_board"
#: The programs a release runs the board and the listener with (their units' ExecStart).
SERVICE_PROGRAMS = ("ticket-board.py", "ticket-board-notify-listener")
#: (module, imported module) -> why the `scripts.`-qualified import is safe in a module the services can import.
EXCEPTIONS = {
    ("provision_role_tooling", "scripts.repository_hooks"):
        "not a ticket_board sibling: its ImportError fallback imports repository_hooks from scripts/ itself, which "
        "is on the path in the release layout",
}
#: module -> why it may keep `scripts.`-qualified imports: neither service can import it (checked below).
CLIENT_ONLY = {
    "design_stage": "the design stage's composer, run only by `switchyard design-stage` from the repository root",
    "router_setup": "Router's composer, run only by `switchyard router` and the launcher from the repository root",
    "runtime_catalog": "the runtime menu, imported only by router_setup and the repository-root launcher and setup commands",
    "terminal_select": "the launcher's terminal picker, imported only from the repository root",
}
CHECKS = 0


def check(condition: object, detail: str) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


# -- 1. the static guard ------------------------------------------------------------------------------------------


def imports(module: str, package: Path = PACKAGE) -> tuple[set[str], list[tuple[ast.stmt, str]]]:
    """The sibling modules `module` can import anywhere in its body, and its `scripts.`-qualified imports."""
    names = {p.stem for p in package.glob("*.py")}
    tree = ast.parse((package / f"{module}.py").read_text())
    siblings: set[str] = set()
    qualified: list[tuple[ast.stmt, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = node.module or ""
            if node.level == 1:
                siblings |= {target.split(".")[0]} if target else {alias.name for alias in node.names}
            elif target == "scripts.ticket_board":
                qualified.append((node, target))
                siblings |= {alias.name for alias in node.names}
            elif target.startswith("scripts."):
                qualified.append((node, target))
                if target.startswith("scripts.ticket_board."):
                    siblings.add(target.split(".")[2])
            elif target == "ticket_board":
                siblings |= {alias.name for alias in node.names}  # `from ticket_board import x` (SYRD-856)
            elif target.startswith("ticket_board."):
                siblings.add(target.split(".")[1])  # `from ticket_board.x import y`
            elif node.level == 0:
                siblings.add(target.split(".")[0])  # `from publisher_stage import ...`, outside the package
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "scripts" or alias.name.startswith("scripts."):
                    qualified.append((node, alias.name))
                    if alias.name.startswith("scripts.ticket_board."):
                        siblings.add(alias.name.split(".")[2])
                elif alias.name.startswith("ticket_board."):
                    siblings.add(alias.name.split(".")[1])  # `import ticket_board.x`
                else:
                    siblings.add(alias.name.split(".")[0])
    return siblings & names, qualified


def entry_modules(scripts: Path = ROOT / "scripts") -> set[str]:
    """The `ticket_board` modules the two service programs import."""
    found = set()
    for program in SERVICE_PROGRAMS:
        for node in ast.walk(ast.parse((scripts / program).read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("ticket_board."):
                found.add(node.module.split(".")[1])
    return found


def closure(entries: set[str], package: Path = PACKAGE) -> set[str]:
    seen: set[str] = set()
    todo = sorted(entries)
    while todo:
        module = todo.pop()
        if module not in seen:
            seen.add(module)
            todo.extend(sorted(imports(module, package)[0] - seen))
    return seen


def falls_back(tree: ast.Module, node: ast.stmt, target: str) -> bool:
    """Whether `node` sits in a `try` whose ImportError handler imports the same names from `target` without
    `scripts.`, as the release layout names it."""
    for parent in ast.walk(tree):
        if isinstance(parent, ast.Try) and any(held.lineno == node.lineno for held in parent.body):
            for handler in parent.handlers:
                caught = ast.unparse(handler.type) if handler.type else ""
                if "ImportError" in caught or "ModuleNotFoundError" in caught:
                    for alt in handler.body:
                        if (isinstance(alt, ast.ImportFrom) and alt.level == 0
                                and alt.module == target.removeprefix("scripts.")
                                and {a.name for a in alt.names} == {a.name for a in node.names}):
                            return True
    return False


def violations(package: Path = PACKAGE, scripts: Path = ROOT / "scripts") -> list[str]:
    """Every `scripts.`-qualified import a service can reach that is not a recorded, still-guarded exception."""
    found = []
    for module in sorted(closure(entry_modules(scripts), package)):
        tree = ast.parse((package / f"{module}.py").read_text())
        for node, target in imports(module, package)[1]:
            if (module, target) in EXCEPTIONS and falls_back(tree, node, target):
                continue
            found.append(f"{module}.py:{node.lineno} imports {target}")
    return found


def test_static() -> None:
    entries = entry_modules()
    check(entries == {"cli", "notify_listener"}, f"the service programs' entry modules: {sorted(entries)}")
    reachable = closure(entries)
    for must in ("server", "workflow_config", "publisher_stage", "research_stage", "plan_gate", "documentation_gate",
                 "designer_implementation", "publisher_pass"):
        check(must in reachable, f"the closure reaches {must}, as the services do")
    found = violations()
    check(not found, "no module the board server or the listener can import holds a scripts.-qualified import:\n  "
          + "\n  ".join(found))
    for (module, target), reason in EXCEPTIONS.items():
        tree = ast.parse((PACKAGE / f"{module}.py").read_text())
        held = [node for node, t in imports(module)[1] if t == target]
        check(reason and held and all(falls_back(tree, node, target) for node in held),
              f"the exception {module} -> {target} still exists and keeps its release-layout fallback")
    holding = {p.stem for p in PACKAGE.glob("*.py") if imports(p.stem)[1]}
    for module, reason in CLIENT_ONLY.items():
        check(reason and module in holding and module not in reachable,
              f"{module} holds scripts.-qualified imports and neither service can import it ({reason})")
    unexplained = sorted(holding - reachable - set(CLIENT_ONLY))
    check(not unexplained, f"every client-only module with scripts.-qualified imports is recorded: {unexplained}")

    # The guard sees what bceacabf shipped: a function-level import in a reachable module, and one a sibling
    # reaches only through another function-level import.
    with tempfile.TemporaryDirectory(prefix="syrd853-static.") as tmp:
        copy = Path(tmp) / "scripts"
        shutil.copytree(PACKAGE, copy / "ticket_board", ignore=shutil.ignore_patterns("__pycache__", "tests"))
        for program in SERVICE_PROGRAMS:
            shutil.copy(ROOT / "scripts" / program, copy / program)
        for module, before, after in (
                ("publisher_stage", "from .research_stage import PUBLISHER_FINDING_ACTIONS",
                 "from scripts.ticket_board.research_stage import PUBLISHER_FINDING_ACTIONS"),
                ("designer_implementation", "from .role_kinds import IMPLEMENTER, has_kind",
                 "from scripts.ticket_board.role_kinds import IMPLEMENTER, has_kind")):
            path = copy / "ticket_board" / f"{module}.py"
            text = path.read_text()
            check(text.count(before) == 1, f"{module} holds `{before}` once")
            path.write_text(text.replace(before, after))
            found = violations(copy / "ticket_board", copy)
            check(len(found) == 1 and found[0].startswith(f"{module}.py:"), f"the guard names {module}'s: {found}")
            path.write_text(text)
        # SYRD-856: a holder reachable only through a package-absolute import is followed and reported, in each form.
        holder = copy / "ticket_board" / "zz_holder.py"
        holder.write_text("def f():\n    from scripts.ticket_board.role_kinds import IMPLEMENTER\n    return IMPLEMENTER\n")
        stage = copy / "ticket_board" / "publisher_stage.py"
        text = stage.read_text()
        for form in ("from ticket_board import zz_holder", "from ticket_board.zz_holder import f",
                     "import ticket_board.zz_holder"):
            stage.write_text(text + f"\n\ndef _reach():\n    {form}  # noqa: F401\n")
            found = violations(copy / "ticket_board", copy)
            check(any(line.startswith("zz_holder.py:2 ") for line in found),
                  f"a holder reached only by `{form}` is reported: {found}")
        stage.write_text(text)
        holder.unlink()
        path = copy / "ticket_board" / "provision_role_tooling.py"
        text = path.read_text()
        path.write_text(text.replace("from repository_hooks import", "from repository_hooks_gone import"))
        found = violations(copy / "ticket_board", copy)
        check(found and found[0].startswith("provision_role_tooling.py:"),
              f"an exception that loses its fallback is a violation: {found}")


# -- 2. the release, at runtime -----------------------------------------------------------------------------------


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def install(commit: str, tmp: Path) -> Path:
    """The release `install-switchyard --apply` makes of `commit`, as root of a user namespace."""
    shared = tmp / "opt" / "switchyard"
    shim = tmp / "shim"
    shim.mkdir()
    for tool, code in (("systemctl", 1), ("tmux", 1)):  # no unit is touched, and no tmux server is reached
        (shim / tool).write_text(f"#!/bin/sh\nexit {code}\n")
        (shim / tool).chmod(0o755)
    env = {"SWITCHYARD_SOURCE_REPO": str(ROOT), "SWITCHYARD_SOURCE_REF": commit,
           "SWITCHYARD_SHARED_INSTALL_ROOT": str(shared), "SWITCHYARD_INSTALL_PATH": str(tmp / "bin" / "switchyard"),
           "PATH": f"{shim}:{os.environ.get('PATH', '/usr/bin:/bin')}"}
    done = as_root(f"exec bash {ROOT}/scripts/install-switchyard --apply", env, "022")
    release = shared / "releases" / commit
    check(done.returncode == 0 and (release / "scripts" / "ticket-board.py").is_file(),
          f"install-switchyard --apply installs {commit[:12]}: {done.stderr[-1200:]}")
    check(not (release / "scripts" / "__init__.py").exists(), "and in the release, scripts/ is no package")
    return release


def document() -> dict:
    """The example workflow with the research stage, the plan gate and the Publisher declared."""
    cfg = json.loads((ROOT / "examples/workflows/inspection.json").read_text())
    cfg["queue"] = {"stage": "analysis", "assignee": "director"}
    return with_plan_gate(with_research_stage(with_publisher(cfg), role="app"), owner="main")


def service_env(tmp: Path, **extra: str) -> dict[str, str]:
    """What a unit hands its service, and nothing of this run's: no PYTHONPATH, no repository root."""
    return {"PATH": f"{tmp / 'shim'}:/usr/bin:/bin", "HOME": str(tmp / "home"), "TMUX_TMPDIR": str(tmp / "tmux"),
            "LANG": "C.UTF-8", "TICKET_BOARD_PROJECT": "cerulean", "TICKET_BOARD_TICKET_PREFIX": "CER", **extra}


def stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def test_release(commit: str) -> None:
    with tempfile.TemporaryDirectory(prefix="syrd853-") as name, \
            temporary_cluster(prefix="syrd853-", shutdown="immediate") as cluster:
        tmp = Path(name)
        tmp.chmod(0o755)
        for sub in ("home", "tmux", "panes", "frames", "assets"):
            (tmp / sub).mkdir()
        release = install(commit, tmp)
        admin = build_board(cluster, "s853", before=release)  # that release's schema, migrations and grants
        service = fixture.conninfo(cluster.socket_dir, cluster.port, "s853", fixture.SERVICE_ROLE)
        listener_url = fixture.conninfo(cluster.socket_dir, cluster.port, "s853", "ticket_board_listener")
        port, sock = free_port(), tmp / "board.sock"
        python = ["/usr/bin/python3", "-E", "-s", "-B"]  # the units' interpreter flags
        board_log, listener_log = tmp / "board.log", tmp / "listener.log"
        processes: list[subprocess.Popen] = []
        try:
            with board_log.open("wb") as sink:
                processes.append(subprocess.Popen(
                    [*python, str(release / "scripts/ticket-board.py"), "--host", "127.0.0.1", "--port", str(port),
                     "--unix-socket", str(sock), "--frames", str(tmp / "frames"), "--assets", str(tmp / "assets")],
                    env=service_env(tmp, TICKET_BOARD_DATABASE_URL=service,
                                    TICKET_BOARD_ROLE_ACCOUNTS=f"director={getpass.getuser()}"),
                    stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd="/"))
            deadline = time.monotonic() + 120  # a hang guard: the board answers in about a second
            while not sock.exists() and processes[0].poll() is None and time.monotonic() < deadline:
                time.sleep(0.2)
            check(sock.exists(), f"the release's board serves its socket: {board_log.read_text()[-1500:]}")

            client = TicketBoardWriteClient(board_url=f"http://127.0.0.1:{port}", caller_role="director",
                                            socket_path=str(sock))
            cfg = document()
            try:
                dry = client.configure_workflow(cfg, expected_revision=0, dry_run=True)
            except TicketBoardWriteError as exc:
                dry = {"refused": str(exc)}
            check("refused" not in dry and dry.get("dry_run") is True,
                  f"configure_workflow's dry run answers from the release's board: {str(dry)[:800]}")
            try:
                applied = client.configure_workflow(cfg, expected_revision=0, dry_run=False)
            except TicketBoardWriteError as exc:
                applied = {"refused": str(exc)}
            check("refused" not in applied and fixture.psql(admin, "SELECT count(*) FROM ticket_board.workflow_configuration;")
                  != "0", f"and applied, the board holds the document: {str(applied)[:800]}")
            check("No module named" not in board_log.read_text(), f"the board logs no import failure: "
                  f"{board_log.read_text()[-1500:]}")

            fixture.seed_postgres_ticket(admin, "CER-1", title="Waiting for the Publisher", state="publication",
                                         assignee="publisher", audit_signoff=True, commit_hash="a" * 40)
            with listener_log.open("wb") as sink:
                processes.append(subprocess.Popen(
                    [*python, str(release / "scripts/ticket-board-notify-listener"), "--database", listener_url,
                     "--directorctl", "/usr/bin/true", "--pane-state-dir", str(tmp / "panes"), "--poll-seconds", "0.5",
                     "--reconnect-seconds", "30", "--board-url", f"http://127.0.0.1:{port}"],
                    env=service_env(tmp, TICKET_BOARD_URL=f"http://127.0.0.1:{port}"),
                    stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, cwd="/"))
            listener = processes[1]
            # SYRD-856: the Publisher's own outcome for CER-1, never "was not recorded": a Publisher that failed on
            # an import logs that line with no traceback (Audit's mutant on SYRD-853).
            outcome = re.compile(r"Publisher: CER-1 (?!was not recorded)(\S+)")
            deadline = time.monotonic() + 300  # a hang guard: a pass takes well under a second
            while (not outcome.search(listener_log.read_text()) and "Publisher: CER-1 was not recorded"
                   not in listener_log.read_text() and listener.poll() is None and time.monotonic() < deadline):
                time.sleep(0.2)
            said = listener_log.read_text()
            check(outcome.search(said) and "was not recorded" not in said and "No module named" not in said,
                  f"the release's listener passes validate_publisher and its Publisher records an outcome for "
                  f"CER-1: {said[-2000:]}")
            check(listener.poll() is None and "Traceback" not in said,
                  f"and is still running, with no traceback: {said[-2000:]}")
        finally:
            for process in processes:
                stop(process)
            as_root(f"rm -rf {tmp}/opt {tmp}/bin")


def main() -> int:
    test_static()
    commit = os.environ.get("SYRD853_RELEASE") or subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    test_release(commit)
    print(f"release_layout_imports_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
