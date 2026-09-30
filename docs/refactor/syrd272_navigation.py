#!/usr/bin/env python3
"""SYRD-272 navigation/review measurement: what a reviewer must read for six fixed tasks.

Reads git objects only (no checkout, no project code is imported or run), so
the same numbers come out of any clone that has both commits:

    python3 docs/refactor/syrd272_navigation.py [BEFORE] [AFTER]

Defaults are the exclusive-window baseline and the reviewed tree the final
report was written against. For each task and commit it resolves a fixed set
of seed symbols plus one level of Switchyard callees of the task's core
function, then reports:

  units       functions/methods/JS or shell functions a reviewer reads
  unit lines  the lines of those definitions (focused reading)
  files       distinct files holding them, and those files' whole lengths
              and bytes (what is loaded when files are read whole)
  hops        facade re-exports or forwarders crossed on the way (0 before)
  locate ms   median of 5 timed `git grep` runs that find every unit's
              definition in the commit's tree (a mechanical proxy only)

It measures reading scope, not human or agent turnaround.
"""

from __future__ import annotations

import ast
import json
import re
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass, field

BEFORE = "d5ffdd00a2b162ac9bee545f52ba0840f70fc7ed"
AFTER = "98d454806374513a1506647ff25ee429b51693cc"
LAUNCHER = "scripts/team_launcher.py"
#: How many calls deep a task's core function is followed, on both commits alike.
DEPTH = int(__import__("os").environ.get("SYRD272_DEPTH", "2"))


def show(commit: str, path: str) -> str | None:
    done = subprocess.run(["git", "show", f"{commit}:{path}"], capture_output=True, text=True)
    return done.stdout if done.returncode == 0 else None


def ls(commit: str, prefix: str) -> list[str]:
    out = subprocess.run(["git", "ls-tree", "-r", "--name-only", commit, "--", prefix], capture_output=True, text=True)
    return [line for line in out.stdout.splitlines() if line]


@dataclass
class Unit:
    path: str
    name: str
    start: int
    end: int
    kind: str = "def"

    @property
    def lines(self) -> int:
        return self.end - self.start + 1


@dataclass
class Result:
    units: list[Unit] = field(default_factory=list)
    hops: list[str] = field(default_factory=list)

    def add(self, unit: Unit | None) -> None:
        if unit and not any(u.path == unit.path and u.name == unit.name for u in self.units):
            self.units.append(unit)


