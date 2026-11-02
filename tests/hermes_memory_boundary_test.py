#!/usr/bin/env python3
"""SYRD-727: a fact Hermes saves in one ticket does not reach an unrelated ticket of an ephemeral role.

The SYRD-693 canary saved a fact with Hermes's own `memory` tool, and after the board's fresh-conversation
command a new session recalled it. Here the installed Hermes 0.15.2 runs that sequence for real, twice per
role home: one `hermes -z` is ticket A, the next, a new session, is ticket B. The model is a stand-in that
calls `memory` whenever Hermes offers it and says whether the fact reached it, so the run measures what
Hermes does, not what a model chooses. Everything runs in its own user and network namespace
(`unshare -rn`): nothing can leave the host. The homes are written by Switchyard's own
`prepare_hermes_role_config`, for a role the workflow declares ephemeral and for a persistent one.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import hermes_role_config  # noqa: E402

HERMES = Path("/opt/hermes/venv/bin/hermes")
CHECKS = 0
FACT = "PLUM-72727"

#: An OpenAI-compatible stand-in on 127.0.0.1:PORT: `SAVE: x` calls `memory` if offered; `RECALL: x` answers
#: SEEN when x reached it other than in the question itself (a system prompt carries Hermes's memory).
STAND_IN = r'''
import http.server, json, re, socketserver, sys, time
log = open(sys.argv[2], "a")
class H(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def reply(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_GET(self):
        self.reply({"object": "list", "data": [{"id": "stand-in", "object": "model", "meta": {"n_ctx": 131072}}]})
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        msgs, tools = req.get("messages", []), [t["function"]["name"] for t in req.get("tools") or []]
        asked = next((str(m.get("content")) for m in reversed(msgs) if m.get("role") == "user"), "")
        m = re.search(r"(SAVE|RECALL): *([A-Z0-9-]+)", asked)
        log.write(json.dumps({"tools": tools, "asked": asked[:60]}) + "\n"); log.flush()
        msg, finish = {"role": "assistant", "content": "OK"}, "stop"
        if msgs and msgs[-1].get("role") == "tool":
            msg = {"role": "assistant", "content": "SAVED"}
        elif m and m.group(1) == "SAVE":
            if "memory" in tools:
                msg, finish = {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function",
                    "function": {"name": "memory", "arguments": json.dumps({"action": "add", "target": "memory",
                                 "content": "Ticket fact: " + m.group(2)})}}]}, "tool_calls"
            else:
                msg = {"role": "assistant", "content": "MEMORY_NOT_OFFERED"}
        elif m and m.group(1) == "RECALL":
            msg = {"role": "assistant", "content": "SEEN" if json.dumps(msgs).count(m.group(2)) > 1 else "NOT_SEEN"}
        if not req.get("stream"):
            return self.reply({"id": "x", "object": "chat.completion", "model": "stand-in",
                               "choices": [{"index": 0, "message": msg, "finish_reason": finish}]})
        self.send_response(200); self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked"); self.end_headers()
        def chunk(text):
            data = ("data: " + text + "\n\n").encode()
            self.wfile.write(b"%x\r\n" % len(data) + data + b"\r\n"); self.wfile.flush()
        delta = {"role": "assistant", **({"tool_calls": [{"index": 0, **msg["tool_calls"][0]}]} if msg.get("tool_calls")
                                         else {"content": msg["content"]})}
        base = {"id": "x", "object": "chat.completion.chunk", "model": "stand-in"}
        chunk(json.dumps({**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}))
        chunk(json.dumps({**base, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}))
        chunk("[DONE]"); self.wfile.write(b"0\r\n\r\n"); self.wfile.flush()
    def log_message(self, *a): pass
class S(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
S(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
'''

#: In the namespace: the stand-in, then ticket A and ticket B as two Hermes sessions, printing B's answer.
TICKETS = r'''
python3 -I "$1" 18631 "$2" </dev/null >/dev/null 2>&1 & server=$!
sleep 0.5
cd "$3"
A=$(hermes -z "Ticket A. SAVE: $4" </dev/null 2>&1 | tail -n 3)
B=$(hermes -z "New ticket B, unrelated. RECALL: $4" </dev/null 2>&1 | tail -n 3)
kill "$server"
printf 'A=%s\nB=%s\n' "$(echo $A)" "$(echo $B)"
'''


def check(condition: object, detail: object) -> None:
    global CHECKS
    if not condition:
        raise AssertionError(f"FAILED CHECK: {detail}")
    CHECKS += 1


def home_for(root: Path, name: str, *, ephemeral: bool) -> Path:
    shared, home = root / "shared", root / name
    shared.mkdir(exist_ok=True)
    (shared / "config.yaml").write_text(
        "model:\n  provider: custom\n  default: stand-in\n  base_url: http://127.0.0.1:18631/v1\n"
        "  api_key: no-key-required\n  context_length: 131072\nfallback_providers: []\n"
        "security:\n  allow_lazy_installs: false\nmemory:\n  memory_enabled: true\n  user_profile_enabled: true\n",
        encoding="utf-8")
    home.mkdir()
    (home / "config.yaml").symlink_to(shared / "config.yaml")
    role = SimpleNamespace(role=name, effort="", hermes_local=None, ephemeral=ephemeral, fresh_session_per_ticket=False)
    hermes_role_config.prepare_hermes_role_config(role, hermes_home=home, shared_home=shared)
    return home


def tickets(root: Path, home: Path) -> dict:
    (root / "stand-in.py").write_text(STAND_IN, encoding="utf-8")
    (root / "tickets.sh").write_text(TICKETS, encoding="utf-8")
    log = root / f"{home.name}.log"
    done = subprocess.run(
        ["unshare", "-rn", "bash", "-c", 'ip link set lo up 2>/dev/null; exec bash "$@"', "_", str(root / "tickets.sh"),
         str(root / "stand-in.py"), str(log), str(root), FACT],
        env={"PATH": f"{HERMES.parent}:/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": str(root), "HERMES_HOME": str(home),
             "TERM": "dumb"},
        capture_output=True, text=True, timeout=240, stdin=subprocess.DEVNULL)
    said = dict(line.split("=", 1) for line in done.stdout.splitlines() if line[:2] in ("A=", "B="))
    requests = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.exists() else []
    memory = home / "memories" / "MEMORY.md"
    return {"a": said.get("A", ""), "b": said.get("B", ""), "requests": requests, "stderr": done.stderr[-600:],
            "memory": memory.read_text(encoding="utf-8") if memory.exists() else ""}


def test_an_unrelated_ticket_cannot_recall_what_an_earlier_one_saved() -> None:
    if not HERMES.exists() or shutil.which("unshare") is None:
        print(f"hermes_memory_boundary_test: {HERMES} is not installed here; SKIPPED")
        return
    root = Path(tempfile.mkdtemp(prefix="syrd727-"))
    try:
        ran = {}
        for name, ephemeral in (("ephemeral", True), ("persistent", False)):
            ran[name] = tickets(root, home_for(root, name, ephemeral=ephemeral))
            check(len(ran[name]["requests"]) >= 2, f"{name}: Hermes reached the stand-in for both tickets: {ran[name]}")
        persistent, ephemeral = ran["persistent"], ran["ephemeral"]
        # The canary's leak, reproduced: the test sees it when the boundary is open.
        check(FACT in persistent["memory"] and "SEEN" in persistent["b"] and "NOT_SEEN" not in persistent["b"],
              f"a persistent role keeps Hermes's memory: ticket A saved the fact and ticket B recalled it: {persistent}")
        check("MEMORY_NOT_OFFERED" in ephemeral["a"] and ephemeral["memory"] == ""
              and not any("memory" in r["tools"] or "session_search" in r["tools"] for r in ephemeral["requests"]),
              f"an ephemeral role is never offered Hermes's memory or session search, so ticket A saves nothing: {ephemeral}")
        check("NOT_SEEN" in ephemeral["b"], f"and unrelated ticket B cannot recall the fact: {ephemeral}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def main() -> int:
    for name, case in sorted(globals().items()):
        if name.startswith("test_") and callable(case):
            case()
    print(f"hermes_memory_boundary_test: {CHECKS} checks ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
