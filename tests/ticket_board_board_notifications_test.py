#!/usr/bin/env python3
"""SYRD-504: director notices and the board event hub, with fake timers, threads and commands only."""

from __future__ import annotations

import ast
import logging
import os
import queue
import socketserver
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board import board_notifications as owner  # noqa: E402
from scripts.ticket_board import server  # noqa: E402

PUBLIC = (
    "DEFAULT_DIRECTORCTL", "DIRECTOR_NOTIFICATION_BATCH_WINDOW_SECONDS", "DirectorNotifier",
    "TicketBoardEventHub", "director_target", "send_director_message",
)


class Logs(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[tuple[str, str, str, bool]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.name, record.levelname, record.getMessage(), bool(record.exc_info)))

    def __enter__(self) -> Logs:
        logging.getLogger("scripts.ticket_board.server").addHandler(self)
        return self

    def __exit__(self, *_args: Any) -> None:
        logging.getLogger("scripts.ticket_board.server").removeHandler(self)


class Runs:
    """Stands in for subprocess.run on the shared module object, as existing suites do."""

    def __init__(self, fail_on: str = "") -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.fail_on = fail_on

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        self.calls.append((list(argv), kwargs))
        if self.fail_on and self.fail_on in argv[-1]:
            raise subprocess.CalledProcessError(1, argv)
        return subprocess.CompletedProcess(argv, 0, "", "")


class FakeTimer:
    made: list[FakeTimer] = []

    def __init__(self, interval: float, function: Any) -> None:
        self.interval, self.function, self.daemon, self.started, self.joins = interval, function, None, False, []
        FakeTimer.made.append(self)

    def start(self) -> None:
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        self.joins.append(timeout)


class FakeThread:
    made: list[FakeThread] = []

    def __init__(self, target: Any = None, name: str | None = None, daemon: bool | None = None) -> None:
        self.target, self.name, self.daemon, self.started, self.joins = target, name, daemon, False, []
        FakeThread.made.append(self)

    def start(self) -> None:
        self.started = True

    def join(self, timeout: float | None = None) -> None:
        self.joins.append(timeout)


class ScriptedStop:
    def __init__(self, scans: int) -> None:
        self.scans, self.waits, self.was_set = scans, [], False

    def wait(self, interval: float) -> bool:
        self.waits.append(interval)
        self.scans -= 1
        return self.scans < 0

    def set(self) -> None:
        self.was_set = True


@contextmanager
def fakes(runs: Runs | None = None) -> Iterator[Runs]:
    runs = runs or Runs()
    FakeTimer.made.clear()
    FakeThread.made.clear()
    with patch.object(server.subprocess, "run", runs), patch.object(threading, "Timer", FakeTimer), \
            patch.object(threading, "Thread", FakeThread):
        yield runs


class App:
    def __init__(self, *scans: Any) -> None:
        self.scans = list(scans)

    def store_signature(self) -> Any:
        value = self.scans.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_server_keeps_public_names_logger_and_default_path() -> None:
    for name in PUBLIC:
        assert getattr(server, name) is getattr(owner, name), name
    assert owner.LOGGER is server.LOGGER and owner.LOGGER.name == "scripts.ticket_board.server"
    assert server.subprocess is subprocess and owner.subprocess is subprocess
    assert owner.DIRECTOR_NOTIFICATION_BATCH_WINDOW_SECONDS == 0.35
    tree = ast.parse(Path(owner.__file__).read_text(encoding="utf-8"))
    runtime = [node.module for node in tree.body if isinstance(node, ast.ImportFrom) and node.module != "__future__"]
    assert runtime == ["typing", "app", "runtime_paths"], runtime
    guarded = [node for node in tree.body if isinstance(node, ast.If)]
    assert len(guarded) == 1 and ast.unparse(guarded[0].test) == "TYPE_CHECKING"
    child = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, 'scripts'); import ticket_board.server as s, ticket_board.board_notifications as n;"
         " print(s.LOGGER.name, n.LOGGER.name, s.DirectorNotifier is n.DirectorNotifier, n.DEFAULT_DIRECTORCTL)"],
        cwd=ROOT, capture_output=True, text=True, check=True, env={"PATH": "/usr/bin:/bin"},
    )
    assert child.stdout.split() == ["ticket_board.server", "ticket_board.server", "True", str(ROOT / "scripts" / "directorctl")], child