class Tree:
    def __init__(self, commit: str) -> None:
        self.commit = commit
        self._text: dict[str, str | None] = {}
        self._ast: dict[str, ast.Module] = {}
        self.py_paths = [p for p in ls(commit, "scripts") if p.endswith(".py")]
        self._reexports: dict[str, str] | None = None

    def text(self, path: str) -> str | None:
        if path not in self._text:
            self._text[path] = show(self.commit, path)
        return self._text[path]

    def module(self, path: str) -> ast.Module:
        if path not in self._ast:
            self._ast[path] = ast.parse(self.text(path) or "")
        return self._ast[path]

    # -- python -------------------------------------------------------------
    def py_def(self, path: str, qualname: str) -> Unit | None:
        if self.text(path) is None:
            return None
        parts = qualname.split(".")
        body = self.module(path).body
        node = None
        for part in parts:
            node = next((n for n in body if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name == part), None)
            if node is None:
                return None
            body = node.body
        return Unit(path, qualname, node.lineno, node.end_lineno)

    def enclosing(self, path: str, needle: str) -> Unit | None:
        """The method or function whose body contains the first line matching `needle`."""
        text = self.text(path)
        if text is None:
            return None
        line = next(i for i, l in enumerate(text.splitlines(), 1) if needle in l)
        best = None
        for n in ast.walk(self.module(path)):
            if isinstance(n, ast.ClassDef):
                for m in n.body:
                    if isinstance(m, ast.FunctionDef) and m.lineno <= line <= m.end_lineno:
                        best = Unit(path, f"{n.name}.{m.name}", m.lineno, m.end_lineno)
        return best

    def reexports(self) -> dict[str, str]:
        """name -> module file, for names the launcher imports from Switchyard modules."""
        if self._reexports is None:
            table: dict[str, str] = {}
            for n in self.module(LAUNCHER).body:
                if isinstance(n, ast.ImportFrom) and n.module and n.module.startswith("scripts."):
                    path = n.module.replace(".", "/") + ".py"
                    for alias in n.names:
                        table[alias.asname or alias.name] = path
            self._reexports = table
        return self._reexports

    def imports(self, path: str) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
        """Module aliases and imported names of one file, including imports inside functions."""
        modules: dict[str, str] = {}
        names: dict[str, tuple[str, str]] = {}
        package = path.rsplit("/", 1)[0]
        for n in ast.walk(self.module(path)):
            if isinstance(n, ast.ImportFrom):
                if n.level:
                    base = package if n.level == 1 else package.rsplit("/", n.level - 1)[0]
                    target = f"{base}/{n.module.replace('.', '/')}" if n.module else base
                elif n.module and n.module.startswith("scripts"):
                    target = n.module.replace(".", "/")
                elif n.module and f"scripts/{n.module.replace('.', '/')}.py" in self.py_paths:
                    target = f"scripts/{n.module.replace('.', '/')}"
                else:
                    continue
                for alias in n.names:
                    local = alias.asname or alias.name
                    if f"{target}/{alias.name}.py" in self.py_paths:
                        modules.setdefault(local, f"{target}/{alias.name}.py")
                    elif f"{target}.py" in self.py_paths:
                        names.setdefault(local, (f"{target}.py", alias.name))
            elif isinstance(n, ast.Import):
                for alias in n.names:
                    if alias.name.startswith("scripts.") and alias.name.replace(".", "/") + ".py" in self.py_paths:
                        modules.setdefault(alias.asname or alias.name.split(".")[-1], alias.name.replace(".", "/") + ".py")
        return modules, names

    def definition(self, path: str, name: str, hops: list[str]) -> Unit | None:
        """Where `name`, reached through module `path`, is really defined; each re-export crossed is a hop."""
        for _ in range(4):
            unit = self.py_def(path, name)
            if unit:
                return unit
            if path == LAUNCHER and name in self.reexports():
                target = self.reexports()[name]
            else:
                imported = self.imports(path)[1].get(name)
                if not imported:
                    return None
                target, name = imported
            hops.append(f"{path}:{name} -> {target}")
            path = target
        return None

    def resolve(self, from_path: str, call: ast.Call) -> tuple[Unit | None, list[str]]:
        """A Switchyard callee's definition, and the re-export hops taken to find it."""
        func = call.func
        modules, names = self.imports(from_path)
        hops: list[str] = []
        if isinstance(func, ast.Name):
            local = self.py_def(from_path, func.id)
            if local:
                return local, hops
            if func.id in names:
                target, name = names[func.id]
                return self.definition(target, name, hops), hops
            return None, hops
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner, name = func.value.id, func.attr
            target = LAUNCHER if owner == "launcher" else modules.get(owner)
            if target:
                return self.definition(target, name, hops), hops
        return None, hops

    def expand(self, result: Result, unit: Unit, *, self_class: str | None = None, depth: int = DEPTH) -> None:
        """Add the Switchyard callees of one Python unit, `depth` calls deep."""
        if depth <= 0 or unit.kind != "def":
            return
        node = next(n for n in ast.walk(self.module(unit.path))
                    if isinstance(n, ast.FunctionDef) and n.lineno == unit.start)
        cls = self_class if "." in unit.name else None
        for call in (c for c in ast.walk(node) if isinstance(c, ast.Call)):
            func = call.func
            if cls and isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "self":
                callee, hops = self.py_def(unit.path, f"{cls}.{func.attr}"), []
            else:
                callee, hops = self.resolve(unit.path, call)
            if not callee or callee.name == unit.name and callee.path == unit.path:
                continue
            fresh = not any(u.path == callee.path and u.name == callee.name for u in result.units)
            result.add(callee)
            for hop in hops:
                if hop not in result.hops:
                    result.hops.append(hop)
            if fresh:
                self.expand(result, callee, self_class=self_class, depth=depth - 1)

    # -- shell and javascript ---------------------------------------------
    def braced(self, path: str, pattern: str, name: str, kind: str) -> Unit | None:
        text = self.text(path)
        if text is None:
            return None
        lines = text.splitlines()
        start = next((i for i, l in enumerate(lines) if re.search(pattern, l)), None)
        if start is None:
            return None
        depth = 0
        for j in range(start, len(lines)):
            depth += lines[j].count("{") - lines[j].count("}")
            if depth == 0 and j > start or (depth == 0 and "}" in lines[j]):
                return Unit(path, name, start + 1, j + 1, kind)
        return None

    def sh_fn(self, path: str, name: str) -> Unit | None:
        return self.braced(path, rf"^{re.escape(name)}\(\) \{{", name, "sh")

    def js_fn(self, path: str, name: str) -> Unit | None:
        return self.braced(path, rf"function {re.escape(name)}\(", name, "js")


