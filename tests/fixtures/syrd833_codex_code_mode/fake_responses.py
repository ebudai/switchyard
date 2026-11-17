"""A stand-in OpenAI Responses API for real Codex (SYRD-833): the first "run it" is answered with one tool call that
runs the command in $PROBE_CMD_FILE -- in Codex 0.166's code mode, an `exec` custom tool call whose JavaScript runs it
through `tools.exec_command`; through a plain shell tool where Codex declares one. Everything else gets a short
message. Each request is logged, its tool names and input kinds only."""
import json, os, sys, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
PORT, LOG = int(sys.argv[1]), sys.argv[2]
CMD = open(os.environ["PROBE_CMD_FILE"]).read().strip()


def sse(event, data):
    return f"event: {event}\ndata: {json.dumps({'type': event, **data})}\n\n".encode()


def tool_call(tools):
    names = {t.get("name"): t for t in tools if isinstance(t, dict)}
    for name, args in (("shell", {"command": ["bash", "-lc", CMD]}), ("exec_command", {"cmd": CMD}),
                       ("local_shell", None), ("shell_command", {"command": CMD})):
        if name in names and args is not None:
            return name, args
    return None, None


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"data": [], "models": []}).encode()
        self.send_response(200); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        n = int(self.headers.get("content-length") or 0); raw = self.rfile.read(n)
        try:
            req = json.loads(raw)
        except ValueError:
            req = {}
        with open(LOG, "a") as f:
            f.write(json.dumps({"path": self.path, "tools": [t.get("name") or t.get("type") for t in req.get("tools", []) if isinstance(t, dict)],
                                "input_types": [i.get("type") for i in req.get("input", []) if isinstance(i, dict)],
                                "text": json.dumps(req.get("input", []))[-300:]}) + "\n")
        inputs = req.get("input", []) if isinstance(req.get("input"), list) else []
        asked = "run it" in json.dumps(inputs)
        answered = any(isinstance(i, dict) and i.get("type") in ("function_call_output", "custom_tool_call_output") for i in inputs)
        rid = "resp_" + uuid.uuid4().hex[:12]
        name, args = tool_call(req.get("tools", [])) if asked and not answered else (None, None)
        if asked and not answered and not name:
            # Codex's code mode: the one tool is `exec`, JavaScript in a V8 isolate (the codex-code-mode process) that
            # runs shell commands through its nested tools.
            js = "const r = await tools.exec_command({cmd: " + json.dumps(CMD) + "}); text(JSON.stringify(r));"
            item = {"type": "custom_tool_call", "id": "ctc_" + uuid.uuid4().hex[:8], "call_id": "call_" + uuid.uuid4().hex[:8],
                    "name": "exec", "input": js, "status": "completed"}
        elif name:
            item = {"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:8], "call_id": "call_" + uuid.uuid4().hex[:8],
                    "name": name, "arguments": json.dumps(args), "status": "completed"}
        else:
            item = {"type": "message", "id": "msg_" + uuid.uuid4().hex[:8], "role": "assistant", "status": "completed",
                    "content": [{"type": "output_text", "text": "done", "annotations": []}]}
        events = [sse("response.created", {"response": {"id": rid, "status": "in_progress", "output": []}}),
                  sse("response.output_item.added", {"output_index": 0, "item": item}),
                  sse("response.output_item.done", {"output_index": 0, "item": item}),
                  sse("response.completed", {"response": {"id": rid, "status": "completed", "output": [item],
                                                          "usage": {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15,
                                                                    "input_tokens_details": {"cached_tokens": 0},
                                                                    "output_tokens_details": {"reasoning_tokens": 0}}}})]
        body = b"".join(events)
        self.send_response(200); self.send_header("content-type", "text/event-stream"); self.send_header("content-length", str(len(body))); self.end_headers()
        self.wfile.write(body)


ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
