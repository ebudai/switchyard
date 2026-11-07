"""A stand-in Anthropic API: every /v1/messages is refused by a subscription limit, as a 429 with unified headers."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PORT, RESET, CLAIM, LOG = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3], sys.argv[4]

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _log(self, body=b""):
        with open(LOG, "a") as f:
            f.write(json.dumps({"t": time.time(), "m": self.command, "p": self.path, "len": len(body)}) + "\n")
    def do_GET(self):
        self._log(); self.send_response(404); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(b"{}")
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0); body = self.rfile.read(n); self._log(body)
        if self.path.startswith("/v1/messages") and "count_tokens" not in self.path:
            payload = json.dumps({"type": "error", "error": {"type": "rate_limit_error", "message": "Rate limited"}}).encode()
            self.send_response(429)
            for k, v in (("content-type", "application/json"), ("anthropic-ratelimit-unified-status", "rejected"),
                         ("anthropic-ratelimit-unified-reset", str(RESET)),
                         ("anthropic-ratelimit-unified-representative-claim", CLAIM),
                         ("anthropic-ratelimit-unified-7d-status", "rejected" if CLAIM == "seven_day" else "allowed"),
                         ("anthropic-ratelimit-unified-7d-reset", str(RESET)),
                         ("anthropic-ratelimit-unified-5h-status", "rejected" if CLAIM == "five_hour" else "allowed"),
                         ("anthropic-ratelimit-unified-5h-reset", str(RESET)),
                         ("anthropic-ratelimit-unified-overage-status", "rejected"),
                         ("anthropic-ratelimit-unified-overage-disabled-reason", "org_level_disabled"),
                         ("request-id", "req_fake")):
                self.send_header(k, v)
            self.send_header("content-length", str(len(payload))); self.end_headers(); self.wfile.write(payload); return
        self.send_response(404); self.send_header("content-type", "application/json"); self.end_headers(); self.wfile.write(b"{}")

ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
