#!/usr/bin/env python3
"""SYRD-718: a stand-in for this host's Tailscale Serve in front of a board, as tailscaled 1.104.2 proxies.

What it copies from tailscaled's reverse proxy (ipn/ipnlocal/serve.go, `addProxyForwardedHeaders` and
`addTailscaleIdentityHeaders`): it keeps the browser's Host; sets X-Forwarded-Host to it, X-Forwarded-Proto to
https (tailscaled terminated TLS) and X-Forwarded-For to the peer's address; deletes every Tailscale-User-*,
Tailscale-Funnel-Request, Tailscale-Headers-Info and Tailscale-App-Capabilities header the client sent; then marks a
Funnel request, or names the login of an untagged tailnet peer -- and nothing for a tagged peer or the host's own
traffic, which WhoIs does not name.

Which peer a request comes from is tailscaled's WhoIs; here it is the stand-in's `peer`, or a test's X-Standin-Peer
header, which it removes: `login:<login>@<tailnet address>`, `tagged@<tailnet address>`, `funnel` or `local`. A test's
X-Standin-Plain header stands for `tailscale serve --http`: no TLS, so no X-Forwarded-Proto.

Run as a program (`python3 -I tailscale_serve_standin.py BOARD_PORT [CERT KEY]`), it prints its port and serves until
it is killed. Standard library only, so it runs as any account.
"""

from __future__ import annotations

import http.client
import ssl
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONTROL, PLAIN = "X-Standin-Peer", "X-Standin-Plain"
#: What tailscaled deletes from the client's request before it decides what to add.
STRIPPED = {"tailscale-user-login", "tailscale-user-name", "tailscale-user-profile-pic", "tailscale-funnel-request",
            "tailscale-headers-info", "tailscale-app-capabilities"}
#: What tailscaled sets (Header.Set), replacing whatever the client sent.
REPLACED = {"x-forwarded-host", "x-forwarded-proto", "x-forwarded-for"}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade"}
HOST_TAILNET_ADDRESS = "100.64.0.1"
FUNNEL_SOURCE = "203.0.113.7"


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"  # one request a connection: an event stream ends when the board closes it

    def log_message(self, *args: object) -> None:
        pass

    def _forward(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        peer, tls = self.headers.get(CONTROL) or self.server.peer, self.headers.get(PLAIN) is None
        headers = [(k, v) for k, v in self.headers.items()
                   if k.lower() not in STRIPPED | REPLACED | HOP_BY_HOP | {CONTROL.lower(), PLAIN.lower()}]
        if peer.startswith("login:"):
            kind, (login, _, source) = "login", peer.removeprefix("login:").rpartition("@")
        elif peer.startswith("tagged@"):
            kind, login, source = "tagged", "", peer.removeprefix("tagged@")
        else:
            kind, login, source = peer, "", {"funnel": FUNNEL_SOURCE, "local": HOST_TAILNET_ADDRESS}[peer]
        headers += [("X-Forwarded-Host", self.headers.get("Host", ""))] + [("X-Forwarded-Proto", "https")] * tls \
            + [("X-Forwarded-For", source)]
        if kind == "funnel":
            headers.append(("Tailscale-Funnel-Request", "?1"))
        elif kind == "login":
            headers += [("Tailscale-User-Login", login), ("Tailscale-User-Name", login.partition("@")[0]),
                        ("Tailscale-User-Profile-Pic", ""), ("Tailscale-Headers-Info", "https://tailscale.com/s/serve-headers")]
        conn = http.client.HTTPConnection("127.0.0.1", self.server.board_port, timeout=60)
        try:
            conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                conn.putheader(name, value)
            conn.endheaders(body)
            answer = conn.getresponse()
        except OSError as exc:  # the board is down: as tailscaled answers then
            self.send_error(502, f"stand-in: {exc}")
            conn.close()
            return
        self.send_response(answer.status, answer.reason)
        for name, value in answer.getheaders():
            if name.lower() not in HOP_BY_HOP | {"server", "date"}:
                self.send_header(name, value)
        self.end_headers()
        try:
            while chunk := answer.read1(65536):
                self.wfile.write(chunk)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            conn.close()

    do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = _forward


class Standin(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, board_port: int, *, peer: str = "local", cert: str = "", key: str = "") -> None:
        self.board_port, self.peer = board_port, peer
        super().__init__(("127.0.0.1", 0), Handler)
        if cert:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert, key)
            self.socket = context.wrap_socket(self.socket, server_side=True)

    def start(self) -> "Standin":
        threading.Thread(target=self.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


if __name__ == "__main__":
    standin = Standin(int(sys.argv[1]), cert=sys.argv[2] if len(sys.argv) > 3 else "",
                      key=sys.argv[3] if len(sys.argv) > 3 else "")
    print(standin.server_address[1], flush=True)
    standin.serve_forever()