def first(*units: Unit | None) -> Unit | None:
    return next((u for u in units if u), None)


# -- the six tasks ------------------------------------------------------------
def launcher_release_status(t: Tree) -> Result:
    """Change what `switchyard release-status <project>` reports."""
    r = Result()
    r.add(first(t.py_def("scripts/switchyard_dispatch.py", "switchyard_main"), t.py_def(LAUNCHER, "switchyard_main")))
    parser = first(t.py_def("scripts/project_status.py", "_build_switchyard_release_status_parser"),
                   t.py_def(LAUNCHER, "_build_switchyard_release_status_parser"))
    handler = first(t.py_def("scripts/project_status.py", "switchyard_release_status_command"),
                    t.py_def(LAUNCHER, "switchyard_release_status_command"))
    r.add(parser)
    r.add(handler)
    t.expand(r, handler)
    if handler.path != LAUNCHER:
        r.hops.insert(0, f"{LAUNCHER}:switchyard_release_status_command -> {handler.path}")
    return r


def provisioning_operator_packet(t: Tree) -> Result:
    """Change a command the operator packet renders for a new tenant."""
    r = Result()
    r.add(t.py_def("scripts/ticket_board/project_provision.py", "main"))
    core = first(t.py_def("scripts/ticket_board/provision_operator_packet.py", "render_operator_commands"),
                 t.py_def("scripts/ticket_board/project_provision.py", "render_operator_commands"))
    r.add(core)
    t.expand(r, core)
    if core.path != "scripts/ticket_board/project_provision.py":
        r.hops.append("project_provision re-export of render_operator_commands")
    return r


def app_crop(t: Tree) -> Result:
    """Change a crop's saved filename or metadata, from the HTTP handler down."""
    r = Result()
    handler = t.enclosing("scripts/ticket_board/server.py", "self.app.crop_attachment(")
    r.add(handler)
    crop = t.py_def("scripts/ticket_board/app.py", "TicketBoardApp.crop_attachment")
    r.add(crop)
    t.expand(r, crop, self_class="TicketBoardApp")
    return r


def cli_free_text(t: Tree) -> Result:
    """Change how `ticket-board-write add-comment --text-file` reads its text."""
    r = Result()
    r.add(Unit("scripts/ticket-board-write", "<wrapper>", 1, len((t.text("scripts/ticket-board-write") or "").splitlines()), "file"))
    forwarder = t.py_def("scripts/ticket_board/write_client.py", "main")
    cli = "scripts/ticket_board/write_cli.py" if t.text("scripts/ticket_board/write_cli.py") else "scripts/ticket_board/write_client.py"
    if cli != "scripts/ticket_board/write_client.py":
        r.add(forwarder)
        r.hops.append("write_client.main -> write_cli.main")
    for name in ("main", "resolve_free_text_arguments", "_read_free_text", "_read_text_stream", "add_free_text_argument"):
        r.add(t.py_def(cli, name))
    return r


