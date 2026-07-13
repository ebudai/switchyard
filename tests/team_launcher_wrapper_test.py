#!/usr/bin/env python3
"""Regression tests for the team-launcher executable wrapper."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
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


def test_agent_user_does_not_self_elevate() -> None:
    wrapper = load_wrapper()

    assert not wrapper.should_self_elevate("agent")


def test_non_agent_user_self_elevates_with_sudo_agent_home() -> None:
    wrapper = load_wrapper()
    calls: list[list[str]] = []

    def fake_call(args: list[str]) -> int:
        calls.append(args)
        return 17

    wrapper.current_user_name = lambda: "eric"
    wrapper.subprocess.call = fake_call

    assert wrapper.self_elevate_to_agent_if_needed(["pgu", "start"]) == 17
    assert calls == [[
        "sudo",
        "-u",
        "agent",
        "-H",
        sys.executable,
        str(WRAPPER_PATH.resolve()),
        "pgu",
        "start",
    ]]


def test_unknown_user_self_elevates_safe() -> None:
    wrapper = load_wrapper()

    assert wrapper.should_self_elevate("")


def test_agent_user_runs_launcher_without_sudo() -> None:
    wrapper = load_wrapper()
    calls: list[list[str]] = []

    def fake_call(args: list[str]) -> int:
        calls.append(args)
        return 99

    wrapper.current_user_name = lambda: "agent"
    wrapper.subprocess.call = fake_call

    assert wrapper.self_elevate_to_agent_if_needed(["pgu", "start"]) is None
    assert calls == []


def main() -> int:
    test_agent_user_does_not_self_elevate()
    test_non_agent_user_self_elevates_with_sudo_agent_home()
    test_unknown_user_self_elevates_safe()
    test_agent_user_runs_launcher_without_sudo()
    print("team_launcher_wrapper_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
