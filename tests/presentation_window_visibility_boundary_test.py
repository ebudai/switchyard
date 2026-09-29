#!/usr/bin/env python3
"""SYRD-498: visible and headless presentation proof with synthetic tmux."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
for extra in (str(ROOT), str(ROOT / "scripts")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import presentation_controller as direct_controller  # noqa: E402
from scripts import presentation_controller as package_controller  # noqa: E402
from scripts import presentation_window_visibility as owner  # noqa: E402


class Tmux:
    def __init__(self) -> None:
        self.sessions = {"sample-display-0", "sample-display-1", "sample-viewer"}
        self.panes = {
            "sample-display-0": ["/dev/pts/20"],
            "sample-display-1": ["/dev/pts/21"],
            "sample-viewer": ["/dev/pts/10", "/dev/pts/11"],
        }
        self.clients = {
            "sample-display-0": ["/dev/pts/10"],
            "sample-display-1": ["/dev/pts/11"],
            "sample-viewer": [],
        }
        self.calls: list[list[str]] = []
        self.kill_return = 0

    def __call__(self, args, **_kwargs):
        argv = list(args)
        self.calls.append(argv)
        verb = argv[1]
        target = argv[argv.index("-t") + 1].removeprefix("=").split(":", 1)[0]
        exists = target in self.sessions
        if verb == "has-session":
            return subprocess.CompletedProcess(argv, 0 if exists else 1, "", "")
        if verb == "list-panes":
            return subprocess.CompletedProcess(argv, 0 if exists else 1,
                                               "\n".join(self.panes.get(target, [])), "")
        if verb == "list-clients":
            return subprocess.CompletedProcess(argv, 0 if exists else 1,
                                               "\n".join(self.clients.get(target, [])), "")
        if verb == "kill-session":
            if self.kill_return == 0:
                self.sessions.remove(target)
            return subprocess.CompletedProcess(argv, self.kill_return, "", "")
        raise AssertionError(argv)


def config():
    return SimpleNamespace(project="sample")


def test_only_external_terminals_prove_a_window_and_slot_visibility() -> None:
    tmux = Tmux()
    own = owner._presentation_client_ttys(config(), 2, runner=tmux)
    assert own == {"/dev/pts/20", "/dev/pts/21", "/dev/pts/10", "/dev/pts/11"}
    assert owner.external_presentation_clients(config(), 2, runner=tmux) == set()
    assert not owner._slot_visible(config(), 0, own, runner=tmux)

    tmux.clients["sample-viewer"].append("/dev/pts/desktop")
    assert owner.external_presentation_clients(config(), 2, runner=tmux) == {"/dev/pts/desktop"}
    assert owner._slot_visible(config(), 0, own, runner=tmux)

    tmux.clients["sample-viewer"].clear()
    tmux.clients["sample-display-1"].append("/dev/pts/direct")
    assert owner._slot_visible(config(), 1, own, runner=tmux)
    assert not owner._slot_visible(config(), 0, own, runner=tmux)
    assert owner.external_presentation_clients(config(), 2, runner=tmux) == {"/dev/pts/direct"}
    assert all(call[call.index("-t") + 1].startswith("=") for call in tmux.calls if "-t" in call)


def test_delayed_attachment_and_timeout_use_injected_clock() -> None:
    tmux = Tmux()
    sleeps: list[float] = []

    def attach_after_poll(seconds: float) -> None:
        sleeps.append(seconds)
        tmux.clients["sample-viewer"].append("/dev/pts/desktop")

    assert owner.await_presentation_window(
        config(), 2, runner=tmux, timeout=1.0, poll=0.25,
        monotonic=lambda: 0.0, sleep=attach_after_poll,
    ) == {"/dev/pts/desktop"}
    assert sleeps == [0.25]
    tmux.clients["sample-viewer"].clear()
    assert owner.await_presentation_window(
        config(), 2, runner=tmux, timeout=0.0, poll=0.25,
        monotonic=lambda: 0.0, sleep=attach_after_poll,
    ) == set()
    assert sleeps == [0.25]


def test_failure_diagnostic_and_headless_detach_keep_order_and_exact_target() -> None:
    tmux = Tmux()
    message = owner.unmapped_presentation_message(
        config(), layout="viewer", control_user="desktop"
    )
    assert "not on any screen" in message
    assert "opens no window of its own" in message
    assert "switchyard sample" in message
    tmux.calls.clear()
    owner._detach_headless_presentation(config(), 2, runner=tmux)
    assert tmux.calls == [
        ["tmux", "has-session", "-t", "=sample-viewer"],
        ["tmux", "kill-session", "-t", "=sample-viewer"],
    ]
    assert "sample-viewer" not in tmux.sessions
    assert {"sample-display-0", "sample-display-1"} <= tmux.sessions
    tmux.calls.clear()
    owner._detach_headless_presentation(config(), 2, runner=tmux)
    assert tmux.calls == [["tmux", "has-session", "-t", "=sample-viewer"]]


def test_direct_and_package_controller_forms_reexport_all_nine_objects() -> None:
    names = (
        "WINDOW_ATTACH_TIMEOUT_SECONDS", "WINDOW_ATTACH_POLL_SECONDS",
        "_presentation_client_ttys", "_session_client_ttys",
        "external_presentation_clients", "await_presentation_window",
        "unmapped_presentation_message", "_detach_headless_presentation", "_slot_visible",
    )
    for name in names:
        assert getattr(package_controller, name) is getattr(owner, name), name
        assert getattr(direct_controller, name) is getattr(owner, name), name


def main() -> int:
    checks = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            checks += 1
    print(f"presentation_window_visibility_boundary_test: {checks} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
