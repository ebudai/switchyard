"""SYRD-756 probe: record one hook call -- the event, when, and the session and turn fields it carried -- and say nothing.

    hooklog.py LOG EVENT   (stdin: the runtime's hook payload)
"""
import json
import select
import sys
import time

log, event = sys.argv[1], sys.argv[2]
raw = ""
if select.select([sys.stdin], [], [], 2.0)[0]:
    raw = sys.stdin.read()
try:
    data = json.loads(raw) if raw.strip() else {}
except ValueError:
    data = {"_unparsed": raw[:200]}
keep = {k: data[k] for k in ("hook_event_name", "session_id", "sessionId", "reason", "stop_hook_active", "source",
                             "turn_id", "prompt", "matcher") if isinstance(data, dict) and k in data}
with open(log, "a") as f:
    f.write(json.dumps({"t": round(time.time(), 3), "event": event, **keep}) + "\n")
if event.startswith("copilot."):
    print("{}")
