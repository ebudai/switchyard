"""The launcher's own definitions, as a guard that counts what its dispatchers call sees them (SYRD-452, SYRD-453).

Two dispatchers moved out of the launcher: `main`, the team-launcher command dispatch, to `scripts/launcher_dispatch.py`
(SYRD-452), and `switchyard_main`, the switchyard command dispatch, to `scripts/switchyard_dispatch.py` (SYRD-453). The
launcher re-exports each, unaliased, and each reads every launcher name through the launcher when it runs. A guard that
counts the calls a dispatcher makes, or the launcher's call sites of something a dispatcher calls, reads the launcher's
top-level nodes through `launcher_body` here. For each dispatcher:

- **Before its move** (no module, and no re-export from it): the launcher's own nodes, unchanged.
- **After it**, only once the launcher defines no function of that name; re-exports exactly the dispatcher's names,
  unaliased, from exactly its module; that module imports nothing of Switchyard's when it loads; and its one function begins
  with the call-time launcher import, imports nothing else but the nested imports it had in the launcher (none for `main`; for `switchyard_main` its four, in their
  order), and reads none of the launcher's names bare: the launcher's nodes plus that function, its launcher import
  dropped and each `launcher.X` read as `X` -- so it counts exactly as the launcher's own definition did.

Anything else raises AssertionError, which fails the guard reading it.
"""

from __future__ import annotations

import ast
from pathlib import Path

IMPORT = "from scripts import team_launcher as launcher"
#: name -> (module file, module, the names the launcher re-exports from it, the nested imports it keeps, in order)
DISPATCHERS = {
    "main": ("scripts/launcher_dispatch.py", "scripts.launcher_dispatch", [("_reject_removed_commands", None), ("main", None)], []),
    "switchyard_main": ("scripts/switchyard_dispatch.py", "scripts.switchyard_dispatch", [("switchyard_main", None)],
                        ["from scripts.ticket_board import privileged_front_door", "from scripts import board_skill_cli",
                         "from scripts import onboarding_readiness", "from scripts import workflow_manage"]),
}


def _moved(root: Path, body: list[ast.stmt], name: str) -> ast.FunctionDef | None:
    path, module_name, reexported, nested = DISPATCHERS[name]
    moved = root / path
    own = [n for n in body if isinstance(n, ast.FunctionDef) and n.name == name]
    reexports = [n for n in body if isinstance(n, ast.ImportFrom) and n.module == module_name]
    if not moved.exists():
        assert own and not reexports, f"no {path}, so the launcher defines {name} itself and re-exports nothing from it"
        return None
    assert not own, f"the launcher defines {name} beside {path}"
    assert len(reexports) == 1 and sorted((a.name, a.asname) for a in reexports[0].names) == reexported, \
        f"the launcher re-exports exactly {', '.join(n for n, _ in reexported)}, unaliased, from {module_name}"
    module = ast.parse(moved.read_text(encoding="utf-8"))
    found = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == name]
    assert len(found) == 1, f"{path} defines one {name}"
    fn = found[0]
    imports = [ast.unparse(x) for x in sorted((x for x in ast.walk(fn) if isinstance(x, (ast.Import, ast.ImportFrom))),
                                              key=lambda x: (x.lineno, x.col_offset))]
    assert fn.body and ast.unparse(fn.body[0]) == IMPORT and imports == [IMPORT, *nested], \
        f"{name}'s first statement is the call-time launcher import, and its only other imports are the nested ones it had: {imports}"
    skip = {id(y) for part in [fn.returns, *(a.annotation for a in fn.args.args + fn.args.kwonlyargs), *fn.args.defaults]
            if part is not None for y in ast.walk(part)}
    local = ({a.arg for x in ast.walk(fn) if isinstance(x, (ast.FunctionDef, ast.Lambda)) for a in x.args.args + x.args.kwonlyargs}
             | {x.id for x in ast.walk(fn) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
             | {a.asname or a.name.split(".")[0] for x in ast.walk(fn) if isinstance(x, (ast.Import, ast.ImportFrom)) for a in x.names})
    launcher_names = ({getattr(n, "name", None) for n in body} | {x.id for n in body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
                      | {a.asname or a.name.split(".")[0] for n in body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names} | {"__file__"})
    loaded = [ast.unparse(n) for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom))
              and any((getattr(n, "module", None) or "") == "scripts" or (getattr(n, "module", None) or "").startswith("scripts.")
                      or a.name.startswith("scripts") for a in n.names)]
    assert loaded == [], f"{path} imports nothing of Switchyard's when it loads: {loaded}"
    own_imports = {a.asname or a.name.split(".")[0] for n in module.body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names} - {"annotations"}
    bare = sorted({x.id for x in ast.walk(fn) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and id(x) not in skip
                   and x.id in launcher_names and x.id not in local and x.id not in own_imports})
    assert bare == [], f"{name} reads none of the launcher's names bare: {bare}"

    class ThroughTheLauncher(ast.NodeTransformer):
        def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
            self.generic_visit(node)
            if isinstance(node.value, ast.Name) and node.value.id == "launcher":
                return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
            return node

    seen = ast.parse(ast.unparse(fn)).body[0]
    seen.body = seen.body[1:]
    return ThroughTheLauncher().visit(seen)


def launcher_body(root: Path, tree: ast.Module | None = None) -> list[ast.stmt]:
    if tree is None:
        tree = ast.parse((root / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    body = list(tree.body)
    return [*body, *(fn for fn in (_moved(root, body, name) for name in DISPATCHERS) if fn is not None)]
