#!/usr/bin/env python3
"""SYRD-68: the installed tenant group can reach only its board socket."""

from __future__ import annotations

import grp
import os
import socket
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.ticket_board.server import (  # noqa: E402
    CallerRegistry,
    DirectorNotifier,
    TicketBoardEventHub,
    TicketBoardUnixServer,
)


class StaticApp:
    def store_signature(self) -> tuple[tuple[object, ...], ...]:
        return ()


def socket_server(path: Path) -> tuple[TicketBoardUnixServer, TicketBoardEventHub]:
    app = StaticApp()
    events = TicketBoardEventHub(app)  # type: ignore[arg-type]
    notifier = DirectorNotifier(sender=lambda payload: None, batch_window_seconds=0.01)
    try:
        server = TicketBoardUnixServer(
            path,
            app,  # type: ignore[arg-type]
            events=events,
            director_notifier=notifier,
            caller_registry=CallerRegistry(),
        )
    except Exception:
        notifier.close()
        events.close()
        raise
    return server, events


def connect(path: Path) -> None:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(path))


def close_server(server: TicketBoardUnixServer, events: TicketBoardEventHub) -> None:
    server.server_close()
    server.director_notifier.close()
    events.close()


def assert_nonmember_cannot_connect(path: Path) -> None:
    source = """
import socket, sys
client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
try:
    client.connect(sys.argv[1])
except PermissionError:
    raise SystemExit(0)
except OSError as exc:
    print(f'unexpected connect error: {exc}', file=sys.stderr)
    raise SystemExit(2)
else:
    print('nonmember connected to tenant socket', file=sys.stderr)
    raise SystemExit(1)
finally:
    client.close()
"""
    proc = subprocess.run(
        [
            "unshare",
            "--map-auto",
            "--setuid=1",
            "--setgid=1",
            sys.executable,
            "-c",
            source,
            str(path),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr or proc.stdout


def main() -> int:
    supplementary = [gid for gid in os.getgroups() if gid != os.getegid()]
    assert supplementary, "this integration test needs a supplementary tenant group"
    tenant_gid = supplementary[0]
    tenant_group = grp.getgrgid(tenant_gid).gr_name

    with tempfile.TemporaryDirectory(prefix="ticket-board-socket-group.") as tmpdir:
        root = Path(tmpdir)
        root.chmod(0o755)
        runtime = root / "runtime"
        runtime.mkdir(mode=0o750)
        socket_path = runtime / "ticket-board.sock"

        with patch.dict(os.environ, {"TICKET_BOARD_SOCKET_GROUP": tenant_group}):
            server, events = socket_server(socket_path)
            try:
                assert runtime.stat().st_gid == tenant_gid
                assert stat.S_IMODE(runtime.stat().st_mode) == 0o750
                assert socket_path.stat().st_gid == tenant_gid
                assert stat.S_IMODE(socket_path.stat().st_mode) == 0o660
                connect(socket_path)
                assert_nonmember_cannot_connect(socket_path)
            finally:
                close_server(server, events)

            # Match systemd recreating RuntimeDirectory as service:service
            # 0750 on restart. Startup must restore group access by itself.
            os.chown(runtime, -1, os.getegid())
            runtime.chmod(0o750)
            restarted, restarted_events = socket_server(socket_path)
            try:
                assert runtime.stat().st_gid == tenant_gid
                assert stat.S_IMODE(runtime.stat().st_mode) == 0o750
                assert socket_path.stat().st_gid == tenant_gid
                assert stat.S_IMODE(socket_path.stat().st_mode) == 0o660
                connect(socket_path)
                assert_nonmember_cannot_connect(socket_path)
            finally:
                close_server(restarted, restarted_events)

        legacy_runtime = root / "legacy-runtime"
        legacy_runtime.mkdir(mode=0o755)
        legacy_socket = legacy_runtime / "ticket-board.sock"
        with patch.dict(os.environ, {}, clear=True):
            legacy, legacy_events = socket_server(legacy_socket)
            try:
                assert stat.S_IMODE(legacy_runtime.stat().st_mode) == 0o755
                assert stat.S_IMODE(legacy_socket.stat().st_mode) == 0o666
            finally:
                close_server(legacy, legacy_events)

        missing_socket = root / "missing" / "ticket-board.sock"
        with patch.dict(os.environ, {"TICKET_BOARD_SOCKET_GROUP": "syrd-68-no-such-group"}):
            try:
                socket_server(missing_socket)
            except RuntimeError as exc:
                assert "does not resolve to a local group" in str(exc), exc
            else:
                raise AssertionError("an unresolved configured socket group did not fail closed")
        assert not missing_socket.exists()

        failed_runtime = root / "failed-runtime"
        failed_runtime.mkdir(mode=0o750)
        failed_socket = failed_runtime / "ticket-board.sock"
        with (
            patch.dict(os.environ, {"TICKET_BOARD_SOCKET_GROUP": tenant_group}),
            patch("scripts.ticket_board.server.os.chown", side_effect=PermissionError("denied")),
        ):
            try:
                socket_server(failed_socket)
            except RuntimeError as exc:
                assert "could not grant socket access" in str(exc), exc
            else:
                raise AssertionError("a socket-group ownership failure did not fail closed")
        assert not failed_socket.exists()
        assert stat.S_IMODE(failed_runtime.stat().st_mode) == 0o700

    print("ticket_board_socket_group_test: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
