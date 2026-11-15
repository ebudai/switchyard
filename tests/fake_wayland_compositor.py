"""A Unix-socket Wayland compositor that speaks just enough of the wire protocol for desktop access (SYRD-791).

It advertises globals, answers `wl_display.sync`, records `wl_registry.bind`, and implements
`wp_security_context_manager_v1.create_listener` for real: it takes the listening socket and the close fd passed with the
request, records the metadata the client sets, accepts clients on that listener as sandboxed ones -- which see only the
globals a sandboxed client is offered -- and stops accepting when the close fd hangs up, as KWin does.
"""

from __future__ import annotations

import array
import os
import select
import socket
import struct
import threading
from pathlib import Path

#: What KWin 6.8.2 offered a client of its own socket that matters here, and what it withheld from a sandboxed one
#: (measured in the SYRD-791 VM).
PRIVILEGED = ("org_kde_plasma_window_management", "org_kde_kwin_fake_input", "zkde_screencast_unstable_v1",
              "ext_data_control_manager_v1", "wp_security_context_manager_v1")
ORDINARY = ("wl_compositor", "wl_shm", "wl_seat", "xdg_wm_base", "xdg_activation_v1", "wl_data_device_manager",
            "zwlr_layer_shell_v1")


def _string(text: str) -> bytes:
    raw = text.encode() + b"\0"
    return struct.pack("<I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


def _read_string(body: bytes, at: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<I", body, at)
    return body[at + 4:at + 4 + length - 1].decode(), at + 4 + length + (-length % 4)


def _message(obj: int, opcode: int, payload: bytes = b"") -> bytes:
    return struct.pack("<II", obj, ((8 + len(payload)) << 16) | opcode) + payload


class Context:
    def __init__(self) -> None:
        self.metadata: dict[str, str] = {}
        self.committed = False
        self.clients = 0
        self.closed = threading.Event()


class FakeCompositor:
    def __init__(self, path: Path, *, security_context: bool = True, refuse_create: bool = False) -> None:
        self.path = Path(path)
        self.globals = [*ORDINARY, *PRIVILEGED] if security_context else [*ORDINARY, *PRIVILEGED[:-1]]
        self.refuse_create = refuse_create
        self.contexts: list[Context] = []
        self._listeners: list[socket.socket] = []
        self.binds: list[str] = []
        self.direct_clients = 0
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(str(self.path))
        self.server.listen(16)
        self._spawn(self._accept, self.server, None)

    def _spawn(self, target, *args) -> None:
        thread = threading.Thread(target=target, args=args, daemon=True)
        thread.start()
        self._threads.append(thread)

    def close(self) -> None:
        """The compositor going away: its own socket and every context listener it held close with it."""
        self._stop.set()
        self.server.close()
        for listener in self._listeners:
            try:
                listener.close()
            except OSError:
                pass
        # A thread still inside select() on a closed listener keeps the socket alive until it returns; a compositor
        # that has exited holds nothing, so wait for those to finish.
        for thread in self._threads:
            if thread is not threading.current_thread():
                thread.join(timeout=1)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    # -- connections ------------------------------------------------------------------------------------------------
    def _accept(self, listener: socket.socket, context: Context | None, close_fd: int | None = None) -> None:
        while not self._stop.is_set():
            watch = [listener] + ([close_fd] if close_fd is not None else [])
            try:
                ready, _, _ = select.select(watch, [], [], 0.1)
            except (OSError, ValueError):
                return
            if close_fd is not None and close_fd in ready:
                context.closed.set()  # the client's end hung up: stop listening, as the protocol says
                listener.close()
                os.close(close_fd)
                return
            if listener in ready:
                try:
                    conn, _ = listener.accept()
                except OSError:
                    return
                if context is None:
                    self.direct_clients += 1
                else:
                    context.clients += 1
                self._spawn(self._serve, conn, context)

    def _serve(self, conn: socket.socket, sandbox: Context | None) -> None:
        offered = [g for g in self.globals if sandbox is None or g not in PRIVILEGED]
        objects: dict[int, str] = {1: "wl_display"}
        buffer, fds = b"", []
        conn.settimeout(0.2)
        while not self._stop.is_set():
            try:
                data, ancillary, _flags, _addr = conn.recvmsg(65536, socket.CMSG_SPACE(16 * 4))
            except socket.timeout:
                continue
            except OSError:
                return
            if not data:
                conn.close()
                return
            for level, kind, payload in ancillary:
                if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                    received = array.array("i")
                    received.frombytes(payload[:len(payload) - len(payload) % received.itemsize])
                    fds.extend(received)
            buffer += data
            while len(buffer) >= 8:
                obj, word = struct.unpack_from("<II", buffer)
                size, opcode = word >> 16, word & 0xFFFF
                if len(buffer) < size:
                    break
                body, buffer = buffer[8:size], buffer[size:]
                reply = self._request(objects, offered, obj, opcode, body, fds)
                if reply:
                    try:
                        conn.sendall(reply)
                    except OSError:  # the client has gone, as a probe connection does at once
                        conn.close()
                        return

    def _request(self, objects, offered, obj, opcode, body, fds) -> bytes:
        kind = objects.get(obj)
        if kind == "wl_display" and opcode == 0:  # sync
            (callback,) = struct.unpack_from("<I", body)
            return _message(callback, 0, struct.pack("<I", 1))
        if kind == "wl_display" and opcode == 1:  # get_registry
            (registry,) = struct.unpack_from("<I", body)
            objects[registry] = "wl_registry"
            return b"".join(_message(registry, 0, struct.pack("<I", n + 1) + _string(g) + struct.pack("<I", 1))
                            for n, g in enumerate(offered))
        if kind == "wl_registry" and opcode == 0:  # bind
            (name,) = struct.unpack_from("<I", body)
            interface, at = _read_string(body, 4)
            _version, new = struct.unpack_from("<II", body, at)
            objects[new] = interface
            self.binds.append(interface)
            return b""
        if kind == "wp_security_context_manager_v1" and opcode == 1:  # create_listener
            (new,) = struct.unpack_from("<I", body)
            listen_fd, close_fd = fds.pop(0), fds.pop(0)
            if self.refuse_create:
                os.close(listen_fd), os.close(close_fd)
                return _message(1, 0, struct.pack("<II", obj, 1) + _string("refused by the fixture"))
            context = Context()
            self.contexts.append(context)
            objects[new] = ("wp_security_context_v1", context)  # type: ignore[assignment]
            listener = socket.socket(fileno=listen_fd)
            self._listeners.append(listener)
            self._spawn(self._accept, listener, context, close_fd)
            return b""
        if isinstance(kind, tuple) and kind[0] == "wp_security_context_v1":
            context = kind[1]
            if opcode in (1, 2, 3):
                value, _ = _read_string(body, 0)
                context.metadata[{1: "engine", 2: "app_id", 3: "instance_id"}[opcode]] = value
            elif opcode == 4:
                context.committed = True
        return b""


def globals_seen(path: Path) -> list[str]:
    """What a client connecting at `path` is offered: a real wl_display handshake."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(3)
    conn.connect(str(path))
    conn.sendall(_message(1, 1, struct.pack("<I", 2)) + _message(1, 0, struct.pack("<I", 3)))
    seen, buffer = [], b""
    while True:
        buffer += conn.recv(65536)
        while len(buffer) >= 8:
            obj, word = struct.unpack_from("<II", buffer)
            size = word >> 16
            if len(buffer) < size:
                break
            body, buffer = buffer[8:size], buffer[size:]
            if obj == 2:
                seen.append(_read_string(body, 4)[0])
            if obj == 3:
                conn.close()
                return seen
