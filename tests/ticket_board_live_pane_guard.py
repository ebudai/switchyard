"""Guards that keep tests from reaching live tmux panes."""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from typing import Any

from standalone_test_runner import module_test_functions

LIVE_TMUX_PANE_COMMANDS = frozenset({"capture-pane", "display-message", "has-session", "list-clients", "send-keys"})


class LivePaneShelloutGuard:
    def __init__(
        self,
        *,
        original_run: Callable[..., subprocess.CompletedProcess[Any]] | None = None,
        patch_bound_run_defaults: tuple[Callable[..., Any], ...] = (),
    ) -> None:
        self.original_run = original_run
        self.patch_bound_run_defaults = patch_bound_run_defaults
        self.escapes: list[list[str]] = []
        self.current_test = ""
        self._installed_original: Callable[..., subprocess.CompletedProcess[Any]] | None = None
        self._patched_defaults: list[tuple[Callable[..., Any], tuple[Any, ...] | None]] = []
        self._patched_kwdefaults: list[tuple[Callable[..., Any], dict[str, Any] | None]] = []

    def _is_live_tmux_pane_shellout(self, args: object) -> bool:
        return (
            isinstance(args, (list, tuple))
            and len(args) > 1
            and str(args[0]) == "tmux"
            and str(args[1]) in LIVE_TMUX_PANE_COMMANDS
        )

    def _is_current_run_callable(self, value: object) -> bool:
        return value is self._installed_original or value == self._installed_original

    def run(self, args: object, *positional: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        if self._is_live_tmux_pane_shellout(args):
            escaped = [str(arg) for arg in args]  # type: ignore[union-attr]
            self.escapes.append(escaped)
            test_detail = f" in {self.current_test}" if self.current_test else ""
            raise AssertionError(f"live tmux pane shellout escaped{test_detail}: {' '.join(escaped)}")
        original = self.original_run or self._installed_original
        assert original is not None
        return original(args, *positional, **kwargs)

    def __enter__(self) -> "LivePaneShelloutGuard":
        self._installed_original = subprocess.run
        subprocess.run = self.run  # type: ignore[assignment]
        for function in self.patch_bound_run_defaults:
            defaults = function.__defaults__
            self._patched_defaults.append((function, defaults))
            if defaults is None:
                function.__defaults__ = None
            else:
                function.__defaults__ = tuple(
                    self.run if self._is_current_run_callable(value) else value
                    for value in defaults
                )
            kwdefaults = function.__kwdefaults__
            self._patched_kwdefaults.append((function, kwdefaults.copy() if kwdefaults is not None else None))
            if kwdefaults is not None:
                function.__kwdefaults__ = {
                    key: self.run if self._is_current_run_callable(value) else value
                    for key, value in kwdefaults.items()
                }
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        for function, kwdefaults in reversed(self._patched_kwdefaults):
            function.__kwdefaults__ = kwdefaults
        self._patched_kwdefaults.clear()
        for function, defaults in reversed(self._patched_defaults):
            function.__defaults__ = defaults
        self._patched_defaults.clear()
        assert self._installed_original is not None
        subprocess.run = self._installed_original  # type: ignore[assignment]

    def assert_no_escapes(self) -> None:
        assert self.escapes == [], f"live tmux pane shellouts escaped: {len(self.escapes)} {self.escapes}"


def run_module_tests_with_live_pane_guard(
    module_globals: Mapping[str, Any],
    *,
    first: tuple[str, ...] = (),
    patch_bound_run_defaults: tuple[Callable[..., Any], ...] = (),
) -> None:
    guard = LivePaneShelloutGuard(patch_bound_run_defaults=patch_bound_run_defaults)
    with guard:
        for name, test in module_test_functions(module_globals, first=first):
            guard.current_test = name
            test()
        guard.current_test = ""
    guard.assert_no_escapes()


def assert_capture_pane_guard_blocks_without_shelling_out(test_name: str) -> None:
    delegated: list[object] = []

    def original_run(args: object, *positional: Any, **kwargs: Any) -> subprocess.CompletedProcess[Any]:
        delegated.append(args)
        return subprocess.CompletedProcess(args, 0)

    guard = LivePaneShelloutGuard(original_run=original_run)
    guard.current_test = test_name
    try:
        guard.run(["tmux", "capture-pane", "-p", "-J", "-t", "pgu-ops:0.0"])
    except AssertionError as exc:
        message = str(exc)
    else:
        raise AssertionError("live pane guard did not reject tmux capture-pane")

    assert delegated == []
    assert test_name in message
    assert "tmux capture-pane" in message
    assert guard.escapes == [["tmux", "capture-pane", "-p", "-J", "-t", "pgu-ops:0.0"]]
