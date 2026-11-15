"""Log every hook payload with its event, and answer nothing."""
import json, sys, time
data = json.load(sys.stdin)
with open(sys.argv[1], "a") as f:
    f.write(json.dumps({"t": round(time.time(), 2), **data}) + "\n")