def test_target_and_send_argv() -> None:
    saved = {key: os.environ.pop(key) for key in ("TICKET_BOARD_PROJECT", "PGU_TICKET_BOARD_PROJECT") if key in os.environ}
    try:
        assert owner.director_target() == "pgu-director:0.0"
        os.environ["TICKET_BOARD_PROJECT"] = " Orbit "
        assert owner.director_target() == "orbit-director:0.0"
        assert owner.director_target(" CERULEAN ") == "cerulean-director:0.0"
        assert owner.director_target("") == "orbit-director:0.0"
        assert owner.director_target("  ") == "pgu-director:0.0"
        with fakes() as runs:
            owner.send_director_message("hello", "a-director:0.0")
            owner.send_director_message("hi")
        assert runs.calls == [
            ([owner.DEFAULT_DIRECTORCTL, "send", "a-director:0.0", "hello"], {"check": True, "capture_output": True, "text": True}),
            ([owner.DEFAULT_DIRECTORCTL, "send", "orbit-director:0.0", "hi"], {"check": True, "capture_output": True, "text": True}),
        ], runs.calls
        with fakes(Runs(fail_on="boom")):
            try:
                owner.send_director_message("boom")
            except subprocess.CalledProcessError:
                pass
            else:
                raise AssertionError("a failed directorctl send must raise to the notifier")
    finally:
        os.environ.pop("TICKET_BOARD_PROJECT", None)
        os.environ.update(saved)


def test_notifier_batches_then_flushes_on_timer_or_close() -> None:
    with fakes() as runs:
        notifier = owner.DirectorNotifier(project="orbit")
        notifier.notify_ticket_created({"id": " ", "title": "x"})
        notifier.notify_ticket_created({"id": "ORB-9"})
        assert FakeTimer.made == []
        notifier.notify_ticket_created({"id": " ORB-1 ", "title": " First "})
        notifier.notify_ticket_created({"id": "ORB-2", "title": "Second"})
        assert len(FakeTimer.made) == 1
        timer = FakeTimer.made[0]
        assert (timer.interval, timer.daemon, timer.started) == (0.35, True, True)
        timer.function()
        notifier.close()
        notifier.notify_ticket_created({"id": "ORB-3", "title": "Third"})
        notifier.close()
        assert FakeTimer.made[1].joins == [0.85]
    assert [argv for argv, _ in runs.calls] == [
        [owner.DEFAULT_DIRECTORCTL, "send", "orbit-director:0.0", "New tickets for you: ORB-1 -- First; ORB-2 -- Second"],
        [owner.DEFAULT_DIRECTORCTL, "send", "orbit-director:0.0", "New ticket for you: ORB-3 -- Third"],
    ], runs.calls


def test_notifier_logs_a_failed_send_and_keeps_going() -> None:
    sent: list[str] = []
    with fakes() as runs, Logs() as logs:
        custom = owner.DirectorNotifier(sender=sent.append, batch_window_seconds=0.01)
        custom.notify_ticket_created({"id": "P-1", "title": "t"})
        assert FakeTimer.made[-1].interval == 0.01
        FakeTimer.made[-1].function()

        def down(_payload: str) -> None:
            raise RuntimeError("down")

        failing = owner.DirectorNotifier(sender=down)
        failing.notify_ticket_created({"id": "P-2", "title": "t"})
        FakeTimer.made[-1].function()
        failing.notify_ticket_created({"id": "P-3", "title": "t"})
        assert len(FakeTimer.made) == 3, "a failed flush must leave the notifier able to batch again"
    assert sent == ["New ticket for you: P-1 -- t"] and runs.calls == []
    assert logs.records == [("scripts.ticket_board.server", "ERROR", "Failed to send director ticket notification", True)]