def service_canary(t: Tree) -> Result:
    """Change what the deploy canary proves before `current` moves."""
    r = Result()
    svc, health = "scripts/ticket-board-service.sh", "scripts/ticket-board-service-health.sh"
    for name in ("main", "deploy_restart_service"):
        r.add(t.sh_fn(svc, name))
    canary = first(t.sh_fn(health, "run_release_canary"), t.sh_fn(svc, "run_release_canary"))
    r.add(canary)
    body = "\n".join((t.text(canary.path) or "").splitlines()[canary.start - 1:canary.end])
    for name in sorted(set(re.findall(r"\b([a-z_]+)\b", body))):
        if name == canary.name:
            continue
        r.add(first(t.sh_fn(health, name), t.sh_fn(svc, name)))
    if t.text(health):
        r.hops.append(f"{svc} sources {health}")
    return r


def frontend_links(t: Tree) -> Result:
    """Change how linked-ticket references render in the board UI."""
    r = Result()
    links, core = "scripts/ticket_board/frontend_script_ticket_links.py", "scripts/ticket_board/frontend_script_core.py"
    for name in ("linkedTicketRow", "appendLinkedTicketText", "buildTicketReference", "ticketById",
                 "linkedTextBlock", "linkedPreview", "buildChildTicketList"):
        r.add(first(t.js_fn(links, name), t.js_fn(core, name)))
    for name in ("openDetail", "stateLabel"):  # named dependencies the owner calls in core
        r.add(t.js_fn(core, name))
    if t.text(links):
        r.hops.append(f"{core} splices SCRIPT_TICKET_LINKS")
    return r


TASKS = [
    ("launcher: release-status report", launcher_release_status),
    ("provisioning: operator packet command", provisioning_operator_packet),
    ("app/attachment: crop filename or metadata", app_crop),
    ("CLI: ticket-board-write free text", cli_free_text),
    ("service: deploy canary", service_canary),
    ("frontend: linked-ticket references", frontend_links),
]


def locate_ms(t: Tree, units: list[Unit]) -> float:
    patterns = []
    for u in units:
        leaf = u.name.split(".")[-1]
        if u.kind == "def":
            patterns.append(rf"def {re.escape(leaf)}\(")
        elif u.kind == "sh":
            patterns.append(rf"^{re.escape(leaf)}\(\) \{{")
        elif u.kind == "js":
            patterns.append(rf"function {re.escape(leaf)}\(")
    runs = []
    for _ in range(5):
        start = time.perf_counter()
        for pattern in patterns:
            subprocess.run(["git", "grep", "-nE", pattern, t.commit, "--", "scripts"], capture_output=True)
        runs.append((time.perf_counter() - start) * 1000)
    return round(statistics.median(runs), 1)


def measure(commit: str) -> dict:
    t = Tree(commit)
    out = {}
    for label, task in TASKS:
        r = task(t)
        files = sorted({u.path for u in r.units})
        out[label] = {
            "units": len(r.units),
            "unit_lines": sum(u.lines for u in r.units),
            "files": len(files),
            "file_lines": sum(len((t.text(p) or "").splitlines()) for p in files),
            "file_bytes": sum(len((t.text(p) or "").encode()) for p in files),
            "hops": r.hops,
            "locate_ms": locate_ms(t, r.units),
            "read": [f"{u.path}:{u.start}-{u.end} {u.name}" for u in r.units],
        }
    return out


def main(argv: list[str]) -> int:
    before, after = (argv + [BEFORE, AFTER])[:2] if len(argv) >= 2 else (BEFORE, AFTER)
    report = {"before": before, "after": after, "tasks": {}}
    b, a = measure(before), measure(after)
    for label, _ in TASKS:
        report["tasks"][label] = {"before": b[label], "after": a[label]}
    print(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
