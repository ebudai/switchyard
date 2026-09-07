"""Helpers for standalone test scripts that execute without pytest."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from tmux_bus_isolation import isolate_tmux_bus

# Applied on import, not per test: a pane is created by whatever a test spawns,
# and only the environment this process passes down can stop it reaching the
# tenant's user manager. Harmless where nothing opens a bus (SYRD-55).
isolate_tmux_bus()


def module_test_functions(
    module_globals: Mapping[str, Any],
    *,
    first: tuple[str, ...] = (),
) -> list[tuple[str, Callable[[], None]]]:
    """Return module-local test_* functions in definition order."""
    module_name = module_globals.get("__name__")
    tests: list[tuple[str, Callable[[], None]]] = []
    for name, value in module_globals.items():
        if not name.startswith("test_") or not callable(value):
            continue
        if getattr(value, "__module__", module_name) != module_name:
            continue
        tests.append((name, value))

    by_name = dict(tests)
    ordered: list[tuple[str, Callable[[], None]]] = []
    for name in first:
        if name not in by_name:
            raise AssertionError(f"unknown priority test: {name}")
        ordered.append((name, by_name[name]))
    ordered.extend((name, test) for name, test in tests if name not in first)
    return ordered


def run_module_tests(module_globals: Mapping[str, Any], *, first: tuple[str, ...] = ()) -> None:
    for _name, test in module_test_functions(module_globals, first=first):
        test()
