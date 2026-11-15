"""A stand-in Anthropic API that answers every /v1/messages with a short streamed reply and logs each request body."""
import json, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PORT, LOG = int(sys.argv[1]), sys.argv[2]

def sse(event, data):
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        self.send_response(404); self.send_header("content-type", "application/json"); self.send_header("content-length", "2"); self.end_headers(); self.wfile.write(b"{}")
    def do_HEAD(self):
        self.send_response(200); self.send_header("content-length", "0"); self.end_headers()
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0); body = self.rfile.read(n)
        with open(LOG, "a") as f:
            f.write(json.dumps({"t": time.time(), "p": self.path, "body": body.decode("utf-8", "replace")}) + "\n")
        if "count_tokens" in self.path:
            out = b'{"input_tokens": 10}'
            self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out); return
        if not self.path.startswith("/v1/messages"):
            self.send_response(404); self.send_header("content-length", "2"); self.end_headers(); self.wfile.write(b"{}"); return
        try:
            model = json.loads(body).get("model", "claude-haiku-5-5")
        except Exception:
            model = "claude-haiku-5-5"
        parts = [
            sse("message_start", {"type": "message_start", "message": {"id": "msg_fake", "type": "message", "role": "assistant", "model": model, "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 1}}}),
            sse("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            sse("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
            sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
            sse("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 1}}),
            sse("message_stop", {"type": "message_stop"}),
        ]
        out = b"".join(parts)
        self.send_response(200); self.send_header("content-type", "text/event-stream"); self.send_header("content-length", str(len(out))); self.end_headers(); self.wfile.write(out)

ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
