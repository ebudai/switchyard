"""SYRD-734 probe: a stand-in OpenAI-compatible server: lists one model, and holds every completion open (a turn in flight)."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
MODEL = "Qwen/Qwen3.8-27B"
LOG = sys.argv[2]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        with open(LOG, "a") as f: f.write(f"GET {self.path}\n")
        if self.path.rstrip("/").endswith("/models"):
            self._json({"object": "list", "data": [{"id": MODEL, "object": "model", "max_model_len": 65536, "context_length": 65536}]})
        else:
            self._json({})
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); body = self.rfile.read(n)
        with open(LOG, "a") as f: f.write(f"POST {self.path} {len(body)}\n")
        time.sleep(600)

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
