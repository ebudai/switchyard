"""The launcher's own definitions, as a guard that counts what `main` calls sees them (SYRD-452).

`main`, the team-launcher command dispatch, moved to `scripts/launcher_dispatch.py` (SYRD-452). The launcher re-exports
it, unaliased, and `main` reads every launcher name through the launcher when it runs. A guard that counts the calls
`main` makes, or the launcher's call sites of something `main` calls, reads the launcher's top-level nodes through
`launcher_body` here:

- **Before the move** (no `scripts/launcher_dispatch.py`, and no re-export from it): the launcher's own nodes, unchanged.
- **After it**, only once the launcher defines no `main`; re-exports exactly `_reject_removed_commands` and `main`,
  unaliased, from exactly `scripts.launcher_dispatch`; and that module's one `main` begins with the call-time launcher
  import, imports nothing else, and reads none of the launcher's names bare: the launcher's nodes plus that `main`, its
  import dropped and each `launcher.X` read as `X` -- so it counts exactly as the launcher's own `main` did.

Anything else raises AssertionError, which fails the guard reading it.
"""

from __future__ import annotations

import ast
from pathlib import Path

IMPORT = "from scripts import team_launcher as launcher"
REEXPORTED = [("_reject_removed_commands", None), ("main", None)]


def launcher_body(root: Path, tree: ast.Module | None = None) -> list[ast.stmt]:
    if tree is None:
        tree = ast.parse((root / "scripts" / "team_launcher.py").read_text(encoding="utf-8"))
    body = list(tree.body)
    moved = root / "scripts" / "launcher_dispatch.py"
    own = [n for n in body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    reexports = [n for n in body if isinstance(n, ast.ImportFrom) and n.module == "scripts.launcher_dispatch"]
    if not moved.exists():
        assert own and not reexports, "no scripts/launcher_dispatch.py, so the launcher defines main itself and re-exports nothing from it"
        return body
    assert not own, "the launcher defines main beside scripts/launcher_dispatch.py"
    assert len(reexports) == 1 and sorted((a.name, a.asname) for a in reexports[0].names) == REEXPORTED, \
        "the launcher re-exports exactly _reject_removed_commands and main, unaliased, from scripts.launcher_dispatch"
    module = ast.parse(moved.read_text(encoding="utf-8"))
    mains = [n for n in module.body if isinstance(n, ast.FunctionDef) and n.name == "main"]
    assert len(mains) == 1, "scripts/launcher_dispatch.py defines one main"
    main = mains[0]
    imports = [ast.unparse(x) for x in ast.walk(main) if isinstance(x, (ast.Import, ast.ImportFrom))]
    assert main.body and ast.unparse(main.body[0]) == IMPORT and imports == [IMPORT], \
        f"main's first statement, and its only import, is the call-time launcher import: {imports}"
    skip = {id(y) for part in [main.returns, *(a.annotation for a in main.args.args + main.args.kwonlyargs), *main.args.defaults]
            if part is not None for y in ast.walk(part)}
    local = {a.arg for a in main.args.args + main.args.kwonlyargs} | {x.id for x in ast.walk(main) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Store)}
    launcher_names = ({getattr(n, "name", None) for n in body} | {x.id for n in body if isinstance(n, ast.Assign) for x in n.targets if isinstance(x, ast.Name)}
                      | {a.asname or a.name.split(".")[0] for n in body if isinstance(n, (ast.Import, ast.ImportFrom)) for a in n.names})
    bare = sorted({x.id for x in ast.walk(main) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load) and id(x) not in skip
                   and x.id in launcher_names and x.id not in local and x.id not in ("subprocess", "sys", "launcher")})
    assert bare == [], f"main reads none of the launcher's names bare: {bare}"

    class ThroughTheLauncher(ast.NodeTransformer):
        def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
            self.generic_visit(node)
            if isinstance(node.value, ast.Name) and node.value.id == "launcher":
                return ast.copy_location(ast.Name(id=node.attr, ctx=node.ctx), node)
            return node

    seen = ast.parse(ast.unparse(main)).body[0]
    seen.body = seen.body[1:]
    return [*body, ThroughTheLauncher().visit(seen)]
