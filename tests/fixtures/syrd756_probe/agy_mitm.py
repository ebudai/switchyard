"""SYRD-756 probe: a loopback stand-in for agy's Google endpoints, behind an HTTPS CONNECT proxy with a throwaway CA.

agy (a Go binary) honours HTTPS_PROXY and SSL_CERT_FILE. This proxy accepts CONNECT for any host, terminates TLS with
a leaf certificate the probe's own CA signed, and answers every request itself: loadCodeAssist, fetchAvailableModels
(one model, "Probe Flash"), streamGenerateContent (the PROBE-BUSY rule of fake_models.py), and {} for anything else.
Every request is logged. Nothing leaves the namespace.

    python3 agy_mitm.py PORT CERT KEY LOG
"""
import json
import re
import socket
import ssl
import sys
import threading
import time

PORT, CERT, KEY, LOG = int(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
WORD = re.compile(rb"PROBE-[A-Z]+")
MODEL_ID = "probe-flash"
MODELS = {"models": {MODEL_ID: {"displayName": "Probe Flash", "model": "MODEL_PLACEHOLDER_M26",
                                 "apiProvider": "API_PROVIDER_GOOGLE_GEMINI", "modelProvider": "MODEL_PROVIDER_GOOGLE",
                                 "maxTokens": 1000000, "maxOutputTokens": 65536, "recommended": True,
                                 "supportsImages": True, "supportsThinking": False}},
          "defaultAgentModelId": MODEL_ID, "commandModelIds": [MODEL_ID], "tabModelIds": [MODEL_ID],
          "agentModelSorts": [{"displayName": "Recommended", "groups": [{"displayName": "Probe", "modelIds": [MODEL_ID]}]}]}
LOCK = threading.Lock()


def log(entry):
    with LOCK, open(LOG, "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), **entry}) + "\n")


def chunk(text, finish=None):
    candidate = {"content": {"role": "model", "parts": [{"text": text}]}}
    if finish:
        candidate["finishReason"] = finish
    body = {"response": {"candidates": [candidate], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 3,
                                                                      "totalTokenCount": 13}, "responseId": "probe"}}
    return f"data: {json.dumps(body)}\r\n\r\n".encode()


def serve_http(conn, host):
    f = conn.makefile("rb")
    while True:
        line = f.readline()
        if not line:
            return
        method, path, _ = line.decode("latin-1").split(" ", 2)
        headers = {}
        while True:
            h = f.readline().decode("latin-1")
            if h in ("\r\n", "\n", ""):
                break
            k, v = h.split(":", 1)
            headers[k.strip().lower()] = v.strip()
        body = b""
        if "content-length" in headers:
            body = f.read(int(headers["content-length"]))
        elif headers.get("transfer-encoding") == "chunked":
            while True:
                size = int(f.readline().strip() or b"0", 16)
                if size == 0:
                    f.readline()
                    break
                body += f.read(size)
                f.readline()
        words = [w.decode() for w in WORD.findall(body)]
        held = bool(words) and words[-1] == "PROBE-BUSY"
        log({"host": host, "m": method, "p": path, "len": len(body), "words": words, "held": held,
             "ctype": headers.get("content-type", ""), "head": body[:300].decode("utf-8", "replace")})
        if "streamGenerateContent" in path:
            conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: text/event-stream\r\nConnection: close\r\n\r\n")
            if held:
                conn.sendall(chunk("Working on it: step one of many. "))
                time.sleep(900)
                return
            conn.sendall(chunk("PROBE-REPLY ok") + chunk("", "STOP"))
            return
        if "fetchAvailableModels" in path:
            payload = MODELS
        elif "loadCodeAssist" in path:
            payload = {"currentTier": {"id": "standard-tier", "name": "Probe Tier", "userDefinedCloudaicompanionProject": False},
                       "allowedTiers": [{"id": "standard-tier", "name": "Probe Tier", "isDefault": True}],
                       "cloudaicompanionProject": "probe-project"}
        elif "fetchUserInfo" in path:
            payload = {"email": "probe@example.invalid", "name": "Probe"}
        else:
            payload = {}
        data = json.dumps(payload).encode()
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: " + str(len(data)).encode()
                     + b"\r\n\r\n" + data)


def handle(client):
    try:
        f = client.makefile("rb")
        line = f.readline().decode("latin-1")
        while True:
            h = f.readline()
            if h in (b"\r\n", b"\n", b""):
                break
        if not line.startswith("CONNECT "):
            client.close()
            return
        host = line.split()[1]
        client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERT, KEY)
        context.set_alpn_protocols(["http/1.1"])
        tls = context.wrap_socket(client, server_side=True)
        serve_http(tls, host)
    except Exception as exc:  # a probe: say what broke and keep serving
        log({"error": repr(exc)[:300]})
    finally:
        try:
            client.close()
        except OSError:
            pass


server = socket.socket()
server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
server.bind(("127.0.0.1", PORT))
server.listen(50)
while True:
    c, _ = server.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
