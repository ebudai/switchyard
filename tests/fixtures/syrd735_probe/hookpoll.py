"""Hook recorder that, for StopFailure, polls the transcript for up to 3 s for a new API-error record."""
import json, sys, time
out = sys.argv[1]
d = json.load(sys.stdin); start = d["_t"] = time.time()
tp = d.get("transcript_path")
def count():
    try:
        return sum(1 for l in open(tp, "rb").read().decode("utf-8", "replace").splitlines() if '"isApiErrorMessage":true' in l)
    except OSError:
        return -1
if d.get("hook_event_name") == "StopFailure":
    first = count(); d["_seen"] = []
    while time.time() - start < 3:
        n = count(); d["_seen"].append([round(time.time() - start, 3), n])
        if n > first:
            break
        time.sleep(0.05)
    d["_seen"] = d["_seen"][:3] + d["_seen"][-2:]
open(out, "a").write(json.dumps(d) + "\n")
