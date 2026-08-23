#!/usr/bin/env python3
"""Tests for the standalone test-script discovery helper."""

from __future__ import annotations

from standalone_test_runner import module_test_functions, run_module_tests


def _fake_test(name: str):
    def _test() -> None:
        return

    _test.__name__ = name
    _test.__module__ = "fake_module"
    return _test


def test_module_test_functions_preserve_definition_order_and_ignore_imports() -> None:
    first = _fake_test("test_first")
    imported = _fake_test("test_imported")
    imported.__module__ = "other_module"
    second = _fake_test("test_second")

    tests = module_test_functions(
        {
            "__name__": "fake_module",
            "helper": _fake_test("helper"),
            "test_first": first,
            "test_imported": imported,
            "test_second": second,
        }
    )

    assert tests == [("test_first", first), ("test_second", second)]


def test_run_module_tests_can_prioritize_order_sensitive_setup() -> None:
    calls: list[str] = []

    def test_normal_first() -> None:
        calls.append("normal-first")

    def test_setup() -> None:
        calls.append("setup")

    def test_normal_second() -> None:
        calls.append("normal-second")

    for test in (test_normal_first, test_setup, test_normal_second):
        test.__module__ = "fake_module"

    run_module_tests(
        {
            "__name__": "fake_module",
            "test_normal_first": test_normal_first,
            "test_setup": test_setup,
            "test_normal_second": test_normal_second,
        },
        first=("test_setup",),
    )

    assert calls == ["setup", "normal-first", "normal-second"]


def main() -> int:
    run_module_tests(globals())
    print("standalone_test_runner_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
