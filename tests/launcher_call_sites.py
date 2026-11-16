"""SYRD-811: where the launcher's names are called and read -- across every module under scripts/, not a fixed list.

The move guards of the SYRD-272 slices named the modules a moved caller lived in. Each later slice moved callers again
(switchyard_dispatch, command_crossing, presentation_window_launch, new_project_phases, ...) without updating its
sibling guards, and the guards went red, hiding their other checks. Scanning every module survives the next slice: a
move relocates a call, and the count across all modules stays the same.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = Path("scripts") / "team_launcher.py"
THROUGH = ("launcher", "team_launcher")


@lru_cache(maxsize=None)
def trees() -> tuple[tuple[Path, ast.AST], ...]:
    return tuple((path.relative_to(ROOT), ast.parse(path.read_text(encoding="utf-8")))
                 for path in sorted((ROOT / "scripts").rglob("*.py")) if "migrations" not in path.parts)


def _through(node: ast.AST) -> bool:
    return isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in THROUGH


def calls(name: str) -> tuple[list[str], list[str], list[str]]:
    """`name`'s call sites as `module:line`: (the launcher's own, by its bare name), (elsewhere, through the
    launcher), (elsewhere, past it -- a bare name, or another module's attribute, which no launcher patch reaches)."""
    own, through, past = [], [], []
    for path, tree in trees():
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if getattr(func, "id", getattr(func, "attr", None)) != name:
                continue
            site = f"{path.name}:{node.lineno}"
            if path == LAUNCHER and isinstance(func, ast.Name):
                own.append(site)
            elif path != LAUNCHER and _through(func):
                through.append(site)
            else:
                past.append(site)
    return own, through, past


def readers(name: str, owners: tuple[str, ...]) -> dict[str, tuple[int, int]]:
    """Every module other than the launcher and `owners` (module stems) that refers to `name`: (references, of
    them through the launcher)."""
    found: dict[str, tuple[int, int]] = {}
    for path, tree in trees():
        if path == LAUNCHER or path.stem in owners:
            continue
        uses = [node for node in ast.walk(tree) if (isinstance(node, ast.Attribute) and node.attr == name)
                or (isinstance(node, ast.Name) and node.id == name)]
        if uses:
            found[path.stem] = (len(uses), sum(1 for node in uses if _through(node)))
    return found
