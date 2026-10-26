"""A stand-in Wayland compositor socket for suites that need one answering, not a desktop (SYRD-687).

It answers `wl_display.sync` the way a compositor does -- `wl_callback.done` on
the new id, then `wl_display.delete_id` -- which is all the readiness probe
(`desktop_access.wayland_roundtrip`) asks. It also answers one request no real
compositor has, object 0xdead opcode 0: it reads every descriptor that came
with it and sends back what they held, each with a descriptor of its own, so a
relay that drops or reorders descriptors is seen to. It records every
connection's peer uid.
"""

from __future__ import annotations

import os
import socket
import struct
import threading
from pathlib import Path

ECHO_OBJECT = 0xDEAD


class FakeCompositor:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.peers: list[int] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        self.server.bind(str(self.path))
        self.server.listen(16)
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                conn, _ = self.server.accept()
            except OSError:
                return
            self.peers.append(struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1])
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        buffer, fds = b"", []
        with conn:
            while True:
                try:
                    data, got, _flags, _ = socket.recv_fds(conn, 4096, 16)
                except OSError:
                    return
                if not data:
                    return
                buffer += data
                fds.extend(got)
                while len(buffer) >= 8:
                    obj, word = struct.unpack_from("=II", buffer)
                    size = word >> 16
                    if len(buffer) < size:
                        break
                    message, buffer = buffer[:size], buffer[size:]
                    if obj == 1 and word & 0xFFFF == 0:
                        callback = struct.unpack_from("=I", message, 8)[0]
                        conn.sendall(struct.pack("=III", callback, 12 << 16, 1) + struct.pack("=III", 1, (12 << 16) | 1, callback))
                    elif obj == ECHO_OBJECT:
                        for fd in fds:
                            content = os.pread(fd, 4096, 0)
                            os.close(fd)
                            read, write = os.pipe()
                            os.write(write, content)
                            os.close(write)
                            socket.send_fds(conn, [struct.pack("=II", ECHO_OBJECT, 8 << 16)], [read])
                            os.close(read)
                        fds = []

    def close(self) -> None:
        try:
            self.server.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.server.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def echo_through(path: Path | str, contents: list[bytes]) -> list[bytes]:
    """Send each of `contents` in a file descriptor through `path`; what comes back, in order."""
    import tempfile

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC) as client:
        client.settimeout(5)
        client.connect(str(path))
        files = []
        for content in contents:
            handle = tempfile.TemporaryFile()
            handle.write(content)
            handle.flush()
            files.append(handle)
        socket.send_fds(client, [struct.pack("=II", ECHO_OBJECT, 8 << 16)], [f.fileno() for f in files])
        for handle in files:
            handle.close()
        back: list[bytes] = []
        while len(back) < len(contents):
            data, got, _flags, _ = socket.recv_fds(client, 64, 4)
            if not data:
                break
            for fd in got:
                back.append(os.read(fd, 4096))
                os.close(fd)
        return back
