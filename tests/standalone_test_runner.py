"""Helpers for standalone test scripts that execute without pytest."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any


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
