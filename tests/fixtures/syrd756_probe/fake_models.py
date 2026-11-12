"""SYRD-756 probe: one stand-in model server for every runtime, on loopback only.

It answers Anthropic Messages (/v1/messages), OpenAI Chat Completions (/chat/completions) and OpenAI Responses
(/responses), streaming or not, and lists one model. The prompt's LAST probe word decides: a request whose last
`PROBE-<WORD>` is `PROBE-BUSY` is held open -- a turn in flight, for the interrupt to cut -- and any other is answered
"PROBE-REPLY". Each request is logged with the probe words it carried, in order, so a later turn shows whether the
interrupted one is still in its conversation. No model is called.

    python3 fake_models.py PORT LOG
"""
import json
import os
import re
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT, LOG = int(sys.argv[1]), sys.argv[2]
MODEL = sys.argv[3] if len(sys.argv) > 3 else "probe-model"
WORD = re.compile(rb"PROBE-[A-Z]+")
REPLY = "PROBE-REPLY ok"
#: PROBE_PARTIAL=1: a held turn first streams some of its answer, as a long turn has, then stalls.
PARTIAL = os.environ.get("PROBE_PARTIAL") == "1"


def log(entry):
    with open(LOG, "a") as f:
        f.write(json.dumps({"t": round(time.time(), 3), **entry}) + "\n")


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, status, body, ctype="application/json"):
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse(self, events):
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()
        for name, data in events:
            chunk = (f"event: {name}\n" if name else "") + f"data: {data if isinstance(data, str) else json.dumps(data)}\n\n"
            self.wfile.write(chunk.encode())
            self.wfile.flush()
        self.close_connection = True

    def do_GET(self):
        log({"m": "GET", "p": self.path})
        if self.path.rstrip("/").endswith("/models"):
            return self._send(200, json.dumps({"object": "list", "data": [
                {"id": MODEL, "object": "model", "created": 0, "owned_by": "probe", "max_model_len": 65536,
                 "context_length": 65536}]}).encode())
        self._send(404, b"{}")

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("content-length") or 0))
        words = [w.decode() for w in WORD.findall(body)]
        try:
            req = json.loads(body or b"{}")
        except ValueError:
            req = {}
        stream = bool(req.get("stream"))
        held = bool(words) and words[-1] == "PROBE-BUSY"
        log({"m": "POST", "p": self.path, "len": len(body), "words": words, "held": held, "stream": stream})
        if os.environ.get("PROBE_BODIES"):  # every request body, for a person to read
            with open(os.path.join(os.environ["PROBE_BODIES"], f"{time.time():.3f}.json"), "wb") as f:
                f.write(body)
        if held and PARTIAL and self.path.split("?")[0].endswith(("/messages", "/chat/completions", "/responses")) \
                and stream:
            return self._partial(req)
        if held:
            time.sleep(900)
            return
        path = self.path.split("?")[0]
        if path.endswith("/messages/count_tokens"):
            return self._send(200, b'{"input_tokens": 10}')
        if path.endswith("/messages"):
            return self._anthropic(req, stream)
        if path.endswith("/chat/completions"):
            return self._chat(req, stream)
        if path.endswith("/responses"):
            return self._responses()
        self._send(404, b"{}")

    def _partial(self, req):
        """Stream the start of an answer, then hold the stream open: a turn that has already said something."""
        path = self.path.split("?")[0]
        text = "Working on it: step one of many. "
        if path.endswith("/messages"):
            message = {"id": "msg_busy", "type": "message", "role": "assistant", "model": req.get("model", MODEL),
                       "content": [], "stop_reason": None, "stop_sequence": None,
                       "usage": {"input_tokens": 10, "output_tokens": 1}}
            events = [("message_start", {"type": "message_start", "message": message}),
                      ("content_block_start", {"type": "content_block_start", "index": 0,
                                               "content_block": {"type": "text", "text": ""}}),
                      ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                               "delta": {"type": "text_delta", "text": text}})]
        elif path.endswith("/chat/completions"):
            base = {"id": "c0", "object": "chat.completion.chunk", "created": int(time.time()), "model": req.get("model", MODEL)}
            events = [("", {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text},
                                                 "finish_reason": None}]})]
        else:
            events = [("response.created", {"type": "response.created", "response": {"id": "resp_busy"}}),
                      ("response.output_item.added", {"type": "response.output_item.added", "output_index": 0,
                       "item": {"type": "message", "role": "assistant", "id": "msg_busy", "status": "in_progress", "content": []}}),
                      ("response.output_text.delta", {"type": "response.output_text.delta", "output_index": 0,
                                                      "content_index": 0, "item_id": "msg_busy", "delta": text})]
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        for name, data in events:
            self.wfile.write(((f"event: {name}\n" if name else "") + f"data: {json.dumps(data)}\n\n").encode())
            self.wfile.flush()
        time.sleep(900)

    def _anthropic(self, req, stream):
        model = req.get("model", MODEL)
        usage = {"input_tokens": 10, "output_tokens": 3, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        message = {"id": "msg_probe", "type": "message", "role": "assistant", "model": model, "content": [],
                   "stop_reason": None, "stop_sequence": None, "usage": usage}
        if not stream:
            return self._send(200, json.dumps({**message, "content": [{"type": "text", "text": REPLY}],
                                               "stop_reason": "end_turn"}).encode())
        self._sse([
            ("message_start", {"type": "message_start", "message": message}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": REPLY}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 3}}),
            ("message_stop", {"type": "message_stop"}),
        ])

    def _chat(self, req, stream):
        model = req.get("model", MODEL)
        usage = {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
        if not stream:
            return self._send(200, json.dumps({"id": "c1", "object": "chat.completion", "created": int(time.time()),
                                               "model": model, "usage": usage, "choices": [{
                                                   "index": 0, "finish_reason": "stop",
                                                   "message": {"role": "assistant", "content": REPLY}}]}).encode())
        base = {"id": "c1", "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
        self._sse([
            ("", {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": REPLY}, "finish_reason": None}]}),
            ("", {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage}),
            ("", "[DONE]"),
        ])

    def _responses(self):
        item = {"type": "message", "role": "assistant", "id": "msg_probe", "status": "completed",
                "content": [{"type": "output_text", "text": REPLY, "annotations": []}]}
        self._sse([
            ("response.created", {"type": "response.created", "response": {"id": "resp_probe"}}),
            ("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": item}),
            ("response.completed", {"type": "response.completed", "response": {"id": "resp_probe", "usage": {
                "input_tokens": 10, "input_tokens_details": None, "output_tokens": 3, "output_tokens_details": None,
                "total_tokens": 13}}}),
        ])


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
