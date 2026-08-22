#!/usr/bin/env python3
"""Regression tests for the team-launcher executable wrapper."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER_PATH = ROOT / "scripts" / "team-launcher"


def load_wrapper():
    loader = importlib.machinery.SourceFileLoader("team_launcher_wrapper", str(WRAPPER_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    if spec is None:
        raise RuntimeError(f"failed to load {WRAPPER_PATH}")
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_wrapper_delegates_directly_without_sudo_for_any_invoking_user() -> None:
    wrapper = load_wrapper()
    import scripts.team_launcher as team_launcher

    calls: list[list[str]] = []
    original_main = team_launcher.main
    try:
        team_launcher.main = lambda argv: calls.append(list(argv)) or 17

        assert wrapper.run(["pgu", "start"]) == 17
    finally:
        team_launcher.main = original_main

    assert calls == [["pgu", "start"]]
    assert not hasattr(wrapper, "TARGET_USER")
    assert not hasattr(wrapper, "sudo_agent_command")
    assert not hasattr(wrapper, "self_elevate_to_agent_if_needed")


def main() -> int:
    test_wrapper_delegates_directly_without_sudo_for_any_invoking_user()
    print("team_launcher_wrapper_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