def test_event_hub_versions_and_keeps_only_the_latest_change() -> None:
    with fakes(), Logs() as logs:
        blind = owner.TicketBoardEventHub(App(RuntimeError("dsn=secret")))
        assert blind.degraded_reason == owner.TicketBoardEventHub.DEGRADED_REASON and blind._signature == ()
        thread = FakeThread.made[0]
        assert (thread.name, thread.daemon, thread.started, thread.target) == ("ticket-board-events", True, True, blind._watch_loop)
        hub = owner.TicketBoardEventHub(App((("a", 1),)), scan_interval_seconds=0.5)
        first, v1 = hub.register()
        second, v2 = hub.register()
        assert (v1, v2, hub.degraded_reason) == (0, 0, "")
        assert first.maxsize == 1, "a slow client must hold one pending version, not a growing backlog"
        assert hub.notify_change() == 1 and hub.notify_change((("b", 2),)) == 2
        assert first.get_nowait() == 2 and second.qsize() == 1 and hub._signature == (("b", 2),)
        hub.unregister(second)
        second.get_nowait()
        hub.notify_change()
        assert first.get_nowait() == 3 and second.qsize() == 0
        hub.close()
        assert FakeThread.made[1].joins == [1.5]
    assert logs.records == [("scripts.ticket_board.server", "WARNING", "board reconciliation could not read its initial state", True)]


def test_event_hub_degrades_on_the_third_failure_and_recovers() -> None:
    with fakes(), Logs() as logs:
        hub = owner.TicketBoardEventHub(App((("a", 1),)), scan_interval_seconds=0.5)
        listener, _ = hub.register()
        hub.app.scans = [RuntimeError("dsn=secret one"), RuntimeError("two"), RuntimeError("three")]
        hub._stop_event = ScriptedStop(2)
        hub._watch_loop()
        assert hub.degraded_reason == "" and hub._version == 0 and listener.empty()
        hub._stop_event = ScriptedStop(1)
        hub._watch_loop()
        assert hub.degraded_reason == "the board service cannot read its own state" and hub._version == 1
        assert "secret" not in hub.degraded_reason and listener.get_nowait() == 1
        hub.app.scans = [RuntimeError("four"), (("a", 1),), (("a", 1),), (("b", 2),)]
        hub._stop_event = ScriptedStop(4)
        hub._watch_loop()
        assert hub._stop_event.waits == [0.5] * 5
        assert hub.degraded_reason == "" and hub._consecutive_failures == 0 and hub._version == 3, hub._version
        assert hub._signature == (("b", 2),) and listener.get_nowait() == 3
        hub.close()
        assert hub._stop_event.was_set
    warnings = [message for _, level, message, _ in logs.records if level == "WARNING"]
    assert warnings == [f"board reconciliation scan failed: {text}" for text in ("dsn=secret one", "two", "three", "four")]


def test_servers_construct_and_close_workers_in_order() -> None:
    order: list[str] = []

    class Hub:
        def __init__(self, app: Any) -> None:
            order.append("hub")

        def close(self) -> None:
            order.append("hub.close")

    class Notifier:
        def __init__(self, project: Any = None) -> None:
            order.append(f"notifier:{project}")

        def close(self) -> None:
            order.append("notifier.close")

    app = type("A", (), {"project": "orbit"})()
    with patch.object(server, "TicketBoardEventHub", Hub), patch.object(server, "DirectorNotifier", Notifier), \
            patch.object(socketserver.TCPServer, "__init__", lambda self, *a, **k: order.append("socket")), \
            patch.object(socketserver.TCPServer, "server_close", lambda self: order.append("socket.close")), \
            patch.object(server, "board_build_id", lambda: "test"):
        owned = server.TicketBoardServer(("127.0.0.1", 0), app)
        owned.server_close()
        injected = server.TicketBoardServer(("127.0.0.1", 0), app, director_notifier=Notifier(), events=Hub(app))
        injected.server_close()
    assert order == [
        "hub", "notifier:orbit", "socket", "hub.close", "notifier.close", "socket.close",
        "notifier:None", "hub", "socket", "socket.close",
    ], order


def main() -> int:
    count = 0
    for name, value in sorted(globals().items()):
        if name.startswith("test_") and callable(value):
            value()
            count += 1
    print(f"ticket_board_board_notifications_test: {count} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
