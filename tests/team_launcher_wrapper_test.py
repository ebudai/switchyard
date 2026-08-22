#!/usr/bin/env python3
"""Regression tests for the team-launcher executable wrapper."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import json
import tempfile
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


def _write_config(tmp: Path, *, run_as_user: str = "") -> Path:
    payload: dict[str, object] = {"project": "porter"}
    if run_as_user:
        payload["run_as_user"] = run_as_user
    config_path = tmp / "porter.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


def test_wrapper_delegates_directly_without_sudo_when_run_as_user_unset() -> None:
    wrapper = load_wrapper()
    import scripts.team_launcher as team_launcher

    calls: list[list[str]] = []
    original_main = team_launcher.main
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-wrapper.") as tmp:
            config_path = _write_config(Path(tmp))
            team_launcher.main = lambda argv: calls.append(list(argv)) or 17

            assert wrapper.run(["porter", "start", "--config", str(config_path)]) == 17
    finally:
        team_launcher.main = original_main

    assert calls == [["porter", "start", "--config", str(config_path)]]
    assert not hasattr(wrapper, "TARGET_USER")
    assert not hasattr(wrapper, "sudo_agent_command")


def test_wrapper_sudos_to_configured_run_as_user_when_different() -> None:
    wrapper = load_wrapper()
    calls: list[list[str]] = []

    def fake_call(args: list[str]) -> int:
        calls.append(args)
        return 23

    with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-wrapper.") as tmp:
        config_path = _write_config(Path(tmp), run_as_user="agent")
        wrapper.current_user_name = lambda: "user"
        wrapper.host_wayland_display = lambda: "/run/user/1000/wayland-0"
        wrapper.subprocess.call = fake_call

        assert wrapper.run(["porter", "start", "--config", str(config_path)]) == 23

    assert calls == [[
        "sudo",
        "-u",
        "agent",
        "-H",
        "env",
        "PGU_HOST_WAYLAND_DISPLAY=/run/user/1000/wayland-0",
        wrapper.sys.executable,
        str(WRAPPER_PATH.resolve()),
        "porter",
        "start",
        "--config",
        str(config_path),
    ]]


def test_wrapper_does_not_sudo_when_already_run_as_user() -> None:
    wrapper = load_wrapper()
    import scripts.team_launcher as team_launcher

    calls: list[list[str]] = []
    sudo_calls: list[list[str]] = []
    original_main = team_launcher.main
    try:
        with tempfile.TemporaryDirectory(prefix="pgu-team-launcher-wrapper.") as tmp:
            config_path = _write_config(Path(tmp), run_as_user="agent")
            wrapper.current_user_name = lambda: "agent"
            wrapper.subprocess.call = lambda args: sudo_calls.append(args) or 99
            team_launcher.main = lambda argv: calls.append(list(argv)) or 17

            assert wrapper.run(["porter", "start", "--config", str(config_path)]) == 17
    finally:
        team_launcher.main = original_main

    assert calls == [["porter", "start", "--config", str(config_path)]]
    assert sudo_calls == []


def main() -> int:
    test_wrapper_delegates_directly_without_sudo_when_run_as_user_unset()
    test_wrapper_sudos_to_configured_run_as_user_when_different()
    test_wrapper_does_not_sudo_when_already_run_as_user()
    print("team_launcher_wrapper_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
